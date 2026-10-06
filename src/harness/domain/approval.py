"""Approval GrantのDomain規則。

署名の暗号学的検証とNonceの永続的一意性はInfrastructure／Repositoryが担う。
本Moduleは、検証済みGrantをどの条件で一回だけ消費できるかを純粋に判定する。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash
from harness.domain.timestamps import canonical_timestamp, timestamp_seconds

__all__ = ["ApprovalGrant", "ApprovalStatus"]


class ApprovalStatus(Enum):
    ISSUED = "ISSUED"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    INVALIDATED = "INVALIDATED"


@dataclass(frozen=True, slots=True)
class ApprovalGrant:
    """Execution Planへ束縛される署名済み・一回限りGrant。"""

    grant_id: str
    plan_content_hash: ContentHash
    execution_plan_hash: ContentHash
    action_scope: tuple[str, ...]
    approver_subject_id: str
    approver_tenant_id: str
    authentication_context_class: str
    mfa_performed: bool
    authentication_time: str
    issued_at: str
    not_before: str
    expires_at: str
    maximum_clock_skew_seconds: int
    nonce: str
    revocation_epoch: int
    issuer_id: str
    issuer_key_id: str
    signature_algorithm: str
    signature: str
    status: ApprovalStatus
    store_version: int
    consumed_at: str | None = None
    consumed_by_actor_id: str | None = None
    attempt_id: str | None = None
    revoked_at: str | None = None
    revoked_by_actor_id: str | None = None
    revocation_reason: str | None = None
    invalidated_at: str | None = None
    invalidation_reason: str | None = None
    delegation_id: str | None = None

    def __post_init__(self) -> None:
        if not all(
            (
                self.grant_id,
                self.action_scope,
                self.approver_subject_id,
                self.approver_tenant_id,
                self.authentication_context_class,
                self.nonce,
                self.issuer_id,
                self.issuer_key_id,
                self.signature_algorithm,
                self.signature,
            )
        ):
            raise ValueError("ApprovalGrant required identity fields must not be empty")
        if self.maximum_clock_skew_seconds < 0:
            raise ValueError("maximum_clock_skew_seconds must not be negative")
        if self.store_version < 1 or self.revocation_epoch < 0:
            raise ValueError("store_version must be >= 1 and revocation_epoch must not be negative")
        canonical_timestamp(self.authentication_time)
        canonical_timestamp(self.issued_at)
        canonical_timestamp(self.not_before)
        canonical_timestamp(self.expires_at)
        if timestamp_seconds(self.expires_at) <= timestamp_seconds(self.not_before):
            raise ValueError("expires_at must be later than not_before")
        if self.status is ApprovalStatus.CONSUMED:
            if not all((self.consumed_at, self.consumed_by_actor_id, self.attempt_id)):
                raise ValueError("consumed ApprovalGrant requires consumed_at, actor, and attempt")
        elif any((self.consumed_at, self.consumed_by_actor_id, self.attempt_id)):
            raise ValueError("unconsumed ApprovalGrant must not have consumption fields")

    @property
    def use_count(self) -> int:
        """Schema契約の固定値。複数利用を表せない。"""
        return 1

    def consume(
        self,
        *,
        now: str,
        expected_execution_plan_hash: ContentHash,
        current_revocation_epoch: int,
        actor_id: str,
        attempt_id: str,
        signature_valid: bool,
    ) -> ApprovalGrant:
        """すべての承認前提を検証し、新しいCONSUMED Recordを返す。

        Repositoryはこの判断と``status=ISSUED``条件付き更新を一つのSQLite CASで実行する。
        このDomain関数だけでは並行消費を防げないため、呼出側の永続化境界が必須である。
        """
        canonical_timestamp(now)
        if not signature_valid:
            raise HarnessError(
                ErrorCode.APPROVAL_ISSUER_UNTRUSTED, "approval signature was not valid"
            )
        if self.status is ApprovalStatus.CONSUMED:
            raise HarnessError(ErrorCode.APPROVAL_REPLAY, "approval grant was already consumed")
        if self.status is not ApprovalStatus.ISSUED:
            raise HarnessError(
                ErrorCode.APPROVAL_INVALIDATED, "approval grant is not in ISSUED state"
            )
        if self.execution_plan_hash != expected_execution_plan_hash:
            raise HarnessError(ErrorCode.APPROVAL_INVALIDATED, "execution plan authority changed")
        if self.revocation_epoch != current_revocation_epoch:
            raise HarnessError(ErrorCode.APPROVAL_INVALIDATED, "grant revocation epoch is stale")
        if not actor_id or not attempt_id:
            raise HarnessError(ErrorCode.APPROVAL_REQUIRED, "actor_id and attempt_id are required")

        now_seconds = timestamp_seconds(now)
        if (
            now_seconds < timestamp_seconds(self.not_before) - self.maximum_clock_skew_seconds
            or now_seconds > timestamp_seconds(self.expires_at) + self.maximum_clock_skew_seconds
        ):
            raise HarnessError(
                ErrorCode.CLOCK_SKEW_EXCEEDED, "approval grant is outside its permitted window"
            )
        return replace(
            self,
            status=ApprovalStatus.CONSUMED,
            consumed_at=now,
            consumed_by_actor_id=actor_id,
            attempt_id=attempt_id,
            store_version=self.store_version + 1,
        )

    def invalidate(self, *, now: str, reason: str) -> ApprovalGrant:
        canonical_timestamp(now)
        if self.status is not ApprovalStatus.ISSUED:
            raise HarnessError(
                ErrorCode.APPROVAL_INVALIDATED, "only ISSUED grants can be invalidated"
            )
        if not reason:
            raise ValueError("invalidation reason must not be empty")
        return replace(
            self,
            status=ApprovalStatus.INVALIDATED,
            invalidated_at=now,
            invalidation_reason=reason,
            store_version=self.store_version + 1,
        )
