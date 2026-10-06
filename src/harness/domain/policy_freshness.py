"""§3.8 Policy Freshness Gate。

## 何を決める規則か

Policy Decisionには有効期限がある。期限を過ぎたPolicyを根拠に**新しい作用**を
起こしてはならない。「前回はこのPolicyで通った」は根拠にならない。

期限切れを検出したとき、選べる道は2つしかない。

| 状況 | 許すこと | 理由 |
|---|---|---|
| まだ作用を起こしていない | **何も起こさず停止** | 新しい作用の根拠が無い |
| 既に作用を起こしたかもしれない | **照合だけ** | 起きたことを確かめないと状態が判定できない |

2つ目が重要である。In-flightのActionを「Policyが切れたから」と放置すると、
外部で起きたかもしれない作用が未照合のまま残る。照合は新しい作用ではなく、
**既に起きたことの観測**なので許される。ただし再試行とFallbackは新しい作用であり、
許されない。

## Registryの語彙だけを使う

本Moduleは `POLICY_STALE_DETECTED` / `POLICY_STALE_ACTION_BLOCKED` /
`POLICY_STALE_RECOVERY_ONLY` と、4つの `POLICY_STALE_*_BLOCKED` Error Codeを使う。
いずれも `design-source/registries/` に実在する。**Registryに無い語彙を作らない。**

## 作用の種類ごとにError Codeが違う

同じ「Policyが古い」でも、止めた対象によってError Codeを分ける。
どこで止まったかが後から分かるようにするためである。

| 作用 | Error Code | 止める位置 |
|---|---|---|
| Local Read | `POLICY_STALE_NEW_ACTION_BLOCKED` | `RUNTIME_GO_POLICY_CHECK` |
| Workspace Write | `POLICY_STALE_EFFECT_BLOCKED` | `BEFORE_ACTION_PREPARED` |
| External Send | `POLICY_STALE_EXTERNAL_EFFECT_BLOCKED` | `EGRESS_PRE_SEND_CHECK` |
| Paid Execution | `POLICY_STALE_PAID_BLOCKED` | `BEFORE_BUDGET_RESERVATION` |
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

from harness.domain._registry_generated import ErrorCode, EventType
from harness.domain.errors import HarnessError
from harness.domain.timestamps import canonical_timestamp

__all__ = [
    "EffectKind",
    "PolicyFreshnessDecision",
    "PolicyFreshnessVerdict",
    "evaluate_policy_freshness",
]


class EffectKind(Enum):
    """止める対象の作用。§3.8の分類に対応する。"""

    LOCAL_READ = "LOCAL_READ"
    WORKSPACE_WRITE = "WORKSPACE_WRITE"
    EXTERNAL_SEND = "EXTERNAL_SEND"
    PAID_EXECUTION = "PAID_EXECUTION"


class PolicyFreshnessVerdict(Enum):
    """評価結果。"""

    FRESH = "FRESH"
    BLOCKED_STALE = "BLOCKED_STALE"
    RECOVERY_ONLY = "RECOVERY_ONLY"


# 作用ごとのError Codeと停止位置。Registryに実在するCodeだけを並べる。
_BLOCK_REASON: Final[dict[EffectKind, tuple[ErrorCode, str]]] = {
    EffectKind.LOCAL_READ: (
        ErrorCode.POLICY_STALE_NEW_ACTION_BLOCKED,
        "RUNTIME_GO_POLICY_CHECK",
    ),
    EffectKind.WORKSPACE_WRITE: (
        ErrorCode.POLICY_STALE_EFFECT_BLOCKED,
        "BEFORE_ACTION_PREPARED",
    ),
    EffectKind.EXTERNAL_SEND: (
        ErrorCode.POLICY_STALE_EXTERNAL_EFFECT_BLOCKED,
        "EGRESS_PRE_SEND_CHECK",
    ),
    EffectKind.PAID_EXECUTION: (
        ErrorCode.POLICY_STALE_PAID_BLOCKED,
        "BEFORE_BUDGET_RESERVATION",
    ),
}

# 新規作用を止めたときのEvent列（§19.1 `AT-POLICY-STALE-001` の期待列）。
_BLOCKED_EVENTS: Final[tuple[EventType, ...]] = (
    EventType.POLICY_STALE_DETECTED,
    EventType.POLICY_STALE_ACTION_BLOCKED,
    EventType.ACTION_BLOCKED,
)

# In-flightを照合だけへ落としたときのEvent列。
_RECOVERY_EVENTS: Final[tuple[EventType, ...]] = (
    EventType.POLICY_STALE_DETECTED,
    EventType.POLICY_STALE_RECOVERY_ONLY,
    EventType.RECOVERY_STARTED,
    EventType.RECOVERY_DECIDED,
    EventType.EFFECT_UNKNOWN,
)


@dataclass(frozen=True, slots=True)
class PolicyFreshnessDecision:
    """Policy Freshness Gateの判定結果。

    **Gate自身は作用を起こさない。** 何を許し何を止めるかだけを返す。
    実際のI/Oはapplication層が、この判定に従って行う（層の依存規則）。
    """

    verdict: PolicyFreshnessVerdict
    attempt_state: str
    error_code: ErrorCode | None
    events: tuple[EventType, ...]
    fault_point: str | None
    #: 新しい作用を起こしてよいか。`RECOVERY_ONLY` でも False である。
    new_effect_allowed: bool
    #: 照合（既に起きたことの観測）を行ってよいか。
    reconciliation_allowed: bool

    @property
    def blocked(self) -> bool:
        return self.verdict is not PolicyFreshnessVerdict.FRESH


def evaluate_policy_freshness(
    *,
    effect_kind: EffectKind,
    policy_expires_at: str,
    now: str,
    effect_attempted: bool,
) -> PolicyFreshnessDecision:
    """Policyの鮮度から、その作用を許すかどうかを決める。

    `effect_attempted` は「既に外部作用を起こした可能性があるか」である。
    True なら結果が判定できないため、照合だけを許す。

    時刻はPort経由で解決した値を渡す。本関数は現在時刻を直接参照しない
    （Plan Content生成中の非決定値混入を防ぐ。不変条件#4）。
    """
    expires = canonical_timestamp(policy_expires_at)
    current = canonical_timestamp(now)

    if current < expires:
        return PolicyFreshnessDecision(
            verdict=PolicyFreshnessVerdict.FRESH,
            # 鮮度は満たす。State遷移はこのGateの外で決まる。
            attempt_state="WAITING_POLICY",
            error_code=None,
            events=(),
            fault_point=None,
            new_effect_allowed=True,
            reconciliation_allowed=True,
        )

    if effect_attempted:
        # 既に起きたかもしれない作用がある。放置すると未照合のまま残る。
        # 照合は新しい作用ではなく、起きたことの観測である。
        return PolicyFreshnessDecision(
            verdict=PolicyFreshnessVerdict.RECOVERY_ONLY,
            attempt_state="EFFECT_UNKNOWN",
            error_code=ErrorCode.EFFECT_UNKNOWN,
            events=_RECOVERY_EVENTS,
            fault_point="AFTER_EXECUTION_ATTEMPTED",
            new_effect_allowed=False,
            reconciliation_allowed=True,
        )

    error_code, fault_point = _BLOCK_REASON[effect_kind]
    return PolicyFreshnessDecision(
        verdict=PolicyFreshnessVerdict.BLOCKED_STALE,
        attempt_state="BLOCKED_POLICY",
        error_code=error_code,
        events=_BLOCKED_EVENTS,
        fault_point=fault_point,
        new_effect_allowed=False,
        # まだ何も起こしていない。照合すべき対象が無い。
        reconciliation_allowed=False,
    )


def require_fresh_policy(
    *,
    effect_kind: EffectKind,
    policy_expires_at: str,
    now: str,
    effect_attempted: bool = False,
) -> PolicyFreshnessDecision:
    """鮮度を満たさなければ例外で停止する呼出し口。

    判定を無視して先へ進む経路を作らないため、返り値を捨てても
    作用が起きないようにする。
    """
    decision = evaluate_policy_freshness(
        effect_kind=effect_kind,
        policy_expires_at=policy_expires_at,
        now=now,
        effect_attempted=effect_attempted,
    )
    if decision.verdict is PolicyFreshnessVerdict.BLOCKED_STALE:
        if decision.error_code is None:  # pragma: no cover - 構成上到達しない
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "blocked verdict without an error code",
            )
        raise HarnessError(
            decision.error_code,
            f"policy expired at {policy_expires_at}; {effect_kind.value} is not permitted",
        )
    return decision
