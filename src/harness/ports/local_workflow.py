"""Ports for the offline, single-file workflow. All state uses the same UoW."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from harness.domain.approval import ApprovalGrant
from harness.domain.attempt import ActionAttempt
from harness.domain.hashing import ContentHash
from harness.ports.storage_commit import StorageCommitGuardPort


@dataclass(frozen=True, slots=True)
class WorkflowRecord:
    run_id: str
    state: str
    version: int
    document_hash: ContentHash


class WorkflowStorePort(Protocol):
    def get(self, run_id: str) -> WorkflowRecord | None: ...
    def save(self, record: WorkflowRecord, *, expected_version: int) -> None: ...


class AttemptStorePort(Protocol):
    def create(self, attempt: ActionAttempt) -> None: ...
    def get(self, attempt_id: str) -> ActionAttempt | None: ...
    def update(self, attempt: ActionAttempt, *, expected_store_version: int) -> None: ...


class LocalWorkspacePort(Protocol):
    def snapshot(self) -> dict[str, str]: ...
    def identity(self) -> str: ...
    def read(self, relative_path: str) -> bytes: ...
    def prepare(
        self,
        *,
        relative_path: str,
        effect_id: str,
        before_hash: ContentHash,
        replacement: bytes,
        guard: StorageCommitGuardPort,
    ) -> dict[str, Any]: ...
    def commit(self, prepared: dict[str, Any], guard: StorageCommitGuardPort) -> ContentHash: ...


class LocalAuthorityPort(Protocol):
    def subject(self, auth_session: str) -> str: ...
    def issue(
        self,
        *,
        run_id: str,
        plan_content_hash: ContentHash,
        execution_plan_hash: ContentHash,
        scope: tuple[str, ...],
        subject: str,
        now: str,
        expires_at: str,
        maximum_clock_skew_seconds: int = 0,
    ) -> tuple[ApprovalGrant, str]:
        """承認を発行する。

        ``maximum_clock_skew_seconds`` は `ApprovalGrant` が既に持つ契約 Field で
        あり、消費時の時刻窓の許容量である。**既定は 0 のままにする。** 既存の
        Offline Workflow の挙動を変えない。値を上げる呼出側は、その根拠を Plan へ
        束縛して記録する。
        """
        ...

    def verify(self, grant: ApprovalGrant, public_key: str) -> bool: ...
