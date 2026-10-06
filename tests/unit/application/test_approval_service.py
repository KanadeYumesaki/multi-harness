from __future__ import annotations

from dataclasses import replace

import pytest

from harness.application.approval_service import ApprovalIssueRequest, ApprovalService
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_canonical
from harness.infrastructure.crypto.ed25519_signer import Ed25519Signer

pytestmark = pytest.mark.unit


def _hash(label: str):
    return hash_canonical({"label": label}, artifact_type="test-value", schema_major=1)


def _request() -> ApprovalIssueRequest:
    return ApprovalIssueRequest(
        grant_id="grant-1",
        plan_content_hash=_hash("content"),
        execution_plan_hash=_hash("execution"),
        action_scope=("action-1",),
        approver_subject_id="operator:local",
        approver_tenant_id="tenant:local",
        authentication_context_class="urn:harness:os-login",
        mfa_performed=False,
        authentication_time="2026-08-15T00:00:00Z",
        issued_at="2026-08-15T00:00:00Z",
        not_before="2026-08-15T00:00:00Z",
        expires_at="2026-08-15T00:10:00Z",
        maximum_clock_skew_seconds=30,
        nonce="0123456789abcdef",
        revocation_epoch=1,
        issuer_id="approval-service:local",
    )


def test_approval_service_issues_and_verifies_ed25519_grant() -> None:
    signer = Ed25519Signer.generate("key-1")
    service = ApprovalService(signer)
    grant = service.issue(_request())

    assert grant.signature_algorithm == "Ed25519"
    assert service.verify(grant)
    consumed = service.consume(
        grant,
        now="2026-08-15T00:01:00Z",
        current_revocation_epoch=1,
        actor_id="worker-1",
        attempt_id="attempt-1",
    )
    assert consumed.attempt_id == "attempt-1"


def test_approval_service_rejects_tampered_plan_authority() -> None:
    service = ApprovalService(Ed25519Signer.generate("key-1"))
    grant = service.issue(_request())
    tampered = replace(grant, execution_plan_hash=_hash("changed"))

    assert not service.verify(tampered)
    with pytest.raises(HarnessError) as error:
        service.consume(
            tampered,
            now="2026-08-15T00:01:00Z",
            current_revocation_epoch=1,
            actor_id="worker-1",
            attempt_id="attempt-1",
        )
    assert error.value.code is ErrorCode.APPROVAL_ISSUER_UNTRUSTED
