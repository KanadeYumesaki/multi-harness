"""`AT-POLICY-STALE-001` 5 Case の Domain 試験（§3.8）。

期待値は `design-source/registries/tests.yaml` の当該行から**読み取って**突き合わせる。
Case期待値を試験側へ手入力しない（不変条件#18）。手入力すると、Registryが動いても
試験が古い期待値を主張し続け、正本との乖離に気付けない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from harness.domain._registry_generated import ErrorCode, EventType
from harness.domain.errors import HarnessError
from harness.domain.policy_freshness import (
    EffectKind,
    PolicyFreshnessVerdict,
    evaluate_policy_freshness,
    require_fresh_policy,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
TESTS_YAML = REPO_ROOT / "design-source" / "registries" / "tests.yaml"

EXPIRED_AT = "2026-08-17T00:00:00Z"
NOW_AFTER = "2026-08-17T00:00:01Z"
NOW_BEFORE = "2026-08-16T23:59:59Z"

# Case ID と、その Case が対象にする作用。
CASE_EFFECT: dict[str, EffectKind] = {
    "NEW_LOCAL_READ": EffectKind.LOCAL_READ,
    "NEW_WORKSPACE_WRITE": EffectKind.WORKSPACE_WRITE,
    "NEW_EXTERNAL_EFFECT": EffectKind.EXTERNAL_SEND,
    "NEW_PAID": EffectKind.PAID_EXECUTION,
}


def _case(case_id: str) -> dict[str, Any]:
    rows = yaml.safe_load(TESTS_YAML.read_text(encoding="utf-8"))["test_cases"]
    for row in rows:
        if row["test_id"] == "AT-POLICY-STALE-001" and row["case_id"] == case_id:
            return dict(row)
    raise AssertionError(f"Registryに AT-POLICY-STALE-001/{case_id} が無い")


@pytest.mark.parametrize("case_id", sorted(CASE_EFFECT))
def test_stale_policy_blocks_new_effects(case_id: str) -> None:
    """期限切れPolicyで新しい作用を起こさない。

    State・Error Code・Event列をRegistryの期待値と突き合わせる。
    """
    expected = _case(case_id)
    decision = evaluate_policy_freshness(
        effect_kind=CASE_EFFECT[case_id],
        policy_expires_at=EXPIRED_AT,
        now=NOW_AFTER,
        effect_attempted=False,
    )
    assert decision.verdict is PolicyFreshnessVerdict.BLOCKED_STALE
    assert decision.attempt_state == expected["expected_state"]
    assert decision.error_code is not None
    assert decision.error_code.value == expected["expected_error_code"]
    assert [e.value for e in decision.events] == expected["expected_event_sequence"]
    assert decision.fault_point == expected["fault_point"]


@pytest.mark.parametrize("case_id", sorted(CASE_EFFECT))
def test_blocked_effects_permit_nothing(case_id: str) -> None:
    """止めたときは新規作用も照合も許さないこと。

    Case の assertions（`network_calls == 0` 等）が成り立つ前提は、
    Gateが「何も許さない」と答えることである。
    """
    decision = evaluate_policy_freshness(
        effect_kind=CASE_EFFECT[case_id],
        policy_expires_at=EXPIRED_AT,
        now=NOW_AFTER,
        effect_attempted=False,
    )
    assert decision.new_effect_allowed is False
    assert decision.reconciliation_allowed is False
    assert decision.blocked is True


def test_inflight_effect_allows_reconciliation_only() -> None:
    """既に作用を起こした可能性がある場合は照合だけを許す。

    放置すると外部で起きたかもしれない作用が未照合のまま残る。
    照合は新しい作用ではなく、起きたことの観測である。
    """
    expected = _case("INFLIGHT_ATTEMPTED")
    decision = evaluate_policy_freshness(
        effect_kind=EffectKind.EXTERNAL_SEND,
        policy_expires_at=EXPIRED_AT,
        now=NOW_AFTER,
        effect_attempted=True,
    )
    assert decision.verdict is PolicyFreshnessVerdict.RECOVERY_ONLY
    assert decision.attempt_state == expected["expected_state"]
    assert decision.error_code is not None
    assert decision.error_code.value == expected["expected_error_code"]
    assert [e.value for e in decision.events] == expected["expected_event_sequence"]
    assert decision.fault_point == expected["fault_point"]
    # 照合は許すが、新しい作用（再試行・Fallback）は許さない。
    assert decision.new_effect_allowed is False
    assert decision.reconciliation_allowed is True


@pytest.mark.parametrize("effect_kind", list(EffectKind))
def test_inflight_verdict_does_not_depend_on_effect_kind(effect_kind: EffectKind) -> None:
    """In-flightの扱いは作用の種類で変わらないこと。

    既に起きたかもしれないものを、種類によって放置してよい理由は無い。
    """
    decision = evaluate_policy_freshness(
        effect_kind=effect_kind,
        policy_expires_at=EXPIRED_AT,
        now=NOW_AFTER,
        effect_attempted=True,
    )
    assert decision.verdict is PolicyFreshnessVerdict.RECOVERY_ONLY
    assert decision.error_code is ErrorCode.EFFECT_UNKNOWN


@pytest.mark.parametrize("effect_kind", list(EffectKind))
def test_fresh_policy_permits_the_effect(effect_kind: EffectKind) -> None:
    """期限内なら止めないこと。Gateが常に止めるだけでは検査にならない。"""
    decision = evaluate_policy_freshness(
        effect_kind=effect_kind,
        policy_expires_at=EXPIRED_AT,
        now=NOW_BEFORE,
        effect_attempted=False,
    )
    assert decision.verdict is PolicyFreshnessVerdict.FRESH
    assert decision.new_effect_allowed is True
    assert decision.events == ()
    assert decision.error_code is None
    assert decision.blocked is False


def test_expiry_boundary_is_exclusive() -> None:
    """期限ちょうどは失効として扱うこと。

    境界で「まだ有効」に倒すと、期限切れPolicyで1回だけ作用が通る窓ができる。
    """
    at_expiry = evaluate_policy_freshness(
        effect_kind=EffectKind.EXTERNAL_SEND,
        policy_expires_at=EXPIRED_AT,
        now=EXPIRED_AT,
        effect_attempted=False,
    )
    assert at_expiry.verdict is PolicyFreshnessVerdict.BLOCKED_STALE


@pytest.mark.parametrize("case_id", sorted(CASE_EFFECT))
def test_each_effect_kind_has_a_distinct_error_code(case_id: str) -> None:
    """作用ごとにError Codeを分けること。どこで止まったかが後から分かる。"""
    codes = {
        kind: evaluate_policy_freshness(
            effect_kind=kind,
            policy_expires_at=EXPIRED_AT,
            now=NOW_AFTER,
            effect_attempted=False,
        ).error_code
        for kind in EffectKind
    }
    assert len(set(codes.values())) == len(EffectKind), codes


def test_require_fresh_policy_raises_instead_of_returning_silently() -> None:
    """判定を無視して先へ進む経路を作らないこと。"""
    with pytest.raises(HarnessError) as excinfo:
        require_fresh_policy(
            effect_kind=EffectKind.PAID_EXECUTION,
            policy_expires_at=EXPIRED_AT,
            now=NOW_AFTER,
        )
    assert excinfo.value.code is ErrorCode.POLICY_STALE_PAID_BLOCKED


def test_require_fresh_policy_returns_for_reconciliation() -> None:
    """照合だけ許す場合は例外にしないこと。照合まで止めると未照合が残る。"""
    decision = require_fresh_policy(
        effect_kind=EffectKind.EXTERNAL_SEND,
        policy_expires_at=EXPIRED_AT,
        now=NOW_AFTER,
        effect_attempted=True,
    )
    assert decision.verdict is PolicyFreshnessVerdict.RECOVERY_ONLY


def test_only_registry_vocabulary_is_used() -> None:
    """Registryに無いEvent／Error Codeを作っていないこと。"""
    for effect_attempted in (False, True):
        for kind in EffectKind:
            decision = evaluate_policy_freshness(
                effect_kind=kind,
                policy_expires_at=EXPIRED_AT,
                now=NOW_AFTER,
                effect_attempted=effect_attempted,
            )
            for event in decision.events:
                assert isinstance(event, EventType)
            if decision.error_code is not None:
                assert isinstance(decision.error_code, ErrorCode)
