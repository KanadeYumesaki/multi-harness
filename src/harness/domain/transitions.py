"""§1.4.2 EventからStateへの正本遷移、および§1.4.1 規範イベント順序。

Domain層の純粋関数だけで構成する。Storage実装から独立して試験できる。

## 遷移が単純な写像にならない理由

§1.4.2の表は Event → State だが、次の3つは Event だけでは State が決まらない。

| Event | 追加で必要な文脈 |
|---|---|
| `POLICY_DECIDED` | ALLOW／DENY と Approval要否 |
| `ACTION_FAILED` | Error Classification |
| `ACTION_BLOCKED` | 理由Code |

`Mapping[EventType, str]` にすると、この3つを誤った単一値へ潰すか取りこぼす。
そのため文脈を受け取る関数とし、文脈が欠けている場合は推測せず停止する
（不変条件#9「例外を握り潰さない。判定不能は停止する」）。

## 「未知のEvent」と「Stateを変えないEvent」の区別

§1.14.1は「Ledger EventがActionAttempt Stateを変更しない場合は、Stateを維持し
Phase Projectionだけを更新する」と定める。両者を同一視すると、
本来拒否すべき未知Eventが「状態維持」として通る。明示的に分ける。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Final

from harness.domain.errors import (
    ErrorClassification,
    ErrorCode,
    HarnessError,
    classification_of,
)
from harness.domain.events import EventType
from harness.domain.states import StateNamespace, is_valid_state

__all__ = [
    "ACTION_ATTEMPT_TERMINAL_STATES",
    "REQUIRED_PREDECESSORS",
    "RUN_NON_TERMINAL_BLOCKED_STATES",
    "RUN_TERMINAL_STATES",
    "PolicyOutcome",
    "TransitionContext",
    "is_retryable_classification",
    "is_terminal_action_attempt_state",
    "resolve_next_state",
    "validate_event_order",
    "validate_run_terminal_transition",
]


class PolicyOutcome(Enum):
    """§1.4.2 `POLICY_DECIDED(ALLOW)` / `POLICY_DECIDED(DENY)`。"""

    ALLOW = "ALLOW"
    DENY = "DENY"


@dataclass(frozen=True, slots=True)
class TransitionContext:
    """Eventだけでは決まらない遷移のための文脈。

    必要な項目が`None`のまま該当Eventを解決しようとすると停止する。
    """

    policy_outcome: PolicyOutcome | None = None
    approval_required: bool | None = None
    error_classification: ErrorClassification | None = None
    blocked_reason: ErrorCode | None = None


# §1.4.3 ActionAttemptの終端状態。ここから同じAttemptを再開しない。
ACTION_ATTEMPT_TERMINAL_STATES: Final[frozenset[str]] = frozenset(
    {
        "SUCCEEDED",
        "FAILED_RETRYABLE",
        "FAILED_PERMANENT",
        "BLOCKED_POLICY",
        "BLOCKED_APPROVAL",
        "BLOCKED_CONFLICT",
        "CANCELLED",
        "CANCEL_UNKNOWN",
        "EFFECT_UNKNOWN",
    }
)

# §1.5 Run状態。`BLOCKED`は終端、`BLOCKED_REPAIR_REQUIRED`は非終端として分離する。
RUN_TERMINAL_STATES: Final[frozenset[str]] = frozenset(
    {"COMPLETED", "CANCELLED", "FAILED", "BLOCKED"}
)
RUN_NON_TERMINAL_BLOCKED_STATES: Final[frozenset[str]] = frozenset({"BLOCKED_REPAIR_REQUIRED"})

# Eventだけで State が一意に決まるもの（§1.4.2）。
_DETERMINISTIC_TRANSITIONS: Final[Mapping[EventType, str]] = MappingProxyType(
    {
        EventType.INTENT_CREATED: "PLANNING",
        EventType.PLAN_RESOLVED: "WAITING_POLICY",
        EventType.PLAN_NONDETERMINISTIC: "BLOCKED_CONFLICT",
        EventType.APPROVAL_CONSUMED: "READY",
        EventType.ACTION_CLAIMED: "CLAIMED",
        EventType.LEASE_ACQUIRED: "LEASED",
        EventType.RUNTIME_ATTESTED: "RUNTIME_VERIFIED",
        EventType.ACTION_STARTED: "RUNNING",
        EventType.ACTION_PREPARED: "PREPARED_DURABLE",
        EventType.EXECUTION_ATTEMPTED: "EFFECT_IN_FLIGHT",
        EventType.EFFECT_OBSERVED: "EFFECT_VERIFIED",
        EventType.EFFECT_RECEIPT_STORED: "RECEIPT_DURABLE",
        EventType.ACTION_COMMITTED: "SUCCEEDED",
        EventType.CANCEL_CONFIRMED: "CANCELLED",
        EventType.CANCEL_UNKNOWN: "CANCEL_UNKNOWN",
        EventType.EFFECT_UNKNOWN: "EFFECT_UNKNOWN",
        # §1.14.1 正規写像。いずれも最終的に共通 EFFECT_UNKNOWN へ写像する。
        EventType.REMOTE_INVOCATION_UNCERTAIN: "EFFECT_UNKNOWN",
        EventType.OUTBOX_STATUS_UNKNOWN: "EFFECT_UNKNOWN",
        EventType.BUDGET_STATUS_UNKNOWN: "EFFECT_UNKNOWN",
    }
)

# ActionAttempt Stateを変更しないEvent（§1.14.1）。Phase Projectionだけを更新する。
# 「未知のEvent」と区別するため明示列挙する。
_STATE_PRESERVING_EVENTS: Final[frozenset[EventType]] = frozenset(
    {
        EventType.RUN_CREATED,
        EventType.CAPABILITY_SNAPSHOT_CAPTURED,
        EventType.RUNTIME_SPEC_RESOLVED,
        EventType.INVOCATION_MANIFEST_RESOLVED,
        EventType.APPROVAL_ISSUED,
        EventType.APPROVAL_REPLAY_DENIED,
        EventType.RUNTIME_SPEC_MISMATCH,
        EventType.INPUT_READ_CAPABILITY_ISSUED,
        EventType.INPUT_READ_STARTED,
        EventType.INPUT_READ_DENIED,
        EventType.INPUT_ARTIFACT_CLASSIFIED,
        EventType.INPUT_MASKING_STARTED,
        EventType.INPUT_MASKING_SCAN1_CANDIDATES_READY,
        EventType.INPUT_MASKING_SPANS_PROPOSED,
        EventType.INPUT_MASKING_COMPLETED,
        EventType.INPUT_MASKING_REJECTED,
        EventType.DELEGATION_CREATED,
        EventType.DELEGATION_MATCHED,
        EventType.DELEGATION_REJECTED,
        EventType.DELEGATION_NARROWED,
        EventType.DELEGATION_REVOKED,
        EventType.DELEGATION_EXPIRED,
        EventType.DELEGATION_INVALIDATED,
        EventType.DELEGATION_REVALIDATED,
        EventType.DELEGATION_EFFECT_LINEARIZED,
        EventType.DELEGATION_REVOKED_AFTER_EFFECT_START,
        EventType.RECOVERY_STARTED,
        EventType.RECOVERY_DECIDED,
        EventType.EVALUATION_COMPLETED,
        EventType.RELEASE_DECIDED,
        EventType.CANCEL_REQUESTED,
        EventType.FENCING_REJECTED,
        EventType.POLICY_STALE_DETECTED,
        EventType.EFFECT_CONFLICT_DETECTED,
        EventType.MANUAL_RECONCILIATION_ENQUEUED,
    }
)

# §1.4.1 規範イベント順序を「必須の先行Event」で表す。
# Append時にStream内の既存Eventを見て検査する。
REQUIRED_PREDECESSORS: Final[Mapping[EventType, tuple[EventType, ...]]] = MappingProxyType(
    {
        EventType.PLAN_RESOLVED: (EventType.INTENT_CREATED,),
        EventType.POLICY_DECIDED: (EventType.PLAN_RESOLVED,),
        EventType.APPROVAL_CONSUMED: (EventType.APPROVAL_ISSUED,),
        EventType.ACTION_CLAIMED: (EventType.POLICY_DECIDED,),
        EventType.LEASE_ACQUIRED: (EventType.ACTION_CLAIMED,),
        EventType.RUNTIME_ATTESTED: (EventType.LEASE_ACQUIRED,),
        EventType.ACTION_STARTED: (EventType.RUNTIME_ATTESTED,),
        EventType.ACTION_PREPARED: (EventType.ACTION_STARTED,),
        EventType.EXECUTION_ATTEMPTED: (EventType.ACTION_PREPARED,),
        EventType.EFFECT_OBSERVED: (EventType.EXECUTION_ATTEMPTED,),
        EventType.EFFECT_RECEIPT_STORED: (EventType.EFFECT_OBSERVED,),
        EventType.ACTION_COMMITTED: (EventType.ACTION_STARTED,),
        EventType.RELEASE_DECIDED: (EventType.EVALUATION_COMPLETED,),
    }
)


def resolve_next_state(
    current_state: str,
    event: EventType,
    context: TransitionContext | None = None,
) -> str:
    """§1.4.2に従い次のActionAttempt Stateを返す。

    Stateを変えないEventは`current_state`をそのまま返す。
    未知のEvent、文脈不足、終端からの遷移は`HarnessError`で停止する。
    """
    if not is_valid_state(StateNamespace.ACTION_ATTEMPT, current_state):
        raise HarnessError(
            ErrorCode.EVENT_ORDER_VIOLATION,
            f"unknown ActionAttempt state: {current_state}",
        )
    if current_state in ACTION_ATTEMPT_TERMINAL_STATES:
        raise HarnessError(
            ErrorCode.EVENT_ORDER_VIOLATION,
            f"terminal state {current_state} does not accept {event.value}; "
            "retry requires a new attempt_id (§1.4.3)",
        )

    context = context or TransitionContext()

    if event is EventType.POLICY_DECIDED:
        return _resolve_policy_decided(context)
    if event is EventType.ACTION_FAILED:
        return _resolve_action_failed(context)
    if event is EventType.ACTION_BLOCKED:
        return _resolve_action_blocked(context)

    deterministic = _DETERMINISTIC_TRANSITIONS.get(event)
    if deterministic is not None:
        return deterministic
    if event in _STATE_PRESERVING_EVENTS:
        return current_state

    raise HarnessError(
        ErrorCode.EVENT_ORDER_VIOLATION,
        f"{event.value} has no defined ActionAttempt transition; add it to §1.4.2 before use",
    )


def _resolve_policy_decided(context: TransitionContext) -> str:
    if context.policy_outcome is None:
        raise HarnessError(
            ErrorCode.EVENT_ORDER_VIOLATION,
            "POLICY_DECIDED requires policy_outcome (ALLOW/DENY)",
        )
    if context.policy_outcome is PolicyOutcome.DENY:
        return "BLOCKED_POLICY"
    if context.approval_required is None:
        raise HarnessError(
            ErrorCode.EVENT_ORDER_VIOLATION,
            "POLICY_DECIDED(ALLOW) requires approval_required to choose "
            "between WAITING_APPROVAL and READY",
        )
    return "WAITING_APPROVAL" if context.approval_required else "READY"


# §1.7 共通エラー分類・Retry規則で「条件付きで自動再試行する」とされた分類名。
#
# `ErrorClassification` は errors.yaml の classification 列から生成されるため、
# **Error Codeが1件も登録されていない分類は列挙に存在しない。**
# 2026-08-07時点で `RATE_LIMITED` / `TRANSIENT_PROVIDER_ERROR` / `TIMEOUT` は
# §1.7に定義がある一方 Error Code が未登録であり、生成Enumに現れない。
# したがってEnum参照ではなく**分類名**で判定する。errors.yamlへ該当Codeが
# 追加された時点で、本Moduleを変更せずに有効になる。
_RETRYABLE_CLASSIFICATION_NAMES: Final[frozenset[str]] = frozenset(
    {"RATE_LIMITED", "TRANSIENT_PROVIDER_ERROR", "TIMEOUT"}
)


def is_retryable_classification(classification: ErrorClassification) -> bool:
    """§1.7で条件付き自動再試行の対象とされた分類か。"""
    return classification.name in _RETRYABLE_CLASSIFICATION_NAMES


def _resolve_action_failed(context: TransitionContext) -> str:
    if context.error_classification is None:
        raise HarnessError(
            ErrorCode.EVENT_ORDER_VIOLATION,
            "ACTION_FAILED requires error_classification to choose between "
            "FAILED_RETRYABLE and FAILED_PERMANENT",
        )
    return (
        "FAILED_RETRYABLE"
        if is_retryable_classification(context.error_classification)
        else "FAILED_PERMANENT"
    )


# `ACTION_BLOCKED`の理由Code Classification から BLOCKED_* を決める（§1.4.2）。
_BLOCKED_STATE_BY_CLASSIFICATION: Final[Mapping[ErrorClassification, str]] = MappingProxyType(
    {
        ErrorClassification.POLICY_DENIED: "BLOCKED_POLICY",
        ErrorClassification.APPROVAL_REQUIRED: "BLOCKED_APPROVAL",
        ErrorClassification.AUTHENTICATION_ERROR: "BLOCKED_APPROVAL",
        ErrorClassification.ENTITLEMENT_ERROR: "BLOCKED_POLICY",
        ErrorClassification.CONFLICT: "BLOCKED_CONFLICT",
        ErrorClassification.EFFECT_UNKNOWN: "EFFECT_UNKNOWN",
    }
)


def _resolve_action_blocked(context: TransitionContext) -> str:
    if context.blocked_reason is None:
        raise HarnessError(
            ErrorCode.EVENT_ORDER_VIOLATION,
            "ACTION_BLOCKED requires blocked_reason (§1.4.2「理由Codeを必須化」)",
        )
    reason_code = context.blocked_reason
    resolved = _BLOCKED_STATE_BY_CLASSIFICATION.get(classification_of(reason_code))
    if resolved is None:
        raise HarnessError(
            ErrorCode.EVENT_ORDER_VIOLATION,
            f"ACTION_BLOCKED with {reason_code.value} "
            f"({classification_of(reason_code).value}) has no BLOCKED_* mapping",
        )
    return resolved


def is_terminal_action_attempt_state(state: str) -> bool:
    return state in ACTION_ATTEMPT_TERMINAL_STATES


def validate_event_order(event: EventType, existing_events: Sequence[EventType]) -> None:
    """§1.4.1の必須先行Eventを満たすか検査する。

    違反時は`EVENT_ORDER_VIOLATION`で停止する。呼出側はStateを変えてはならない
    （`AT-EVENT-ORDER-001/ACTION_STARTED_MISSING`が`attempt_state_unchanged`を要求）。
    """
    required = REQUIRED_PREDECESSORS.get(event)
    if not required:
        return
    present = set(existing_events)
    missing = [item.value for item in required if item not in present]
    if missing:
        raise HarnessError(
            ErrorCode.EVENT_ORDER_VIOLATION,
            f"{event.value} requires preceding {', '.join(missing)}",
        )


def validate_run_terminal_transition(
    target_state: str,
    *,
    unresolved_action_count: int,
    has_unreconciled_effect: bool,
    release_decided: bool,
) -> None:
    """§1.5 Run不変条件。終端へ落とせない条件を検査する。

    * `COMPLETED`には`ReleaseDecision=RELEASE`が必要
    * 未解決Action、未照合Effectを持つRunは終端へ遷移しない。
      遷移先は`BLOCKED_REPAIR_REQUIRED`だけとする
    """
    if not is_valid_state(StateNamespace.RUN, target_state):
        raise HarnessError(ErrorCode.EVENT_ORDER_VIOLATION, f"unknown Run state: {target_state}")
    if target_state not in RUN_TERMINAL_STATES:
        return

    if has_unreconciled_effect:
        raise HarnessError(
            ErrorCode.UNRECONCILED_EFFECT_PRESENT,
            f"Run with unreconciled effect cannot enter {target_state}; "
            "only BLOCKED_REPAIR_REQUIRED is permitted (§1.5)",
        )
    if unresolved_action_count > 0:
        raise HarnessError(
            ErrorCode.UNRECONCILED_EFFECT_PRESENT,
            f"Run with {unresolved_action_count} unresolved action(s) cannot enter {target_state}",
        )
    if target_state == "COMPLETED" and not release_decided:
        raise HarnessError(
            ErrorCode.RELEASE_DECISION_REQUIRED,
            "COMPLETED requires ReleaseDecision=RELEASE (§1.5)",
        )
