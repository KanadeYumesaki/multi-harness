"""ADR-006 段階的委任。宣言型Predicateと**いま解決されたPlan**を照合する。

## 判定根拠を置き換えるのであって、禁止を撤廃しない

§3.8.5 の「前回と同じ場合の自動承認」の禁止はそのままである。
本Moduleは**過去の実行履歴を一切参照しない**。判定に使うのは、
人間が事前に承認して署名した `DelegationGrant` のPredicateと、
いま解決されたPlanだけである。

不変条件#11 が禁じるApproval Skip Flag相当の経路は持たない。

## 非委任床はRegistryが正本である

床Ruleは `design-source/registries/delegation-floor.yaml` が正本であり、
**本Moduleはそれを読まない。** Domain層はFilesystemへ触れない（層の依存規則）。
呼出側が読み込んだRuleを `FloorPolicy` として渡す。

条件をコードへ直書きしない（不変条件#18）。**Predicate照合より先に**評価し、
1つでも該当すれば即不成立とする。判定不能な項目があれば
`fail_closed_on_unknown` に従って不成立にする。

床が無いと委任範囲は無限に拡大する。特に `FLOOR-07-DELEGATION-META` が
**委任による委任の作成・拡大**を止めている。

## Grantは実行権限ではない

成立時は、その実行専用の `DerivedApprovalGrant` を**毎回新規発行**する。
`DelegationGrant` そのものを実行権限として使い回さない。
Derived Approval は単回使用であり、2回目の消費は `APPROVAL_REPLAY` で拒否する。

## 失効とEffect開始の競合

不変条件#3 と同じ扱いである。DB内の**Effect線形化点**（`BEGIN IMMEDIATE` +
CAS更新のCommit）を境界とし、Commit後にのみ外部Effectを開始する。

| 失効の時点 | 結果 |
|---|---|
| 線形化点より**前** | Effectを止める。`effect_attempts == 0` |
| 線形化点より**後** | **止められない。** 監査記録しReconciliation対象とする |

**「失効競合時は常にEffect 0件」とは主張しない。** 線形化点より後に失効しても
外部で起きた作用は取り消せない。起きたことを未実行扱いへ戻さない。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final

from harness.domain._registry_generated import ErrorCode, EventType

__all__ = [
    "INVALIDATING_FIELDS",
    "DelegationGrant",
    "DelegationOutcome",
    "DelegationRequest",
    "DerivedApprovalGrant",
    "EffectLinearization",
    "FloorPolicy",
    "GrantState",
    "narrow_predicate",
    "replay_derived_approval",
    "resolve_delegation",
]

# Audit Trail の段階名。Registry の Event 名ではなく、本Module内の進行記録である。
# Registry語彙と混同しないよう `EventType` の値と重ならない名前にする。
_AUDIT_FLOOR: Final[str] = "floor.evaluated"
_AUDIT_PREDICATE: Final[str] = "predicate.matched"
_AUDIT_ISSUE: Final[str] = "derived_approval.issued"
_AUDIT_CONSUME: Final[str] = "derived_approval.consumed"
_AUDIT_TRUST_ANCHOR: Final[str] = "trust_anchor.checked"
_AUDIT_SELF_MOD: Final[str] = "self_modification.checked"
_AUDIT_POLICY_HASH: Final[str] = "policy_hash.checked"
_AUDIT_FRESHNESS: Final[str] = "policy_freshness.checked"
_AUDIT_REVOCATION: Final[str] = "revocation.checked"
_AUDIT_AUDIENCE: Final[str] = "audience.checked"
_AUDIT_REVOKED_BEFORE: Final[str] = "revoked.before_linearization"
_AUDIT_REVOKED_AFTER: Final[str] = "revoked.after_linearization"
_AUDIT_REPLAY_DENIED: Final[str] = "derived_approval.replay_denied"

# §3.8.2 の無効化項目。Approvalを無効化するField＝Predicateの照合対象である。
# ADR-006「Predicateは§3.8.2の無効化項目を全て照合対象へ含める」。
INVALIDATING_FIELDS: Final[tuple[str, ...]] = (
    "plan",
    "context",
    "target_path",
    "base_hash",
    "provider",
    "executable",
    "argv",
    "cwd",
    "runtime_spec",
    "auth_route",
    "account",
    "policy",
    "schema",
    "token_profile",
    "pricing",
    "entitlement",
)


@dataclass(frozen=True, slots=True)
class FloorPolicy:
    """非委任床。Registryから読み込んだものを呼出側が渡す。

    Domain層はFilesystemへ触れない。読込みは infrastructure 側が行う
    （`harness.infrastructure.delegation_floor`）。
    """

    rules: tuple[Mapping[str, Any], ...]
    fail_closed_on_unknown: bool
    evaluation_order: str

    def rule_ids(self) -> tuple[str, ...]:
        return tuple(str(rule["rule_id"]) for rule in self.rules)


class GrantState(Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    SUPERSEDED = "SUPERSEDED"
    INVALIDATED = "INVALIDATED"


@dataclass(frozen=True, slots=True)
class DelegationGrant:
    """人間が承認して発行した署名付きArtifact。**Flagではない。**"""

    delegation_id: str
    state: GrantState
    #: 委任先。Workspace／Subjectが一致しなければ使えない。
    audience_workspace_id: str
    subject_id: str
    #: 宣言型Predicate。§3.8.2 の16項目を照合対象に持つ。
    predicate: Mapping[str, Any]
    #: 発行時のPolicy Hash。変わればGrantは無効になる。
    policy_hash: str
    #: 署名したTrust AnchorのHashとVersion。
    trust_anchor_hash: str
    trust_anchor_version: int


@dataclass(frozen=True, slots=True)
class DerivedApprovalGrant:
    """その実行専用の承認。**単回使用。**"""

    approval_id: str
    delegation_id: str
    execution_plan_hash: str
    consumed: bool = False

    def consume(self) -> DerivedApprovalGrant:
        """1回だけ消費できる。2回目は例外ではなく呼出側が拒否判定に使う。"""
        if self.consumed:
            raise ValueError("derived approval already consumed")
        return DerivedApprovalGrant(
            approval_id=self.approval_id,
            delegation_id=self.delegation_id,
            execution_plan_hash=self.execution_plan_hash,
            consumed=True,
        )


class EffectLinearization(Enum):
    """Effect線形化点との前後関係。"""

    BEFORE = "BEFORE"
    AFTER = "AFTER"


@dataclass(frozen=True, slots=True)
class DelegationOutcome:
    """委任解決の結果。"""

    attempt_state: str
    grant_state: str | None
    error_code: ErrorCode | None
    events: tuple[EventType, ...]
    #: 自動承認された件数。床に触れたら常に0。
    auto_approved_count: int = 0
    #: 人間の承認が要るか。
    human_approval_required: bool = True
    #: Predicateで照合したField数。
    checked_field_count: int = 0
    #: 作成されたDelegationGrantの数。委任による委任は常に0。
    delegation_created_count: int = 0
    #: 起こしたEffectの数。
    effect_attempts: int = 0
    duplicate_effects: int = 0
    reconciliation_required: bool = False
    derived_approval: DerivedApprovalGrant | None = None
    #: 触れた床Rule。
    floor_rule_id: str | None = None
    approval_mode: str | None = None
    delegation_id_present: bool = False
    audit_trail: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class DelegationRequest:
    """委任解決の入力。**過去の実行履歴は含まない。**"""

    grant: DelegationGrant
    #: いま解決されたPlanの16項目。
    plan_fields: Mapping[str, Any]
    #: 実行しようとしているWorkspace／Subject。
    workspace_id: str
    subject_id: str
    #: 現在のPolicy Hashと鮮度。
    current_policy_hash: str
    policy_freshness: str
    #: 床評価に使う観測値。
    floor_facts: Mapping[str, Any]
    #: 現在のTrust Anchor。
    current_trust_anchor_hash: str
    minimum_trust_anchor_version: int
    execution_plan_hash: str
    #: この解決がDelegationGrant自体を作る／広げるものか。
    creates_or_widens_delegation: bool = False


def _floor_violation(request: DelegationRequest, floor: FloorPolicy) -> Mapping[str, Any] | None:
    """床評価。**Predicate照合より先に行う。**

    判定に必要な情報が欠けていれば `fail_closed_on_unknown` に従い不成立にする。
    """
    facts = dict(request.floor_facts)

    # 委任による委任の作成・拡大は床である（FLOOR-07）。
    if request.creates_or_widens_delegation:
        facts["delegation_meta_operation"] = True

    for rule in floor.rules:
        fact_key = str(rule["rule_id"]).lower().replace("-", "_")
        # Registry の condition を評価するのではなく、Resolverが渡した
        # 観測値を Rule ID で引く。任意式の評価系を持たない（ADR-006）。
        triggered = facts.get(rule["rule_id"])
        if triggered is None:
            triggered = facts.get(fact_key)
        if triggered is None:
            if floor.fail_closed_on_unknown:
                # 判定できない項目がある。委任を成立させない。
                return rule
            continue
        if triggered:
            return rule
    return None


def narrow_predicate(
    original: Mapping[str, Any], proposed: Mapping[str, Any]
) -> tuple[bool, str | None]:
    """Predicateの縮小だけを許す。

    **拡大は既存Grantの編集では認めない。** 広げたいなら新しいGrantを
    人間が承認して発行する。編集で広げられるなら、承認した範囲と
    実際の範囲が食い違う。
    """
    for key, value in proposed.items():
        if key not in original:
            return False, f"{key} is not in the original predicate"
        current = original[key]
        if isinstance(current, list | tuple | set) and isinstance(value, list | tuple | set):
            if not set(value) <= set(current):
                return False, f"{key} is not a subset"
        elif value != current:
            return False, f"{key} differs and is not a narrowing"
    return True, None


def resolve_delegation(
    request: DelegationRequest,
    floor: FloorPolicy,
    *,
    revocation: EffectLinearization | None = None,
) -> DelegationOutcome:
    """委任が成立するかを判定する。

    判定順は「後から取り返せない拒否」を先に置く。
    床 → Trust Anchor → Policy → 失効 → Audience → Predicate。
    """
    audit: list[str] = []

    # --- Trust Anchor（Rollbackを拒む）---------------------------------
    if (
        request.grant.trust_anchor_hash != request.current_trust_anchor_hash
        or request.grant.trust_anchor_version < request.minimum_trust_anchor_version
    ):
        return DelegationOutcome(
            attempt_state="WAITING_APPROVAL",
            grant_state=GrantState.INVALIDATED.value,
            error_code=ErrorCode.DELEGATION_TRUST_ANCHOR_INVALID,
            events=(),
            human_approval_required=True,
            audit_trail=(_AUDIT_TRUST_ANCHOR,),
        )

    # --- Self modification（委任による委任）----------------------------
    if request.creates_or_widens_delegation:
        return DelegationOutcome(
            attempt_state="BLOCKED_POLICY",
            grant_state=request.grant.state.value,
            error_code=ErrorCode.DELEGATION_SELF_MODIFICATION_DENIED,
            events=(EventType.POLICY_DECIDED, EventType.ACTION_BLOCKED),
            delegation_created_count=0,
            audit_trail=(_AUDIT_SELF_MOD,),
        )

    # --- Policy Hash 変更 ----------------------------------------------
    if request.grant.policy_hash != request.current_policy_hash:
        return DelegationOutcome(
            attempt_state="WAITING_APPROVAL",
            grant_state=GrantState.INVALIDATED.value,
            error_code=None,
            events=(EventType.DELEGATION_INVALIDATED, EventType.POLICY_DECIDED),
            audit_trail=(_AUDIT_POLICY_HASH,),
        )

    # --- Policy 鮮度 ------------------------------------------------------
    if request.policy_freshness != "CURRENT":
        return DelegationOutcome(
            attempt_state="WAITING_APPROVAL",
            grant_state=request.grant.state.value,
            error_code=None,
            events=(EventType.POLICY_STALE_DETECTED, EventType.DELEGATION_REJECTED),
            audit_trail=(_AUDIT_FRESHNESS,),
        )

    # --- 即時失効 ---------------------------------------------------------
    if request.grant.state is GrantState.REVOKED and revocation is None:
        return DelegationOutcome(
            attempt_state="WAITING_APPROVAL",
            grant_state=GrantState.REVOKED.value,
            error_code=ErrorCode.APPROVAL_REQUIRED,
            events=(EventType.DELEGATION_REVOKED, EventType.POLICY_DECIDED),
            audit_trail=(_AUDIT_REVOCATION,),
        )

    # --- Audience／Subject -----------------------------------------------
    if (
        request.grant.audience_workspace_id != request.workspace_id
        or request.grant.subject_id != request.subject_id
    ):
        return DelegationOutcome(
            attempt_state="WAITING_APPROVAL",
            grant_state=request.grant.state.value,
            error_code=ErrorCode.DELEGATION_SUBJECT_MISMATCH,
            events=(),
            audit_trail=(_AUDIT_AUDIENCE,),
        )

    # --- 非委任床（Predicate照合より先）-----------------------------------
    violated = _floor_violation(request, floor)
    if violated is not None:
        return DelegationOutcome(
            attempt_state="WAITING_APPROVAL",
            grant_state=request.grant.state.value,
            error_code=ErrorCode(violated["error_code"]),
            events=(),
            auto_approved_count=0,
            floor_rule_id=violated["rule_id"],
            audit_trail=(_AUDIT_FLOOR,),
        )
    audit.append(_AUDIT_FLOOR)

    # --- Predicate 照合（16項目すべて）------------------------------------
    mismatched = [
        name
        for name in INVALIDATING_FIELDS
        if request.plan_fields.get(name) != request.grant.predicate.get(name)
    ]
    audit.append(_AUDIT_PREDICATE)
    if mismatched:
        # 1項目でも違えば人間へ戻す。範囲外は自動承認しない。
        code = (
            ErrorCode.DELEGATION_SCOPE_EXCEEDED
            if len(mismatched) < len(INVALIDATING_FIELDS)
            else ErrorCode.APPROVAL_REQUIRED
        )
        return DelegationOutcome(
            attempt_state="WAITING_APPROVAL",
            grant_state=request.grant.state.value,
            error_code=code,
            events=(
                EventType.PLAN_RESOLVED,
                EventType.POLICY_DECIDED,
                EventType.DELEGATION_REJECTED,
            ),
            auto_approved_count=0,
            human_approval_required=True,
            checked_field_count=len(INVALIDATING_FIELDS),
            audit_trail=tuple(audit),
        )

    # --- 成立。DerivedApprovalGrant を毎回新規発行する --------------------
    derived = DerivedApprovalGrant(
        approval_id=f"derived-{request.execution_plan_hash[:16]}",
        delegation_id=request.grant.delegation_id,
        execution_plan_hash=request.execution_plan_hash,
    )
    audit.extend([_AUDIT_ISSUE, _AUDIT_CONSUME])

    # --- 失効とEffect線形化点の競合 ---------------------------------------
    if revocation is EffectLinearization.BEFORE:
        # 線形化点より前。まだ外部Effectを開始していないので止められる。
        return DelegationOutcome(
            attempt_state="BLOCKED_CONFLICT",
            grant_state=GrantState.REVOKED.value,
            error_code=ErrorCode.DELEGATION_REVOKED_MID_FLIGHT,
            events=(
                EventType.DELEGATION_MATCHED,
                EventType.APPROVAL_CONSUMED,
                EventType.DELEGATION_REVOKED,
                EventType.ACTION_BLOCKED,
            ),
            effect_attempts=0,
            duplicate_effects=0,
            derived_approval=derived.consume(),
            audit_trail=(*audit, _AUDIT_REVOKED_BEFORE),
        )

    if revocation is EffectLinearization.AFTER:
        # 線形化点より後。**止められない。** 起きたことを未実行扱いへ戻さない。
        return DelegationOutcome(
            attempt_state="READY",
            grant_state=GrantState.REVOKED.value,
            error_code=ErrorCode.DELEGATION_REVOKED_AFTER_EFFECT_START,
            events=(
                EventType.DELEGATION_MATCHED,
                EventType.APPROVAL_CONSUMED,
                EventType.DELEGATION_EFFECT_LINEARIZED,
                EventType.DELEGATION_REVOKED_AFTER_EFFECT_START,
            ),
            effect_attempts=1,
            duplicate_effects=0,
            reconciliation_required=True,
            derived_approval=derived.consume(),
            audit_trail=(*audit, _AUDIT_REVOKED_AFTER),
        )

    return DelegationOutcome(
        attempt_state="READY",
        grant_state=GrantState.ACTIVE.value,
        error_code=None,
        events=(
            EventType.DELEGATION_MATCHED,
            EventType.APPROVAL_ISSUED,
            EventType.APPROVAL_CONSUMED,
        ),
        auto_approved_count=1,
        human_approval_required=False,
        checked_field_count=len(INVALIDATING_FIELDS),
        effect_attempts=1,
        duplicate_effects=0,
        derived_approval=derived,
        approval_mode="POLICY_DELEGATED",
        delegation_id_present=True,
        audit_trail=tuple(audit),
    )


def replay_derived_approval(approval: DerivedApprovalGrant) -> DelegationOutcome:
    """消費済みDerived Approvalの再使用を拒否する。

    `DelegationGrant` は実行権限ではない。Derived Approval も単回使用である。
    """
    if not approval.consumed:
        raise ValueError("approval has not been consumed yet")
    return DelegationOutcome(
        attempt_state="BLOCKED_APPROVAL",
        grant_state=None,
        error_code=ErrorCode.APPROVAL_REPLAY,
        events=(
            EventType.DELEGATION_MATCHED,
            EventType.APPROVAL_CONSUMED,
            EventType.APPROVAL_REPLAY_DENIED,
            EventType.ACTION_BLOCKED,
        ),
        effect_attempts=0,
        audit_trail=(_AUDIT_REPLAY_DENIED,),
    )
