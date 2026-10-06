"""## Case Adapter はここではない

本 File が持っていた `@pytest.mark.case` は
`tests/integration/sqlite/test_approval_plan_orchestrator.py` へ移した。
Case は Event 列まで要求するが、本 File は Ledger を観測しない。
Ledger を観測しない試験を Adapter にすると「State だけ一致した Case」になる。

本 File の試験は消していない。Domain／Repository の振る舞いは引き続き検証する。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from case_probe import observe_unit_case

from harness.domain.approval import ApprovalGrant, ApprovalStatus
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash, hash_canonical
from harness.domain.lease import Lease, LeaseStatus, require_current_fencing_token
from harness.domain.plan import PlanAction, PlanAuthority, PlanBuildInput, build_execution_plan

pytestmark = pytest.mark.unit


def _hash(label: str) -> ContentHash:
    return hash_canonical({"label": label}, artifact_type="test-value", schema_major=1)


def _input(*actions: PlanAction) -> PlanBuildInput:
    return PlanBuildInput(
        intent_hash=_hash("intent"),
        workspace_snapshot_hash=_hash("workspace"),
        input_read_evidence_hash=_hash("read"),
        context_bundle_hash=_hash("context"),
        policy_snapshot_hash=_hash("policy"),
        token_profile_hash=_hash("tokens"),
        schema_set_hash=_hash("schemas"),
        capability_snapshot_hash=_hash("capabilities"),
        entitlement_snapshot_hash=None,
        normalized_actions=actions,
        runtime_envelope_spec_hash=_hash("runtime"),
        invocation_manifest_hash=_hash("invocation"),
        token_budget_policy_hash=_hash("budget"),
        control_data_policy_hash=_hash("control-data"),
        planner_algorithm_version="planner/1.0",
    )


def _authority(*, run_id: str = "run-1", issued_at: str = "2026-08-15T00:00:00Z") -> PlanAuthority:
    return PlanAuthority(
        run_id=run_id,
        execution_plan_id=f"plan-{run_id}",
        plan_version=1,
        issued_at=issued_at,
        expires_at="2026-08-15T00:10:00Z",
        planner_identity="harness-planner/1.8.0",
        authority_scope="ACTION_EXECUTION",
    )


@pytest.mark.case("AT-PLAN-DETERMINISM-001/SAME_INPUT")
@pytest.mark.unit_subject("PLAN_COMPARISON", durability="NOT_APPLICABLE")
def test_plan_content_hash_is_order_independent_but_authority_hash_is_run_specific(
    case_observation: Any,
) -> None:
    task = PlanAction("TASK_LOAD", {"path": "task.md"}, {"artifact": "task"})
    build = PlanAction(
        "CONTEXT_BUILD", {"from": "task"}, {"bundle": "context"}, (task.semantic_key,)
    )

    run_a, run_b = "run-1", "run-2"
    first = build_execution_plan(_input(task, build), _authority(run_id=run_a))
    second = build_execution_plan(_input(build, task), _authority(run_id=run_b))

    assert first.plan_content_hash == second.plan_content_hash
    assert first.execution_plan_hash != second.execution_plan_hash
    assert first.action_graph["nodes"] == second.action_graph["nodes"]
    assert run_a != run_b

    # Planner の比較結果である。**実行を主張しない**ので Ledger を観測しない
    # （設計書 §1.16.2.2／§19.1.1、Owner Decision 2-B）。
    observe_unit_case(
        case_observation,
        "AT-PLAN-DETERMINISM-001/SAME_INPUT",
        state="ASSERTIONS_SATISFIED",
        subject_id=f"plan-comparison:{run_a}:{run_b}",
        error_code=None,
        payload={
            "plan_content_hash_a": str(first.plan_content_hash),
            "plan_content_hash_b": str(second.plan_content_hash),
            "execution_plan_hash_a": str(first.execution_plan_hash),
            "execution_plan_hash_b": str(second.execution_plan_hash),
            "run_id_a": run_a,
            "run_id_b": run_b,
        },
    )


@pytest.mark.case("AT-PLAN-001/TAMPER")
def test_plan_integrity_rejects_modified_content_or_authority_hash() -> None:
    action = PlanAction("TASK_LOAD", {"path": "task.md"}, {"artifact": "task"})
    plan = build_execution_plan(_input(action), _authority())

    with pytest.raises(HarnessError) as content_tamper:
        replace(plan, plan_content_hash=_hash("changed-content")).assert_integrity()
    assert content_tamper.value.code is ErrorCode.PLAN_NONDETERMINISTIC

    with pytest.raises(HarnessError) as authority_tamper:
        replace(plan, execution_plan_hash=_hash("changed-authority")).assert_integrity()
    assert authority_tamper.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH


@pytest.mark.case("AT-PLAN-DETERMINISM-001/NONDETERMINISTIC_OUTPUT")
def test_plan_rejects_unknown_or_cyclic_dependencies() -> None:
    unknown = PlanAction("TASK_LOAD", {}, {}, ("action:sha256:" + "1" * 64,))
    with pytest.raises(HarnessError) as unknown_error:
        build_execution_plan(_input(unknown), _authority())
    assert unknown_error.value.code is ErrorCode.PLAN_NONDETERMINISTIC

    first = PlanAction("TASK_LOAD", {}, {})
    second = PlanAction("CONTEXT_BUILD", {}, {}, (first.semantic_key,))
    cyclic_first = PlanAction("TASK_LOAD", {}, {}, (second.semantic_key,))
    with pytest.raises(HarnessError) as cyclic_error:
        build_execution_plan(_input(cyclic_first, second), _authority())
    assert cyclic_error.value.code is ErrorCode.PLAN_NONDETERMINISTIC


def _grant(*, status: ApprovalStatus = ApprovalStatus.ISSUED) -> ApprovalGrant:
    return ApprovalGrant(
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
        revocation_epoch=3,
        issuer_id="approval-service:local",
        issuer_key_id="key-1",
        signature_algorithm="ed25519",
        signature="signature-placeholder",
        status=status,
        store_version=1,
    )


@pytest.mark.case("AT-APPROVAL-001/REPLAY")
def test_approval_consumption_binds_exact_execution_hash_and_is_single_use() -> None:
    grant = _grant()
    consumed = grant.consume(
        now="2026-08-15T00:01:00Z",
        expected_execution_plan_hash=grant.execution_plan_hash,
        current_revocation_epoch=3,
        actor_id="worker-1",
        attempt_id="attempt-1",
        signature_valid=True,
    )

    assert consumed.status is ApprovalStatus.CONSUMED
    assert consumed.attempt_id == "attempt-1"
    with pytest.raises(HarnessError) as error:
        consumed.consume(
            now="2026-08-15T00:02:00Z",
            expected_execution_plan_hash=grant.execution_plan_hash,
            current_revocation_epoch=3,
            actor_id="worker-2",
            attempt_id="attempt-2",
            signature_valid=True,
        )
    assert error.value.code is ErrorCode.APPROVAL_REPLAY


def test_approval_rejects_changed_authority_or_expired_clock_window() -> None:
    grant = _grant()
    with pytest.raises(HarnessError) as mismatched:
        grant.consume(
            now="2026-08-15T00:01:00Z",
            expected_execution_plan_hash=_hash("different"),
            current_revocation_epoch=3,
            actor_id="worker-1",
            attempt_id="attempt-1",
            signature_valid=True,
        )
    assert mismatched.value.code is ErrorCode.APPROVAL_INVALIDATED

    with pytest.raises(HarnessError) as expired:
        grant.consume(
            now="2026-08-15T00:10:31Z",
            expected_execution_plan_hash=grant.execution_plan_hash,
            current_revocation_epoch=3,
            actor_id="worker-1",
            attempt_id="attempt-1",
            signature_valid=True,
        )
    assert expired.value.code is ErrorCode.CLOCK_SKEW_EXCEEDED


def test_lease_rejects_stale_fencing_token_and_expired_lease() -> None:
    lease = Lease(
        lease_id="lease-1",
        resource_key="workspace:demo",
        holder_id="worker-1",
        attempt_id="attempt-1",
        fencing_token=7,
        issued_at="2026-08-15T00:00:00Z",
        expires_at="2026-08-15T00:01:00Z",
        renewed_at="2026-08-15T00:00:00Z",
        status=LeaseStatus.ACTIVE,
        store_version=1,
    )

    require_current_fencing_token(lease, current_token=7, now="2026-08-15T00:00:30Z")
    with pytest.raises(HarnessError) as stale:
        require_current_fencing_token(lease, current_token=8, now="2026-08-15T00:00:30Z")
    assert stale.value.code is ErrorCode.STALE_FENCING_TOKEN

    with pytest.raises(HarnessError) as expired:
        require_current_fencing_token(lease, current_token=7, now="2026-08-15T00:02:00Z")
    assert expired.value.code is ErrorCode.STALE_FENCING_TOKEN
