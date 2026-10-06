"""Action Attemptの状態依存不変条件。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, cast

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash
from harness.domain.timestamps import canonical_timestamp
from harness.domain.transitions import ACTION_ATTEMPT_TERMINAL_STATES

__all__ = ["ActionAttempt"]


@dataclass(frozen=True, slots=True)
class ActionAttempt:
    """一回のAction実行を表す不変Record。

    すべての状態遷移は新しいRecordを返す。Repositoryはこれをstore_versionでCASし、
    Ledger EventのAppendと同一Transactionで永続化する。
    """

    attempt_id: str
    action_id: str
    attempt_number: int
    state: str = "PLANNING"
    plan_content_hash: ContentHash | None = None
    execution_plan_hash: ContentHash | None = None
    worker_id: str | None = None
    claim_id: str | None = None
    lease_id: str | None = None
    fencing_token: int | None = None
    runtime_attestation_hash: ContentHash | None = None
    operation_journal_id: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    receipt_ids: tuple[str, ...] = ()
    error_classification: str | None = None
    store_version: int = 1

    def __post_init__(self) -> None:
        if not self.attempt_id or not self.action_id or self.attempt_number < 1:
            raise ValueError("attempt identity fields must be valid")
        if self.store_version < 1:
            raise ValueError("store_version must be >= 1")
        if (self.plan_content_hash is None) != (self.execution_plan_hash is None):
            raise ValueError(
                "plan_content_hash and execution_plan_hash must be both set or both absent"
            )
        for value in (self.started_at, self.ended_at):
            if value is not None:
                canonical_timestamp(value)
        if self.state in ACTION_ATTEMPT_TERMINAL_STATES and self.ended_at is None:
            raise ValueError("terminal ActionAttempt requires ended_at")

    def with_plan(
        self, plan_content_hash: ContentHash, execution_plan_hash: ContentHash
    ) -> ActionAttempt:
        self._require_state("PLANNING")
        return self._transition(
            "WAITING_POLICY",
            plan_content_hash=plan_content_hash,
            execution_plan_hash=execution_plan_hash,
        )

    def ready(self) -> ActionAttempt:
        self._require_state("WAITING_POLICY", "WAITING_APPROVAL")
        self._require_plan()
        return self._transition("READY")

    def claim(self, *, worker_id: str, claim_id: str) -> ActionAttempt:
        self._require_state("READY")
        self._require_plan()
        if not worker_id or not claim_id:
            raise ValueError("worker_id and claim_id must not be empty")
        return self._transition("CLAIMED", worker_id=worker_id, claim_id=claim_id)

    def with_lease(self, *, lease_id: str, fencing_token: int) -> ActionAttempt:
        self._require_state("CLAIMED")
        if not lease_id or fencing_token < 1:
            raise ValueError("lease_id and fencing_token must be valid")
        return self._transition("LEASED", lease_id=lease_id, fencing_token=fencing_token)

    def with_runtime_attestation(self, runtime_attestation_hash: ContentHash) -> ActionAttempt:
        self._require_state("LEASED")
        return self._transition(
            "RUNTIME_VERIFIED", runtime_attestation_hash=runtime_attestation_hash
        )

    def start(self, started_at: str) -> ActionAttempt:
        self._require_state("RUNTIME_VERIFIED")
        canonical_timestamp(started_at)
        return self._transition("RUNNING", started_at=started_at)

    def prepare_effect(self, operation_journal_id: str) -> ActionAttempt:
        self._require_state("RUNNING")
        if not operation_journal_id:
            raise ValueError("operation_journal_id must not be empty")
        if self.fencing_token is None:
            raise HarnessError(
                ErrorCode.STALE_FENCING_TOKEN, "effect requires a current fencing token"
            )
        return self._transition("PREPARED_DURABLE", operation_journal_id=operation_journal_id)

    def with_receipt(self, receipt_id: str) -> ActionAttempt:
        self._require_state("PREPARED_DURABLE", "EFFECT_IN_FLIGHT", "EFFECT_VERIFIED")
        if not receipt_id or receipt_id in self.receipt_ids:
            raise ValueError("receipt_id must be new and non-empty")
        return self._transition("RECEIPT_DURABLE", receipt_ids=(*self.receipt_ids, receipt_id))

    def succeed(self, ended_at: str) -> ActionAttempt:
        self._require_state("RUNNING", "RECEIPT_DURABLE")
        canonical_timestamp(ended_at)
        if self.started_at is None or self.runtime_attestation_hash is None:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH, "success requires runtime attestation and start"
            )
        if self.operation_journal_id is None or not self.receipt_ids:
            raise HarnessError(
                ErrorCode.UNRECONCILED_EFFECT_PRESENT,
                "effectful success requires operation journal and durable receipt",
            )
        return self._transition("SUCCEEDED", ended_at=ended_at)

    def fail(self, *, retryable: bool, error_classification: str, ended_at: str) -> ActionAttempt:
        if not error_classification:
            raise ValueError("error_classification must not be empty")
        self._require_not_terminal()
        canonical_timestamp(ended_at)
        return self._transition(
            "FAILED_RETRYABLE" if retryable else "FAILED_PERMANENT",
            ended_at=ended_at,
            error_classification=error_classification,
        )

    def block(self, *, state: str, error_classification: str, ended_at: str) -> ActionAttempt:
        if state not in {
            "BLOCKED_POLICY",
            "BLOCKED_APPROVAL",
            "BLOCKED_CONFLICT",
            "EFFECT_UNKNOWN",
        }:
            raise ValueError("state must be a terminal blocked ActionAttempt state")
        if not error_classification:
            raise ValueError("error_classification must not be empty")
        self._require_not_terminal()
        canonical_timestamp(ended_at)
        return self._transition(state, ended_at=ended_at, error_classification=error_classification)

    def _transition(self, state: str, **changes: object) -> ActionAttempt:
        self._require_not_terminal()
        # 個々の公開遷移は全て型付き引数だけを受け取る。dataclasses.replaceは
        # **kwargsをFieldごとに型推論できないため、ここだけ境界としてAnyへ狭める。
        updated = replace(
            cast(Any, self),
            state=state,
            store_version=self.store_version + 1,
            **changes,
        )
        return cast(ActionAttempt, updated)

    def _require_state(self, *expected: str) -> None:
        self._require_not_terminal()
        if self.state not in expected:
            raise HarnessError(
                ErrorCode.EVENT_ORDER_VIOLATION,
                f"state {self.state} does not permit this transition",
            )

    def _require_not_terminal(self) -> None:
        if self.state in ACTION_ATTEMPT_TERMINAL_STATES:
            raise HarnessError(
                ErrorCode.EVENT_ORDER_VIOLATION, "terminal attempt cannot be resumed"
            )

    def _require_plan(self) -> None:
        if self.plan_content_hash is None or self.execution_plan_hash is None:
            raise HarnessError(ErrorCode.EVENT_ORDER_VIOLATION, "plan hashes are required")
