"""Effect ProtocolとRecoveryの純粋Domain規則。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from harness.domain._registry_generated import EventType
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash, hash_canonical
from harness.domain.timestamps import canonical_timestamp

__all__ = [
    "EffectState",
    "OperationJournal",
    "RecoveryDecision",
    "RecoveryObservation",
    "decide_recovery",
]


class EffectState(Enum):
    PREPARED_DURABLE = "PREPARED_DURABLE"
    EXECUTION_ATTEMPTED = EventType.EXECUTION_ATTEMPTED.value
    EFFECT_OBSERVED = EventType.EFFECT_OBSERVED.value
    RECEIPT_DURABLE = "RECEIPT_DURABLE"
    EFFECT_UNKNOWN = "EFFECT_UNKNOWN"
    EFFECT_CONFLICT = "EFFECT_CONFLICT"


class RecoveryDecision(Enum):
    CONTINUE_PREPARED_COMMIT = "CONTINUE_PREPARED_COMMIT"
    COMPLETE_OBSERVATION_AND_RECEIPT = "COMPLETE_OBSERVATION_AND_RECEIPT"
    COMPLETE_ACTION_COMMIT = "COMPLETE_ACTION_COMMIT"
    REQUIRES_NEW_APPROVED_ATTEMPT = "REQUIRES_NEW_APPROVED_ATTEMPT"
    EFFECT_CONFLICT = "EFFECT_CONFLICT"
    EFFECT_UNKNOWN = "EFFECT_UNKNOWN"


@dataclass(frozen=True, slots=True)
class RecoveryObservation:
    observed_hash: ContentHash | None
    base_hash_matches: bool
    effect_absence_proven: bool
    receipt_exists: bool


@dataclass(frozen=True, slots=True)
class OperationJournal:
    """一意EffectのAppend-only遷移を表現するJournal Record。

    各遷移はRepositoryがstore_versionをCASして新Recordとして監査する。現在の
    Modelはその遷移を計算するだけで、過去Recordを上書きしない。
    """

    operation_journal_id: str
    operation_id: str
    effect_id: str
    run_id: str
    action_id: str
    attempt_id: str
    operation_type: str
    target_resource_identity: str
    fencing_token: int
    before_hash: ContentHash
    expected_after_hash: ContentHash
    prepared_event_id: str
    prepared_at: str
    durability_level: str
    state: EffectState = EffectState.PREPARED_DURABLE
    execution_attempted_event_id: str | None = None
    execution_attempted_at: str | None = None
    observed_hash: ContentHash | None = None
    observation_method: str | None = None
    observed_at: str | None = None
    receipt_id: str | None = None
    error_classification: str | None = None
    store_version: int = 1

    def __post_init__(self) -> None:
        if not all(
            (
                self.operation_journal_id,
                self.operation_id,
                self.effect_id,
                self.run_id,
                self.action_id,
                self.attempt_id,
                self.operation_type,
                self.target_resource_identity,
                self.prepared_event_id,
                self.durability_level,
            )
        ):
            raise ValueError("OperationJournal identity and prepared fields must not be empty")
        if self.fencing_token < 1 or self.store_version < 1:
            raise ValueError("fencing_token and store_version must be >= 1")
        canonical_timestamp(self.prepared_at)
        if self.state in (
            EffectState.EXECUTION_ATTEMPTED,
            EffectState.EFFECT_OBSERVED,
            EffectState.RECEIPT_DURABLE,
        ):
            if not self.execution_attempted_event_id or self.execution_attempted_at is None:
                raise ValueError("execution state requires execution attempted evidence")
        if self.execution_attempted_at is not None:
            canonical_timestamp(self.execution_attempted_at)
        if self.state in (EffectState.EFFECT_OBSERVED, EffectState.RECEIPT_DURABLE):
            if not self.observed_hash or not self.observation_method or self.observed_at is None:
                raise ValueError("observed state requires observed hash, method, and time")
        if self.observed_at is not None:
            canonical_timestamp(self.observed_at)
        if self.state is EffectState.RECEIPT_DURABLE and not self.receipt_id:
            raise ValueError("receipt durable state requires receipt_id")
        if self.state in (EffectState.EFFECT_UNKNOWN, EffectState.EFFECT_CONFLICT):
            if not self.error_classification:
                raise ValueError("unknown or conflict state requires an error classification")

    @property
    def journal_hash(self) -> ContentHash:
        return hash_canonical(self._projection(), artifact_type="operation-journal", schema_major=1)

    def mark_execution_attempted(self, *, event_id: str, at: str) -> OperationJournal:
        self._require_state(EffectState.PREPARED_DURABLE)
        canonical_timestamp(at)
        if not event_id:
            raise ValueError("event_id must not be empty")
        return replace(
            self,
            state=EffectState.EXECUTION_ATTEMPTED,
            execution_attempted_event_id=event_id,
            execution_attempted_at=at,
            store_version=self.store_version + 1,
        )

    def mark_observed(
        self,
        *,
        observed_hash: ContentHash,
        observation_method: str,
        at: str,
    ) -> OperationJournal:
        self._require_state(EffectState.EXECUTION_ATTEMPTED)
        canonical_timestamp(at)
        if not observation_method:
            raise ValueError("observation_method must not be empty")
        if observed_hash != self.expected_after_hash:
            raise HarnessError(
                ErrorCode.EFFECT_UNKNOWN, "effect result does not match expected hash"
            )
        return replace(
            self,
            state=EffectState.EFFECT_OBSERVED,
            observed_hash=observed_hash,
            observation_method=observation_method,
            observed_at=at,
            store_version=self.store_version + 1,
        )

    def store_receipt(self, receipt_id: str) -> OperationJournal:
        self._require_state(EffectState.EFFECT_OBSERVED)
        if not receipt_id:
            raise ValueError("receipt_id must not be empty")
        return replace(
            self,
            state=EffectState.RECEIPT_DURABLE,
            receipt_id=receipt_id,
            store_version=self.store_version + 1,
        )

    def mark_unknown(self, classification: str) -> OperationJournal:
        if self.state in (EffectState.RECEIPT_DURABLE, EffectState.EFFECT_UNKNOWN):
            raise HarnessError(
                ErrorCode.EFFECT_UNKNOWN, "cannot change a durable or unknown effect"
            )
        if not classification:
            raise ValueError("classification must not be empty")
        return replace(
            self,
            state=EffectState.EFFECT_UNKNOWN,
            error_classification=classification,
            store_version=self.store_version + 1,
        )

    def _require_state(self, expected: EffectState) -> None:
        if self.state is not expected:
            raise HarnessError(
                ErrorCode.EFFECT_UNKNOWN,
                f"effect transition requires {expected.value}, found {self.state.value}",
            )

    def _projection(self) -> dict[str, Any]:
        return {
            "operation_journal_id": self.operation_journal_id,
            "operation_id": self.operation_id,
            "effect_id": self.effect_id,
            "run_id": self.run_id,
            "action_id": self.action_id,
            "attempt_id": self.attempt_id,
            "operation_type": self.operation_type,
            "target_resource_identity": self.target_resource_identity,
            "fencing_token": self.fencing_token,
            "before_hash": str(self.before_hash),
            "expected_after_hash": str(self.expected_after_hash),
            "prepared_event_id": self.prepared_event_id,
            "prepared_at": self.prepared_at,
            "durability_level": self.durability_level,
            "state": self.state.value,
            "execution_attempted_event_id": self.execution_attempted_event_id,
            "execution_attempted_at": _datetime_string(self.execution_attempted_at),
            "observed_hash": _hash_string(self.observed_hash),
            "observation_method": self.observation_method,
            "observed_at": _datetime_string(self.observed_at),
            "receipt_id": self.receipt_id,
            "error_classification": self.error_classification,
            "store_version": self.store_version,
        }


def decide_recovery(journal: OperationJournal, observed: RecoveryObservation) -> RecoveryDecision:
    """設計§3.10に従い、推測を含む再実行を拒否する。"""
    if journal.state in (EffectState.EFFECT_UNKNOWN, EffectState.EFFECT_CONFLICT):
        return RecoveryDecision.EFFECT_UNKNOWN
    if journal.state is EffectState.RECEIPT_DURABLE:
        return (
            RecoveryDecision.COMPLETE_ACTION_COMMIT
            if observed.receipt_exists and observed.observed_hash == journal.expected_after_hash
            else RecoveryDecision.EFFECT_UNKNOWN
        )
    if observed.observed_hash == journal.expected_after_hash:
        return RecoveryDecision.COMPLETE_OBSERVATION_AND_RECEIPT
    if journal.state is EffectState.PREPARED_DURABLE:
        return (
            RecoveryDecision.CONTINUE_PREPARED_COMMIT
            if observed.base_hash_matches and observed.effect_absence_proven
            else RecoveryDecision.EFFECT_CONFLICT
        )
    if journal.state is EffectState.EXECUTION_ATTEMPTED:
        return (
            RecoveryDecision.REQUIRES_NEW_APPROVED_ATTEMPT
            if observed.base_hash_matches and observed.effect_absence_proven
            else RecoveryDecision.EFFECT_UNKNOWN
        )
    return RecoveryDecision.EFFECT_UNKNOWN


def _datetime_string(value: str | None) -> str | None:
    return value


def _hash_string(value: ContentHash | None) -> str | None:
    return None if value is None else str(value)
