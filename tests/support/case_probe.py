"""Case観測の共通経路。全系統（unit/domain・SQLite・filesystem）で同じ突合をする。

## なぜ1箇所へ寄せるか

Batch ごとに突合を書くと、強さがばらつく。実際、Masking Batch では
観測helperが State と Event 列しか見ておらず **Evidence 層より弱かった**ため、
`error_code` の食い違いが Evidence 生成段まで生き延びた。

ここでは Evidence 層（`emit_case_evidence`）と**同じ3項目**を突合する。

* `observed_state` == `expected_state`
* `observed_error_code` == `expected_error_code`
* `observed_event_sequence` == `expected_event_sequence`

**完全一致だけを認める。** 先頭一致・部分一致・`any(...)` は使わない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from ledger_probe import LedgerProbe, SideEffectProbe, record_case

__all__ = ["observe_case", "observe_unit_case", "registry_case"]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REGISTRIES = _REPO_ROOT / "design-source" / "registries"
_CACHE: dict[str, dict[str, Any]] = {}


def registry_case(case_id: str) -> dict[str, Any]:
    """`AT-XXX-001/CASE_ID` 形式で Registry の Case 定義を引く。"""
    if not _CACHE:
        rows = yaml.safe_load((_REGISTRIES / "tests.yaml").read_text(encoding="utf-8"))
        for row in rows["test_cases"]:
            _CACHE[f"{row['test_id']}/{row['case_id']}"] = dict(row)
    if case_id not in _CACHE:
        raise AssertionError(f"Registryに {case_id} が無い")
    return _CACHE[case_id]


def observe_case(
    observation: Any,
    case_id: str,
    *,
    state: str,
    subject_id: str,
    payload: dict[str, Any],
    error_code: str | None = None,
    ledger: LedgerProbe | None = None,
    effects: SideEffectProbe | None = None,
    head_before: int | None = None,
) -> None:
    """観測値を記録し、Registry 期待値と**完全一致**で突合する。

    `ledger` を渡さない Case は、この関数が新しい Probe を作って観測する。
    Append が起きない Case でも **Ledger を実際に読む**。観測せずに空配列を
    書くのは「見ていない」を「見て0件」と偽ることになる（設計書§19.1.1）。

    Probe は Case ごとに新しく作る。共有すると他CaseのEventが混ざる。
    """
    probe = ledger if ledger is not None else LedgerProbe()
    counters = effects if effects is not None else SideEffectProbe()
    start = probe.head if head_before is None else head_before

    observation.record_input(payload)
    record_case(
        observation,
        state=state,
        subject_id=subject_id,
        ledger=probe,
        head_before=start,
        effects=counters,
        error_code=error_code,
    )

    expected = registry_case(case_id)
    assert observation.observed_state == expected["expected_state"], case_id
    assert observation.observed_error_code == expected["expected_error_code"], case_id
    assert list(observation.observed_events) == (expected["expected_event_sequence"] or []), case_id
    # Head は Probe の実測値と一致する。record_case が Head と列の整合を別途見る。
    assert observation.ledger_head_after == probe.head, case_id
    assert observation.ledger_head_before == start, case_id


def observe_unit_case(
    observation: Any,
    case_id: str,
    *,
    state: str,
    subject_id: str,
    payload: dict[str, Any],
    error_code: str | None = None,
    effects: SideEffectProbe | None = None,
    ledger_effect_attempts: int | None = None,
) -> None:
    """Unit 層の Case を、**Ledger を観測せずに** 記録する。

    `observe_case` との違いは1点だけである。あちらは必ず Ledger を読んで
    「見て0件だった」ことを実測する。こちらは**読まない**。

    読まないことを Evidence へ正直に残すために、`observe_ledger()` を呼ばない。
    その結果 `ledger_observed` は `false` のままになり、Emitter は
    `event_observation=NOT_APPLICABLE` と `event_sequence=null` を書く。
    **空配列で埋めない。** 見ていないことを「見て0件」と偽らない（§19.1.1）。

    免除は Registry 正本が `NOT_APPLICABLE` と宣言した Case に限る。宣言の無い
    Case でこの関数を呼んだら止める。呼び分けの誤りで Ledger 検証が黙って
    外れるのを防ぐ（Owner Decision DCR-5 / E1）。
    """
    expected = registry_case(case_id)
    policy = expected.get("event_observation_policy")
    if policy != "NOT_APPLICABLE":
        raise AssertionError(
            f"{case_id}: event_observation_policy={policy!r} は NOT_APPLICABLE でない。"
            "Ledger 観測を要求する Case へ observe_unit_case を使わない"
        )
    if expected["expected_event_sequence"]:
        raise AssertionError(f"{case_id}: 期待Event列が非空である。Unit Case として扱えない")

    observation.record_input(payload)
    observation.record(state=state, subject_id=subject_id, error_code=error_code)
    # 実行後に作った未接続Probeの0は観測ではない。観測元が無い場合は
    # side_effects=Noneを保ち、Emitter/Runnerが未測定として拒否する。
    # Ledgerを読まないUnitでも、Effect試行数を0で補完しない。
    if effects is not None and ledger_effect_attempts is not None:
        values = (
            effects.network_calls,
            effects.process_launches,
            effects.workspace_commits,
            effects.external_effects,
            ledger_effect_attempts,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("invalid measured side-effect counter")
        observation.observe_side_effects(
            network_calls=effects.network_calls,
            process_launches=effects.process_launches,
            workspace_commits=effects.workspace_commits,
            external_effects=effects.external_effects,
            ledger_effect_attempts=ledger_effect_attempts,
        )

    assert observation.observed_state == expected["expected_state"], case_id
    assert observation.observed_error_code == expected["expected_error_code"], case_id
    # 見ていないことが記録に残っていること。
    assert list(observation.observed_events) == [], case_id
    assert observation.as_dict()["ledger_observed"] is False, case_id
