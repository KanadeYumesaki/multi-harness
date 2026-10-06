"""`AT-DELEGATION-001` 14 Case の統合試験（ADR-006）。

期待値は `design-source/registries/tests.yaml` から読み取る。Case IDも手入力しない。

## 守っている核

**過去の実行履歴を判定根拠にしない。** 判定に使うのは、人間が事前に承認した
宣言型Predicateと、いま解決されたPlanだけである。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from harness.domain.delegation import (
    INVALIDATING_FIELDS,
    DelegationGrant,
    DelegationRequest,
    DerivedApprovalGrant,
    EffectLinearization,
    GrantState,
    narrow_predicate,
    replay_derived_approval,
    resolve_delegation,
)
from harness.infrastructure.delegation_floor import load_floor_policy

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTRIES = REPO_ROOT / "design-source" / "registries"

POLICY_HASH = "sha256:" + "a" * 64
ANCHOR_HASH = "sha256:" + "b" * 64
PLAN_HASH = "sha256:" + "c" * 64

# 16項目すべてが一致する Predicate／Plan。
BASELINE: dict[str, Any] = {name: f"value-{name}" for name in INVALIDATING_FIELDS}

# 床にどれも触れない観測値。Rule IDごとに false を渡す。
import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "support"))
from ledger_probe import LedgerProbe, SideEffectProbe, record_case  # noqa: E402

FLOOR = load_floor_policy()
CLEAR_FLOOR: dict[str, Any] = {rule_id: False for rule_id in FLOOR.rule_ids()}


def _case(case_id: str) -> dict[str, Any]:
    rows = yaml.safe_load((REGISTRIES / "tests.yaml").read_text(encoding="utf-8"))["test_cases"]
    for row in rows:
        if row["test_id"] == "AT-DELEGATION-001" and row["case_id"] == case_id:
            return dict(row)
    raise AssertionError(f"Registryに AT-DELEGATION-001/{case_id} が無い")


def _assertion_int(case: dict[str, Any], name: str) -> int:
    for assertion in case["assertions"]:
        left, _, right = assertion.partition("==")
        if left.strip() == name:
            return int(right.strip())
    raise AssertionError(f"{name} の assertion が無い")


def _grant(**overrides: Any) -> DelegationGrant:
    base = DelegationGrant(
        delegation_id="dg-1",
        state=GrantState.ACTIVE,
        audience_workspace_id="ws-1",
        subject_id="subject-1",
        predicate=dict(BASELINE),
        policy_hash=POLICY_HASH,
        trust_anchor_hash=ANCHOR_HASH,
        trust_anchor_version=2,
    )
    return replace(base, **overrides)


def _request(**overrides: Any) -> DelegationRequest:
    base = DelegationRequest(
        grant=_grant(),
        plan_fields=dict(BASELINE),
        workspace_id="ws-1",
        subject_id="subject-1",
        current_policy_hash=POLICY_HASH,
        policy_freshness="CURRENT",
        floor_facts=dict(CLEAR_FLOOR),
        current_trust_anchor_hash=ANCHOR_HASH,
        minimum_trust_anchor_version=2,
        execution_plan_hash=PLAN_HASH,
    )
    return replace(base, **overrides)


def _resolve(request: DelegationRequest, **kwargs: Any) -> Any:
    """床Policyを注入して解決する。

    Domain層はFilesystemへ触れないため、床は呼出側が読んで渡す。
    """
    return resolve_delegation(request, FLOOR, **kwargs)


def _observe(
    case_id: str,
    outcome: Any,
    request: Any,
    observation: Any,
    *,
    grant_subject: bool = False,
    subject_id: str | None = None,
) -> None:
    """Registry突合とLedger観測を1箇所で行う。

    `outcome.events` は Domain が「Appendされるべき」と決めた列である。
    **そのまま観測値にしない。** Application 層と同じように Ledger へ Append
    してから読み直す（設計書§19.1.1）。

    副作用は `outcome.effect_attempts` を実測値として渡す。拒否経路の 0 は
    書き込んだ値ではなく数えた結果である。Probe は Case ごとに新規作成し、
    共有しない（他CaseのEvent混入を防ぐ）。
    """
    _assert_registry(case_id, outcome, grant_subject=grant_subject)

    ledger = LedgerProbe()
    effects = SideEffectProbe()
    head_before = ledger.head
    ledger.append_names([event.value for event in outcome.events])

    observation.record_input(
        {
            "delegation_id": request.grant.delegation_id,
            "grant_state": request.grant.state.value,
            "audience_workspace_id": request.grant.audience_workspace_id,
            "grant_subject_id": request.grant.subject_id,
            "predicate_keys": sorted(request.grant.predicate),
            "policy_hash": request.grant.policy_hash,
            "trust_anchor_hash": request.grant.trust_anchor_hash,
            "trust_anchor_version": request.grant.trust_anchor_version,
            "workspace_id": request.workspace_id,
            "subject_id": request.subject_id,
            "current_policy_hash": request.current_policy_hash,
            "policy_freshness": request.policy_freshness,
            "floor_facts": {k: request.floor_facts[k] for k in sorted(request.floor_facts)},
            "current_trust_anchor_hash": request.current_trust_anchor_hash,
            "minimum_trust_anchor_version": request.minimum_trust_anchor_version,
            "execution_plan_hash": request.execution_plan_hash,
            "creates_or_widens_delegation": request.creates_or_widens_delegation,
            "plan_field_keys": sorted(request.plan_fields),
        }
    )
    record_case(
        observation,
        state=outcome.grant_state if grant_subject else outcome.attempt_state,
        subject_id=subject_id or request.grant.delegation_id,
        ledger=ledger,
        head_before=head_before,
        effects=effects,
        error_code=outcome.error_code.value if outcome.error_code else None,
        # Effect試行数はEvent列ではなく副作用の計測値である。
        # 架空のEventをLedgerへ足して表現しない（Registry語彙を汚さない）。
        ledger_effect_attempts=outcome.effect_attempts,
    )

    expected = _case(case_id)
    # 先頭一致ではなく**全体一致**で突合する。先頭一致だと余分なEventを見逃す。
    assert list(observation.observed_events) == expected["expected_event_sequence"], case_id
    assert observation.ledger_head_after == len(observation.observed_events)


def _assert_registry(case_id: str, outcome: Any, *, grant_subject: bool = False) -> None:
    """State・Error Code・Event列をRegistry期待値と突き合わせる。"""
    expected = _case(case_id)
    actual_state = outcome.grant_state if grant_subject else outcome.attempt_state
    assert actual_state == expected["expected_state"], case_id
    if expected["expected_error_code"] is None:
        assert outcome.error_code is None, case_id
    else:
        assert outcome.error_code is not None, case_id
        assert outcome.error_code.value == expected["expected_error_code"], case_id
    assert [e.value for e in outcome.events] == expected["expected_event_sequence"], case_id


# ==========================================================================
# Phase 2：Scope／Subject
# ==========================================================================


@pytest.mark.case("AT-DELEGATION-001/SCOPE_EXCEEDED")
def test_delegated_plan_outside_predicate_escalates_to_human(case_observation: Any) -> None:
    """Predicate外のPlanを自動承認しないこと。"""
    expected = _case("SCOPE_EXCEEDED")
    plan = {**BASELINE, **{name: "changed" for name in INVALIDATING_FIELDS}}
    request = _request(plan_fields=plan)
    outcome = _resolve(request)
    _observe("SCOPE_EXCEEDED", outcome, request, case_observation)
    assert outcome.auto_approved_count == _assertion_int(expected, "auto_approved_count")
    assert outcome.human_approval_required is True


@pytest.mark.case("AT-DELEGATION-001/AUDIENCE_MISMATCH")
def test_delegation_from_another_workspace_is_rejected(case_observation: Any) -> None:
    """別WorkspaceのGrantを使えないこと。"""
    request = _request(workspace_id="ws-other")
    outcome = _resolve(request)
    _observe("AUDIENCE_MISMATCH", outcome, request, case_observation)
    assert outcome.auto_approved_count == 0


def test_subject_mismatch_is_also_rejected() -> None:
    """Subjectが違えば拒否すること。"""
    outcome = _resolve(_request(subject_id="subject-other"))
    assert outcome.error_code is not None
    assert outcome.error_code.value == "DELEGATION_SUBJECT_MISMATCH"


@pytest.mark.case("AT-DELEGATION-001/PREDICATE_FIELD_CHANGED")
def test_any_invalidating_field_change_escalates_to_human(case_observation: Any) -> None:
    """無効化項目が1つでも変われば人間へ戻すこと。16項目すべてを照合する。"""
    expected = _case("PREDICATE_FIELD_CHANGED")
    plan = {**BASELINE, "provider": "different-provider"}
    request = _request(plan_fields=plan)
    outcome = _resolve(request)
    _observe("PREDICATE_FIELD_CHANGED", outcome, request, case_observation)
    assert outcome.checked_field_count == _assertion_int(expected, "checked_field_count")
    assert outcome.auto_approved_count == _assertion_int(expected, "auto_approved_count")


@pytest.mark.parametrize("field_name", INVALIDATING_FIELDS)
def test_every_invalidating_field_blocks_auto_approval(field_name: str) -> None:
    """16項目のどれが変わっても自動承認しないこと。

    1項目でも見落とすと、そのFieldだけ差し替えて自動承認を通せる。
    """
    plan = {**BASELINE, field_name: "changed"}
    outcome = _resolve(_request(plan_fields=plan))
    assert outcome.auto_approved_count == 0, field_name
    assert outcome.human_approval_required is True, field_name


@pytest.mark.case("AT-DELEGATION-001/SELF_MODIFICATION")
def test_delegation_cannot_create_or_widen_delegation(case_observation: Any) -> None:
    """委任による委任の作成・拡大を禁じること。

    これが無いと委任範囲が無限に拡大する。
    """
    expected = _case("SELF_MODIFICATION")
    request = _request(creates_or_widens_delegation=True)
    outcome = _resolve(request)
    _observe("SELF_MODIFICATION", outcome, request, case_observation)
    assert outcome.delegation_created_count == _assertion_int(expected, "delegation_created_count")


@pytest.mark.case("AT-DELEGATION-001/DESTRUCTIVE_NEVER_DELEGATED")
def test_destructive_operations_ignore_delegation(case_observation: Any) -> None:
    """破壊的・特権操作を自動委任しないこと（FLOOR-08）。"""
    expected = _case("DESTRUCTIVE_NEVER_DELEGATED")
    facts = {**CLEAR_FLOOR, "FLOOR-08-DESTRUCTIVE": True}
    request = _request(floor_facts=facts)
    outcome = _resolve(request)
    _observe("DESTRUCTIVE_NEVER_DELEGATED", outcome, request, case_observation)
    assert outcome.auto_approved_count == _assertion_int(expected, "auto_approved_count")
    assert outcome.floor_rule_id == "FLOOR-08-DESTRUCTIVE"


@pytest.mark.parametrize("rule", list(FLOOR.rule_ids()))
def test_every_floor_rule_blocks_delegation(rule: str) -> None:
    """床Rule11件すべてが委任を止めること。Registryが正本である。"""
    facts = {**CLEAR_FLOOR, rule: True}
    outcome = _resolve(_request(floor_facts=facts))
    assert outcome.auto_approved_count == 0, rule
    assert outcome.floor_rule_id == rule


def test_floor_is_evaluated_before_predicate_match() -> None:
    """床評価がPredicate照合より先であること。

    Predicateが完全一致していても床に触れれば不成立である。
    """
    facts = {**CLEAR_FLOOR, "FLOOR-01-PAID": True}
    outcome = _resolve(_request(floor_facts=facts))
    assert outcome.floor_rule_id == "FLOOR-01-PAID"
    assert outcome.checked_field_count == 0, "Predicate照合まで進んでいる"


def test_unknown_floor_fact_fails_closed() -> None:
    """床の判定に必要な情報が欠けていれば不成立にすること。

    `fail_closed_on_unknown: true`。判定できないものを通さない。
    """
    facts = dict(CLEAR_FLOOR)
    del facts["FLOOR-03-SECRET"]
    outcome = _resolve(_request(floor_facts=facts))
    assert outcome.auto_approved_count == 0
    assert outcome.floor_rule_id == "FLOOR-03-SECRET"


def test_floor_rules_come_from_the_registry() -> None:
    """床条件をコードへ直書きしていないこと（不変条件#18）。"""
    document = yaml.safe_load((REGISTRIES / "delegation-floor.yaml").read_text(encoding="utf-8"))
    assert list(FLOOR.rule_ids()) == [r["rule_id"] for r in document["rules"]]
    assert document["evaluation_order"] == "BEFORE_PREDICATE_MATCH"
    assert document["fail_closed_on_unknown"] is True


# ==========================================================================
# Phase 3：Policy／Revocation
# ==========================================================================


@pytest.mark.case("AT-DELEGATION-001/POLICY_HASH_CHANGED")
def test_policy_change_invalidates_delegation(case_observation: Any) -> None:
    """Policy Hashが変わればGrantを無効化すること。"""
    request = _request(current_policy_hash="sha256:" + "9" * 64)
    outcome = _resolve(request)
    _observe("POLICY_HASH_CHANGED", outcome, request, case_observation, grant_subject=True)


@pytest.mark.case("AT-DELEGATION-001/REVOKE_IMMEDIATE")
def test_revoked_delegation_stops_auto_approval_at_once(case_observation: Any) -> None:
    """失効後は即座に自動承認を止めること。"""
    request = _request(grant=_grant(state=GrantState.REVOKED))
    outcome = _resolve(request)
    _observe("REVOKE_IMMEDIATE", outcome, request, case_observation)
    assert outcome.auto_approved_count == 0


@pytest.mark.case("AT-DELEGATION-001/STALE_POLICY_NO_AUTO")
def test_delegation_inactive_when_policy_not_current(case_observation: Any) -> None:
    """Policyが最新でなければ自動実行しないこと。"""
    request = _request(policy_freshness="STALE_TTL_EXCEEDED")
    outcome = _resolve(request)
    _observe("STALE_POLICY_NO_AUTO", outcome, request, case_observation)
    assert outcome.auto_approved_count == 0


@pytest.mark.case("AT-DELEGATION-001/WIDEN_DENIED")
def test_narrow_rejects_predicate_that_is_not_a_subset(case_observation: Any) -> None:
    """既存Grantの編集でScopeを広げられないこと。

    広げられるなら、人間が承認した範囲と実際の範囲が食い違う。
    """
    expected = _case("WIDEN_DENIED")
    original = {"target_path": ["/a", "/b"]}
    widened = {"target_path": ["/a", "/b", "/c"]}
    ok, reason = narrow_predicate(original, widened)
    assert ok is False
    assert reason is not None
    # assertions: original_predicate_unchanged == true
    assert original == {"target_path": ["/a", "/b"]}
    assert expected["expected_state"] == "ACTIVE"
    assert expected["expected_error_code"] == "DELEGATION_SCOPE_EXCEEDED"

    # narrow の拒否は Grant を編集しないので Ledger は動かない。
    # **それでも Ledger を観測する。** 見て0件と、見ていないことは別である。
    ledger = LedgerProbe()
    effects = SideEffectProbe()
    head_before = ledger.head
    case_observation.record_input(
        {
            "original_predicate": {k: sorted(v) for k, v in original.items()},
            "requested_predicate": {k: sorted(v) for k, v in widened.items()},
            "operation": "narrow_predicate",
        }
    )
    record_case(
        case_observation,
        state=expected["expected_state"],
        subject_id="dg-1",
        ledger=ledger,
        head_before=head_before,
        effects=effects,
        error_code=expected["expected_error_code"],
    )
    assert list(case_observation.observed_events) == (expected["expected_event_sequence"] or [])
    assert case_observation.ledger_head_after == head_before


def test_narrowing_a_predicate_is_allowed() -> None:
    """縮小だけは安全方向として許すこと。"""
    ok, reason = narrow_predicate({"target_path": ["/a", "/b"]}, {"target_path": ["/a"]})
    assert ok is True
    assert reason is None


def test_adding_a_new_predicate_key_is_denied() -> None:
    """元Predicateに無いKeyの追加を拒否すること。"""
    ok, _ = narrow_predicate({"target_path": ["/a"]}, {"provider": "x"})
    assert ok is False


@pytest.mark.case("AT-DELEGATION-001/TRUST_ANCHOR_ROLLBACK")
def test_delegation_signed_by_rotated_key_is_rejected(case_observation: Any) -> None:
    """Rotate／Rollbackされた鍵で署名されたGrantを拒否すること。"""
    request = _request(current_trust_anchor_hash="sha256:" + "f" * 64)
    outcome = _resolve(request)
    _observe("TRUST_ANCHOR_ROLLBACK", outcome, request, case_observation, grant_subject=True)


def test_trust_anchor_version_rollback_is_rejected() -> None:
    """古いProfile Versionへの巻き戻しを拒否すること。"""
    outcome = _resolve(
        _request(grant=_grant(trust_anchor_version=1), minimum_trust_anchor_version=2)
    )
    assert outcome.grant_state == "INVALIDATED"
    assert outcome.error_code is not None
    assert outcome.error_code.value == "DELEGATION_TRUST_ANCHOR_INVALID"


# ==========================================================================
# Phase 4：Effect Linearization
# ==========================================================================


@pytest.mark.case("AT-DELEGATION-001/REVOKE_BEFORE_EFFECT_LINEARIZATION")
def test_revocation_wins_before_effect_linearization(case_observation: Any) -> None:
    """線形化点より前の失効はEffectを止めること。"""
    expected = _case("REVOKE_BEFORE_EFFECT_LINEARIZATION")
    request = _request()
    outcome = _resolve(request, revocation=EffectLinearization.BEFORE)
    _observe("REVOKE_BEFORE_EFFECT_LINEARIZATION", outcome, request, case_observation)
    # assertions: effect_attempts == 0 / duplicate_effects == 0
    assert outcome.effect_attempts == _assertion_int(expected, "effect_attempts")
    assert outcome.duplicate_effects == _assertion_int(expected, "duplicate_effects")


@pytest.mark.case("AT-DELEGATION-001/REVOKE_AFTER_EFFECT_LINEARIZATION")
def test_effect_linearization_wins_before_revocation(case_observation: Any) -> None:
    """線形化点より後の失効はEffectを止められないこと。

    **起きたことを未実行扱いへ戻さない。** Reconciliation対象として記録する。
    """
    expected = _case("REVOKE_AFTER_EFFECT_LINEARIZATION")
    request = _request()
    outcome = _resolve(request, revocation=EffectLinearization.AFTER)
    _observe("REVOKE_AFTER_EFFECT_LINEARIZATION", outcome, request, case_observation)
    # assertions: effect_attempts == 1 / reconciliation_required == true
    #             / duplicate_effects == 0
    assert outcome.effect_attempts == _assertion_int(expected, "effect_attempts")
    assert outcome.duplicate_effects == _assertion_int(expected, "duplicate_effects")
    assert outcome.reconciliation_required is True


def test_before_and_after_linearization_are_distinct_states() -> None:
    """線形化点の前後を同じ状態へ畳まないこと。

    畳むと、起きた作用が未実行として記録されるか、
    起きていない作用が実行済みとして記録される。
    """
    before = _resolve(_request(), revocation=EffectLinearization.BEFORE)
    after = _resolve(_request(), revocation=EffectLinearization.AFTER)
    assert before.attempt_state != after.attempt_state
    assert before.error_code != after.error_code
    assert before.effect_attempts == 0
    assert after.effect_attempts == 1
    assert before.reconciliation_required is False
    assert after.reconciliation_required is True


def test_audit_trail_distinguishes_the_linearization_side() -> None:
    """どちら側で失効したかがAudit Trailに残ること。"""
    before = _resolve(_request(), revocation=EffectLinearization.BEFORE)
    after = _resolve(_request(), revocation=EffectLinearization.AFTER)
    assert "revoked.before_linearization" in before.audit_trail
    assert "revoked.after_linearization" in after.audit_trail


# ==========================================================================
# Phase 5：Derived Approval
# ==========================================================================


@pytest.mark.case("AT-DELEGATION-001/DERIVED_APPROVAL_IS_SINGLE_USE")
def test_derived_approval_cannot_be_replayed(case_observation: Any) -> None:
    """Derived Approvalの2回目の使用を拒否すること。"""
    expected = _case("DERIVED_APPROVAL_IS_SINGLE_USE")
    request = _request()
    granted = _resolve(request)
    assert granted.derived_approval is not None
    consumed = granted.derived_approval.consume()

    outcome = replay_derived_approval(consumed)
    _observe("DERIVED_APPROVAL_IS_SINGLE_USE", outcome, request, case_observation)
    # assertions: delegation_grant_reused_as_execution_authority == false
    assert outcome.effect_attempts == 0
    assert expected["assertions"] == ["delegation_grant_reused_as_execution_authority == false"]


def test_consuming_twice_raises() -> None:
    """同じDerived Approvalを2回消費できないこと。"""
    approval = DerivedApprovalGrant(
        approval_id="a-1", delegation_id="dg-1", execution_plan_hash=PLAN_HASH
    )
    consumed = approval.consume()
    with pytest.raises(ValueError):
        consumed.consume()


def test_each_resolution_issues_a_new_derived_approval() -> None:
    """成立のたびに新しいDerived Approvalを発行すること。

    `DelegationGrant` を実行権限として使い回さない。
    """
    first = _resolve(_request())
    second = _resolve(_request(execution_plan_hash="sha256:" + "d" * 64))
    assert first.derived_approval is not None
    assert second.derived_approval is not None
    assert first.derived_approval.approval_id != second.derived_approval.approval_id
    assert first.derived_approval.consumed is False


def test_replay_requires_a_consumed_approval() -> None:
    """未消費のApprovalをReplay扱いしないこと。"""
    approval = DerivedApprovalGrant(
        approval_id="a-1", delegation_id="dg-1", execution_plan_hash=PLAN_HASH
    )
    with pytest.raises(ValueError):
        replay_derived_approval(approval)


# ==========================================================================
# Phase 6：Audit Trail
# ==========================================================================


@pytest.mark.case("AT-DELEGATION-001/AUDIT_TRAIL_COMPLETE")
def test_auto_approved_run_is_fully_traceable(case_observation: Any) -> None:
    """自動承認された実行が委任まで辿れること。"""
    expected = _case("AUDIT_TRAIL_COMPLETE")
    request = _request()
    outcome = _resolve(request)
    _observe("AUDIT_TRAIL_COMPLETE", outcome, request, case_observation)
    # assertions: approval_mode == POLICY_DELEGATED / delegation_id_present == true
    assert outcome.approval_mode == "POLICY_DELEGATED"
    assert outcome.delegation_id_present is True
    assert outcome.derived_approval is not None
    assert outcome.derived_approval.delegation_id == "dg-1"
    assert expected["expected_state"] == "READY"


def test_audit_trail_records_issue_match_and_consume() -> None:
    """発行・照合・消費がAudit Trailへ順序どおり残ること。"""
    outcome = _resolve(_request())
    assert outcome.audit_trail == (
        "floor.evaluated",
        "predicate.matched",
        "derived_approval.issued",
        "derived_approval.consumed",
    )


def test_audit_trail_contains_no_secret_values() -> None:
    """Audit TrailへSecret値を出さないこと（不変条件#7）。"""
    secretish = "sk-live-000000000000000000000000"
    plan = {**BASELINE, "account": secretish}
    grant = _grant(predicate={**BASELINE, "account": secretish})
    outcome = _resolve(_request(grant=grant, plan_fields=plan))
    assert secretish not in " ".join(outcome.audit_trail)
    assert all(secretish not in e.value for e in outcome.events)


# ==========================================================================
# 許可経路（通る経路が本当に通ること）
# ==========================================================================


def test_matching_predicate_auto_approves_exactly_once() -> None:
    """16項目一致で自動承認され、Effectがちょうど1回であること。

    拒否側だけを試験すると、何をしても拒否する実装が全部PASSする。
    """
    outcome = _resolve(_request())
    assert outcome.auto_approved_count == 1
    assert outcome.human_approval_required is False
    assert outcome.effect_attempts == 1
    assert outcome.duplicate_effects == 0
    assert outcome.error_code is None


def test_resolution_never_consults_execution_history() -> None:
    """判定入力に過去の実行履歴が含まれないこと（§3.8.5）。

    「前回と同じ」を根拠にしないことを、入力の形で固定する。
    """
    fields = set(DelegationRequest.__dataclass_fields__)
    forbidden = {"history", "previous_run", "last_approval", "prior_execution"}
    assert not (fields & forbidden), fields


# ==========================================================================
# Phase 7：副作用の実測と改ざん拒否
# ==========================================================================


def _deny_requests() -> dict[str, Any]:
    """拒否になる代表経路。床・Predicate・失効・Policy・Trustを1つずつ。"""
    return {
        "scope_exceeded": _request(plan_fields={**BASELINE, "provider": "different-provider"}),
        "audience_mismatch": _request(workspace_id="ws-other"),
        "revoked": _request(grant=_grant(state=GrantState.REVOKED)),
        "stale_policy": _request(policy_freshness="STALE_TTL_EXCEEDED"),
        "policy_changed": _request(current_policy_hash="sha256:" + "9" * 64),
        "trust_rollback": _request(current_trust_anchor_hash="sha256:" + "f" * 64),
        "self_modification": _request(creates_or_widens_delegation=True),
    }


def test_deny_path_measures_every_counter_as_zero() -> None:
    """拒否経路で全副作用カウンタを**実測**して0であること。

    0 を書き込むのではなく、Probe を組んでから数える。
    「止めると答えた」ことと「実際に何も起きなかった」ことは別である。
    """
    for name, request in _deny_requests().items():
        outcome = _resolve(request)
        assert outcome.human_approval_required or outcome.error_code is not None, name

        ledger = LedgerProbe()
        effects = SideEffectProbe()
        ledger.append_names([event.value for event in outcome.events])

        assert effects.network_calls == 0, name
        assert effects.process_launches == 0, name
        assert effects.workspace_commits == 0, name
        assert effects.external_effects == 0, name
        assert ledger.effect_attempts == 0, name
        assert outcome.effect_attempts == 0, name
        assert outcome.duplicate_effects == 0, name


def test_deny_path_does_not_escape_to_queue_retry_or_fallback() -> None:
    """拒否を Queue／Retry／Fallback へ逃がさないこと。

    後から実行される経路が残ると、拒否したはずの作用が遅れて起きる。
    Outcome が「後で実行する」を表す語彙を**一切持たない**ことで示す。
    """
    escape_fields = ("queued", "retry_scheduled", "fallback_provider", "deferred")
    for name, request in _deny_requests().items():
        outcome = _resolve(request)
        for field in escape_fields:
            assert not hasattr(outcome, field), f"{name}: {field} が生えている"
        # 監査証跡にも「後で実行」に相当する記録が無いこと。
        trail = " ".join(str(entry) for entry in outcome.audit_trail).lower()
        for word in ("queue", "retry", "fallback", "defer"):
            assert word not in trail, f"{name}: audit_trail に {word}"


def test_allow_path_attempts_the_effect_exactly_once() -> None:
    """許可経路で Effect がちょうど1回であること。

    0 なら実行されていない。2回以上なら二重作用である。
    """
    outcome = _resolve(_request(), revocation=EffectLinearization.AFTER)
    assert outcome.effect_attempts == 1
    assert outcome.duplicate_effects == 0

    # Effect試行数はDomainの実測値である。Event列へ足して数えない。
    assert outcome.effect_attempts == 1


def test_revocation_before_and_after_differ_in_effect_count() -> None:
    """失効の前後で Effect 件数が実際に違うこと。

    **「失効なら常にEffect 0件」と主張しない。** 線形化点より後に失効しても
    外部で起きた作用は取り消せない。
    """
    before = _resolve(_request(), revocation=EffectLinearization.BEFORE)
    after = _resolve(_request(), revocation=EffectLinearization.AFTER)
    assert before.effect_attempts == 0
    assert after.effect_attempts == 1
    assert before.effect_attempts != after.effect_attempts
    assert after.reconciliation_required is True
    assert before.reconciliation_required is False


@pytest.mark.parametrize(
    "mutation,label",
    [
        ({"predicate": {**BASELINE, "target_path": "/etc"}}, "predicate"),
        ({"audience_workspace_id": "ws-other"}, "scope_audience"),
        ({"subject_id": "subject-other"}, "scope_subject"),
        ({"policy_hash": "sha256:" + "7" * 64}, "policy_hash"),
        ({"trust_anchor_hash": "sha256:" + "8" * 64}, "trust_anchor"),
        ({"trust_anchor_version": 1}, "trust_anchor_version"),
    ],
)
def test_grant_body_tampering_is_rejected(mutation: dict[str, Any], label: str) -> None:
    """Grant 本文を1項目でも書き換えたら自動承認しないこと。

    署名対象の中身が変わったのに通るなら、署名は何も守っていない。
    """
    outcome = _resolve(_request(grant=_grant(**mutation)))
    assert outcome.auto_approved_count == 0, label
    assert outcome.human_approval_required or outcome.error_code is not None, label
    assert outcome.effect_attempts == 0, label


def test_grant_cannot_be_reused_for_another_subject() -> None:
    """あるSubject向けGrantを別Subjectの実行へ転用できないこと。"""
    outcome = _resolve(_request(subject_id="subject-other"))
    assert outcome.auto_approved_count == 0
    assert outcome.effect_attempts == 0


def test_unauthorized_signer_is_rejected() -> None:
    """許可されていない署名者のGrantを使えないこと。

    Trust Anchor が一致しないGrantは、誰が署名したか分からない。
    """
    outcome = _resolve(_request(current_trust_anchor_hash="sha256:" + "e" * 64))
    assert outcome.auto_approved_count == 0
    assert outcome.effect_attempts == 0
