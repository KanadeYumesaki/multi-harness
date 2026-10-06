"""署名済みApproval Grantの発行・検証・消費前判断。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from harness.domain.approval import ApprovalGrant, ApprovalStatus
from harness.domain.canonical import canonicalize
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash
from harness.ports.signer import SignerPort

__all__ = ["ApprovalIssueRequest", "ApprovalService", "approval_signature_payload"]


@dataclass(frozen=True, slots=True)
class ApprovalIssueRequest:
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
    delegation_id: str | None = None


class ApprovalService:
    def __init__(self, signer: SignerPort) -> None:
        self._signer = signer

    def issue(self, request: ApprovalIssueRequest) -> ApprovalGrant:
        unsigned = ApprovalGrant(
            grant_id=request.grant_id,
            plan_content_hash=request.plan_content_hash,
            execution_plan_hash=request.execution_plan_hash,
            action_scope=request.action_scope,
            approver_subject_id=request.approver_subject_id,
            approver_tenant_id=request.approver_tenant_id,
            authentication_context_class=request.authentication_context_class,
            mfa_performed=request.mfa_performed,
            authentication_time=request.authentication_time,
            issued_at=request.issued_at,
            not_before=request.not_before,
            expires_at=request.expires_at,
            maximum_clock_skew_seconds=request.maximum_clock_skew_seconds,
            nonce=request.nonce,
            revocation_epoch=request.revocation_epoch,
            issuer_id=request.issuer_id,
            issuer_key_id=self._signer.issuer_key_id,
            signature_algorithm=self._signer.algorithm,
            signature="unsigned",
            status=ApprovalStatus.ISSUED,
            store_version=1,
            delegation_id=request.delegation_id,
        )
        return ApprovalGrant(
            **_grant_fields(unsigned),
            signature=self._signer.sign(approval_signature_payload(unsigned)),
        )

    def verify(self, grant: ApprovalGrant) -> bool:
        if grant.signature_algorithm != self._signer.algorithm:
            return False
        if grant.issuer_key_id != self._signer.issuer_key_id:
            return False
        return self._signer.verify(approval_signature_payload(grant), grant.signature)

    def consume(
        self,
        grant: ApprovalGrant,
        *,
        now: str,
        current_revocation_epoch: int,
        actor_id: str,
        attempt_id: str,
    ) -> ApprovalGrant:
        if not self.verify(grant):
            raise HarnessError(
                ErrorCode.APPROVAL_ISSUER_UNTRUSTED, "approval grant signature is invalid"
            )
        return grant.consume(
            now=now,
            expected_execution_plan_hash=grant.execution_plan_hash,
            current_revocation_epoch=current_revocation_epoch,
            actor_id=actor_id,
            attempt_id=attempt_id,
            signature_valid=True,
        )


def approval_signature_payload(grant: ApprovalGrant) -> bytes:
    """一回消費前のAuthority・主体・時刻・Nonceを署名対象へ束縛する。"""
    return canonicalize(
        {
            "grant_id": grant.grant_id,
            "plan_content_hash": str(grant.plan_content_hash),
            "execution_plan_hash": str(grant.execution_plan_hash),
            "action_scope": sorted(grant.action_scope),
            "approver_subject_id": grant.approver_subject_id,
            "approver_tenant_id": grant.approver_tenant_id,
            "authentication_context_class": grant.authentication_context_class,
            "mfa_performed": grant.mfa_performed,
            "authentication_time": grant.authentication_time,
            "issued_at": grant.issued_at,
            "not_before": grant.not_before,
            "expires_at": grant.expires_at,
            "maximum_clock_skew_seconds": grant.maximum_clock_skew_seconds,
            "nonce": grant.nonce,
            "revocation_epoch": grant.revocation_epoch,
            "issuer_id": grant.issuer_id,
            "issuer_key_id": grant.issuer_key_id,
            "signature_algorithm": grant.signature_algorithm,
            "delegation_id": grant.delegation_id,
        }
    )


def _grant_fields(grant: ApprovalGrant) -> dict[str, Any]:
    return {
        "grant_id": grant.grant_id,
        "plan_content_hash": grant.plan_content_hash,
        "execution_plan_hash": grant.execution_plan_hash,
        "action_scope": grant.action_scope,
        "approver_subject_id": grant.approver_subject_id,
        "approver_tenant_id": grant.approver_tenant_id,
        "authentication_context_class": grant.authentication_context_class,
        "mfa_performed": grant.mfa_performed,
        "authentication_time": grant.authentication_time,
        "issued_at": grant.issued_at,
        "not_before": grant.not_before,
        "expires_at": grant.expires_at,
        "maximum_clock_skew_seconds": grant.maximum_clock_skew_seconds,
        "nonce": grant.nonce,
        "revocation_epoch": grant.revocation_epoch,
        "issuer_id": grant.issuer_id,
        "issuer_key_id": grant.issuer_key_id,
        "signature_algorithm": grant.signature_algorithm,
        "status": grant.status,
        "store_version": grant.store_version,
        "consumed_at": grant.consumed_at,
        "consumed_by_actor_id": grant.consumed_by_actor_id,
        "attempt_id": grant.attempt_id,
        "revoked_at": grant.revoked_at,
        "revoked_by_actor_id": grant.revoked_by_actor_id,
        "revocation_reason": grant.revocation_reason,
        "invalidated_at": grant.invalidated_at,
        "invalidation_reason": grant.invalidation_reason,
        "delegation_id": grant.delegation_id,
    }
