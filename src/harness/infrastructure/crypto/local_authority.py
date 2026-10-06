"""Local OS session approval, with ephemeral signing keys and persisted public verification."""

from __future__ import annotations

import os
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from harness.application.approval_service import (
    ApprovalIssueRequest,
    ApprovalService,
    approval_signature_payload,
)
from harness.domain.approval import ApprovalGrant
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash
from harness.infrastructure.crypto.ed25519_signer import Ed25519Signer, Ed25519Verifier


class LocalAuthority:
    def subject(self, auth_session: str) -> str:
        expected = "local-uid:" + str(os.getuid())
        if auth_session != expected:
            raise HarnessError(
                ErrorCode.APPROVAL_ISSUER_UNTRUSTED,
                "auth session does not identify current OS user",
            )
        return expected

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
        signer = Ed25519Signer.generate("local-key:" + str(uuid.uuid4()))
        grant = ApprovalService(signer).issue(
            ApprovalIssueRequest(
                grant_id=run_id + ":approval",
                plan_content_hash=plan_content_hash,
                execution_plan_hash=execution_plan_hash,
                action_scope=scope,
                approver_subject_id=subject,
                approver_tenant_id="local-single-user",
                authentication_context_class="LOCAL_OS_SESSION",
                mfa_performed=False,
                authentication_time=now,
                issued_at=now,
                not_before=now,
                expires_at=expires_at,
                maximum_clock_skew_seconds=maximum_clock_skew_seconds,
                nonce=str(uuid.uuid4()),
                revocation_epoch=0,
                issuer_id="local-offline-approval",
            )
        )
        return grant, signer.verifier().public_key_bytes().hex()

    def verify(self, grant: ApprovalGrant, public_key: str) -> bool:
        try:
            verifier = Ed25519Verifier(
                Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key)), grant.issuer_key_id
            )
            return grant.signature_algorithm == "Ed25519" and verifier.verify(
                approval_signature_payload(grant), grant.signature
            )
        except ValueError:
            return False
