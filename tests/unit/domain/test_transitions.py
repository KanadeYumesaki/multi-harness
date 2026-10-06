"""§1.4.1／§1.4.2／§1.4.3／§1.5 の遷移と順序の試験。"""

from __future__ import annotations

import pytest

from harness.domain.errors import ErrorClassification, ErrorCode, HarnessError
from harness.domain.events import EventType
from harness.domain.states import STATE_NAMESPACES, StateNamespace
from harness.domain.transitions import (
    ACTION_ATTEMPT_TERMINAL_STATES,
    RUN_TERMINAL_STATES,
    PolicyOutcome,
    TransitionContext,
    is_retryable_classification,
    is_terminal_action_attempt_state,
    resolve_next_state,
    validate_event_order,
    validate_run_terminal_transition,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# §1.4.2 決定的な遷移
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "event", "expected"),
    [
        ("PLANNING", EventType.PLAN_RESOLVED, "WAITING_POLICY"),
        ("WAITING_POLICY", EventType.PLAN_NONDETERMINISTIC, "BLOCKED_CONFLICT"),
        ("WAITING_APPROVAL", EventType.APPROVAL_CONSUMED, "READY"),
        ("READY", EventType.ACTION_CLAIMED, "CLAIMED"),
        ("CLAIMED", EventType.LEASE_ACQUIRED, "LEASED"),
        ("LEASED", EventType.RUNTIME_ATTESTED, "RUNTIME_VERIFIED"),
        ("RUNTIME_VERIFIED", EventType.ACTION_STARTED, "RUNNING"),
        ("RUNNING", EventType.ACTION_PREPARED, "PREPARED_DURABLE"),
        ("PREPARED_DURABLE", EventType.EXECUTION_ATTEMPTED, "EFFECT_IN_FLIGHT"),
        ("EFFECT_IN_FLIGHT", EventType.EFFECT_OBSERVED, "EFFECT_VERIFIED"),
        ("EFFECT_VERIFIED", EventType.EFFECT_RECEIPT_STORED, "RECEIPT_DURABLE"),
        ("RECEIPT_DURABLE", EventType.ACTION_COMMITTED, "SUCCEEDED"),
        ("RUNNING", EventType.CANCEL_CONFIRMED, "CANCELLED"),
        ("EFFECT_IN_FLIGHT", EventType.EFFECT_UNKNOWN, "EFFECT_UNKNOWN"),
    ],
)
def test_deterministic_transitions(current: str, event: EventType, expected: str) -> None:
    assert resolve_next_state(current, event) == expected


@pytest.mark.parametrize(
    "event",
    [
        EventType.REMOTE_INVOCATION_UNCERTAIN,
        EventType.OUTBOX_STATUS_UNKNOWN,
        EventType.BUDGET_STATUS_UNKNOWN,
    ],
)
def test_uncertainty_events_map_to_effect_unknown(event: EventType) -> None:
    """§1.14.1「最終的に共通`EFFECT_UNKNOWN`へ写像する」。"""
    assert resolve_next_state("EFFECT_IN_FLIGHT", event) == "EFFECT_UNKNOWN"


def test_every_resolved_state_is_registered() -> None:
    """遷移先が全てstates.yamlのACTION_ATTEMPT名前空間に存在する。"""
    valid = set(STATE_NAMESPACES[StateNamespace.ACTION_ATTEMPT])
    assert ACTION_ATTEMPT_TERMINAL_STATES <= valid


# --------------------------------------------------------------------------
# §1.4.2 文脈が要る遷移
# --------------------------------------------------------------------------


def test_policy_decided_allow_branches_on_approval_requirement() -> None:
    allow_needs_approval = TransitionContext(
        policy_outcome=PolicyOutcome.ALLOW, approval_required=True
    )
    allow_no_approval = TransitionContext(
        policy_outcome=PolicyOutcome.ALLOW, approval_required=False
    )
    assert (
        resolve_next_state("WAITING_POLICY", EventType.POLICY_DECIDED, allow_needs_approval)
        == "WAITING_APPROVAL"
    )
    assert (
        resolve_next_state("WAITING_POLICY", EventType.POLICY_DECIDED, allow_no_approval) == "READY"
    )


def test_policy_decided_deny_is_blocked_policy() -> None:
    context = TransitionContext(policy_outcome=PolicyOutcome.DENY)
    assert (
        resolve_next_state("WAITING_POLICY", EventType.POLICY_DECIDED, context) == "BLOCKED_POLICY"
    )


def test_policy_decided_without_outcome_stops() -> None:
    """文脈不足を推測で埋めない（不変条件#9）。"""
    with pytest.raises(HarnessError, match="policy_outcome"):
        resolve_next_state("WAITING_POLICY", EventType.POLICY_DECIDED)


def test_policy_allow_without_approval_flag_stops() -> None:
    context = TransitionContext(policy_outcome=PolicyOutcome.ALLOW)
    with pytest.raises(HarnessError, match="approval_required"):
        resolve_next_state("WAITING_POLICY", EventType.POLICY_DECIDED, context)


@pytest.mark.parametrize("classification", list(ErrorClassification))
def test_action_failed_branches_on_classification(
    classification: ErrorClassification,
) -> None:
    """§1.7のRetry規則に従い`FAILED_RETRYABLE`／`FAILED_PERMANENT`を決める。

    登録済み全分類について、判定が`is_retryable_classification`と一致すること。
    """
    context = TransitionContext(error_classification=classification)
    expected = (
        "FAILED_RETRYABLE" if is_retryable_classification(classification) else "FAILED_PERMANENT"
    )
    assert resolve_next_state("RUNNING", EventType.ACTION_FAILED, context) == expected


def test_registry_currently_has_no_retryable_classification() -> None:
    """Registryの実態を固定する。

    §1.7は`RATE_LIMITED`／`TRANSIENT_PROVIDER_ERROR`／`TIMEOUT`を条件付き再試行と
    定めるが、errors.yamlにこれらのError Codeが1件も登録されていないため、
    生成される`ErrorClassification`にも現れない。
    結果として現時点の`ACTION_FAILED`は必ず`FAILED_PERMANENT`になる。

    MVP0-C以降でProvider由来のCodeが登録された時点でこの試験が落ちる。
    そのとき初めてRetry経路が有効になったことを意味するので、
    期待値をこの試験ごと更新する。
    """
    retryable = [c for c in ErrorClassification if is_retryable_classification(c)]
    assert retryable == [], (
        f"retryable classification が登録された: {[c.name for c in retryable]}。"
        "Retry経路が有効になったため、§1.7の上限・待機規則の実装が必要"
    )


def test_permanent_failure_for_a_registered_classification() -> None:
    context = TransitionContext(error_classification=ErrorClassification.VALIDATION_ERROR)
    assert resolve_next_state("RUNNING", EventType.ACTION_FAILED, context) == "FAILED_PERMANENT"


def test_action_failed_without_classification_stops() -> None:
    with pytest.raises(HarnessError, match="error_classification"):
        resolve_next_state("RUNNING", EventType.ACTION_FAILED)


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (ErrorCode.POLICY_STALE_NEW_ACTION_BLOCKED, "BLOCKED_POLICY"),
        (ErrorCode.APPROVAL_REPLAY, "BLOCKED_APPROVAL"),
        (ErrorCode.APPROVAL_SUBJECT_MISMATCH, "BLOCKED_APPROVAL"),
        (ErrorCode.STALE_FENCING_TOKEN, "BLOCKED_CONFLICT"),
        (ErrorCode.DELEGATION_REVOKED_MID_FLIGHT, "BLOCKED_CONFLICT"),
        (ErrorCode.EFFECT_UNKNOWN, "EFFECT_UNKNOWN"),
    ],
)
def test_action_blocked_branches_on_reason_code(reason: ErrorCode, expected: str) -> None:
    """§1.4.2「`ACTION_BLOCKED` は理由Codeを必須化」。"""
    context = TransitionContext(blocked_reason=reason)
    assert resolve_next_state("RUNNING", EventType.ACTION_BLOCKED, context) == expected


def test_action_blocked_without_reason_stops() -> None:
    with pytest.raises(HarnessError, match="blocked_reason"):
        resolve_next_state("RUNNING", EventType.ACTION_BLOCKED)


# --------------------------------------------------------------------------
# 未知Event と State維持Event の区別
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event",
    [
        EventType.RUNTIME_SPEC_RESOLVED,
        EventType.INVOCATION_MANIFEST_RESOLVED,
        EventType.CAPABILITY_SNAPSHOT_CAPTURED,
        EventType.APPROVAL_ISSUED,
        EventType.RECOVERY_STARTED,
        EventType.DELEGATION_MATCHED,
        EventType.INPUT_MASKING_COMPLETED,
    ],
)
def test_state_preserving_events_keep_current_state(event: EventType) -> None:
    """§1.14.1「StateがLedger Eventで変わらない場合はStateを維持する」。"""
    assert resolve_next_state("RUNNING", event) == "RUNNING"


def test_event_without_defined_transition_is_rejected() -> None:
    """State維持Eventと未知Eventを同一視しない。

    遷移も維持も宣言されていないEventは、表への追加を求めて停止する。
    黙って現在Stateを返すと、定義漏れが検出できない。
    """
    with pytest.raises(HarnessError, match="no defined ActionAttempt transition"):
        resolve_next_state("RUNNING", EventType.BUDGET_RESERVED)


def test_unknown_current_state_is_rejected() -> None:
    with pytest.raises(HarnessError, match="unknown ActionAttempt state"):
        resolve_next_state("NOT_A_STATE", EventType.ACTION_STARTED)


# --------------------------------------------------------------------------
# §1.4.3 終端状態
# --------------------------------------------------------------------------


@pytest.mark.parametrize("state", sorted(ACTION_ATTEMPT_TERMINAL_STATES))
def test_terminal_states_reject_further_transitions(state: str) -> None:
    """§1.4.3「終端状態から同じAttemptを再開しない」。"""
    assert is_terminal_action_attempt_state(state)
    with pytest.raises(HarnessError, match="terminal state"):
        resolve_next_state(state, EventType.ACTION_STARTED)


def test_non_terminal_states_are_not_terminal() -> None:
    for state in ("PLANNING", "RUNNING", "PREPARED_DURABLE", "EFFECT_IN_FLIGHT"):
        assert not is_terminal_action_attempt_state(state)


# --------------------------------------------------------------------------
# §1.4.1 規範イベント順序
# --------------------------------------------------------------------------


def test_action_prepared_requires_action_started() -> None:
    """`AT-EVENT-ORDER-001/ACTION_STARTED_MISSING`。"""
    with pytest.raises(HarnessError, match="ACTION_STARTED"):
        validate_event_order(
            EventType.ACTION_PREPARED,
            [EventType.LEASE_ACQUIRED, EventType.RUNTIME_ATTESTED],
        )


def test_full_normative_sequence_is_accepted() -> None:
    """§1.4.1の規範順序を通しで流して1件も拒否されない。"""
    sequence = [
        EventType.INTENT_CREATED,
        EventType.CAPABILITY_SNAPSHOT_CAPTURED,
        EventType.RUNTIME_SPEC_RESOLVED,
        EventType.INVOCATION_MANIFEST_RESOLVED,
        EventType.PLAN_RESOLVED,
        EventType.POLICY_DECIDED,
        EventType.APPROVAL_ISSUED,
        EventType.APPROVAL_CONSUMED,
        EventType.ACTION_CLAIMED,
        EventType.LEASE_ACQUIRED,
        EventType.RUNTIME_ATTESTED,
        EventType.ACTION_STARTED,
        EventType.ACTION_PREPARED,
        EventType.EXECUTION_ATTEMPTED,
        EventType.EFFECT_OBSERVED,
        EventType.EFFECT_RECEIPT_STORED,
        EventType.ACTION_COMMITTED,
        EventType.EVALUATION_COMPLETED,
        EventType.RELEASE_DECIDED,
    ]
    seen: list[EventType] = []
    for event in sequence:
        validate_event_order(event, seen)
        seen.append(event)


@pytest.mark.parametrize(
    ("event", "missing"),
    [
        (EventType.EXECUTION_ATTEMPTED, "ACTION_PREPARED"),
        (EventType.EFFECT_OBSERVED, "EXECUTION_ATTEMPTED"),
        (EventType.EFFECT_RECEIPT_STORED, "EFFECT_OBSERVED"),
        (EventType.APPROVAL_CONSUMED, "APPROVAL_ISSUED"),
        (EventType.RELEASE_DECIDED, "EVALUATION_COMPLETED"),
        (EventType.LEASE_ACQUIRED, "ACTION_CLAIMED"),
    ],
)
def test_missing_predecessor_is_rejected(event: EventType, missing: str) -> None:
    with pytest.raises(HarnessError, match=missing):
        validate_event_order(event, [EventType.INTENT_CREATED])


def test_events_without_predecessor_requirement_pass() -> None:
    validate_event_order(EventType.INTENT_CREATED, [])
    validate_event_order(EventType.RECOVERY_STARTED, [])


# --------------------------------------------------------------------------
# §1.5 Run不変条件
# --------------------------------------------------------------------------


def test_completed_requires_release_decision() -> None:
    with pytest.raises(HarnessError) as excinfo:
        validate_run_terminal_transition(
            "COMPLETED",
            unresolved_action_count=0,
            has_unreconciled_effect=False,
            release_decided=False,
        )
    assert excinfo.value.code is ErrorCode.RELEASE_DECISION_REQUIRED


@pytest.mark.parametrize("target", sorted(RUN_TERMINAL_STATES))
def test_unreconciled_effect_blocks_every_terminal_state(target: str) -> None:
    """§1.5「未照合Effectを持つRunは終端のいずれへも遷移しない」。"""
    with pytest.raises(HarnessError) as excinfo:
        validate_run_terminal_transition(
            target,
            unresolved_action_count=0,
            has_unreconciled_effect=True,
            release_decided=True,
        )
    assert excinfo.value.code is ErrorCode.UNRECONCILED_EFFECT_PRESENT


def test_unresolved_actions_block_terminal_state() -> None:
    with pytest.raises(HarnessError, match="unresolved action"):
        validate_run_terminal_transition(
            "FAILED",
            unresolved_action_count=2,
            has_unreconciled_effect=False,
            release_decided=True,
        )


def test_blocked_repair_required_is_not_terminal() -> None:
    """§1.5「`BLOCKED`は終端、`BLOCKED_REPAIR_REQUIRED`は非終端」。

    未照合Effectがあっても`BLOCKED_REPAIR_REQUIRED`へは遷移できる。
    """
    validate_run_terminal_transition(
        "BLOCKED_REPAIR_REQUIRED",
        unresolved_action_count=3,
        has_unreconciled_effect=True,
        release_decided=False,
    )


def test_clean_completed_transition_is_allowed() -> None:
    validate_run_terminal_transition(
        "COMPLETED",
        unresolved_action_count=0,
        has_unreconciled_effect=False,
        release_decided=True,
    )


def test_unknown_run_state_is_rejected() -> None:
    with pytest.raises(HarnessError, match="unknown Run state"):
        validate_run_terminal_transition(
            "NOPE",
            unresolved_action_count=0,
            has_unreconciled_effect=False,
            release_decided=True,
        )
