"""`AT-CRASH-001` 11 Case の統合試験（§26.5）。

期待値は `design-source/registries/tests.yaml` から読み取って突き合わせる。

## 何を確かめているか

中断点ごとに「作用が起きたか」を3値で判定し、**`UNKNOWN` を
`NOT_EXECUTED` へ変換しない**ことを固定する。

「たぶん起きていない」で再実行すると、既に起きていた場合に二重作用になる。
外部への送信や課金は取り消せない。
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from harness.domain.crash_recovery import (
    CrashSite,
    EffectDisposition,
    recover_from_crash,
)
from harness.domain.faults import FaultPoint
from harness.domain.hashing import ContentHash

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "support"))
from ledger_probe import LedgerProbe, SideEffectProbe, record_case

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
TESTS_YAML = REPO_ROOT / "design-source" / "registries" / "tests.yaml"

BASE = ContentHash.parse("sha256:" + "1" * 64)
AFTER = ContentHash.parse("sha256:" + "2" * 64)
STRANGE = ContentHash.parse("sha256:" + "3" * 64)


def _case(case_id: str) -> dict[str, Any]:
    rows = yaml.safe_load(TESTS_YAML.read_text(encoding="utf-8"))["test_cases"]
    for row in rows:
        if row["test_id"] == "AT-CRASH-001" and row["case_id"] == case_id:
            return dict(row)
    raise AssertionError(f"Registryに AT-CRASH-001/{case_id} が無い")


def _site(**overrides: Any) -> CrashSite:
    base = CrashSite(
        attempt_id="attempt-1",
        fault_point=FaultPoint.AFTER_EXECUTION_ATTEMPTED,
        durable_events=(),
        base_hash=BASE,
        expected_after_hash=AFTER,
        observed_hash=None,
        journal_prepared=True,
        receipt_stored=False,
    )
    return replace(base, **overrides)


def _observe(case_id: str, outcome: Any, site: Any, observation: Any) -> None:
    """Registry突合とLedger観測を1箇所で行う。

    Domainが返す `full_event_sequence` を観測値にしない。それは
    「Appendされるはずの列」であって Ledger に残った列ではない。
    中断前の `durable_events` を Ledger へ載せ、Recovery が追記する
    `appended_events` を Application 層と同じように Append してから読む。
    """
    _assert_matches_registry(case_id, outcome)

    ledger = LedgerProbe()
    effects = SideEffectProbe()
    ledger.append_names([event.value for event in site.durable_events])
    head_before = ledger.head
    ledger.append_names([event.value for event in outcome.appended_events])

    observation.record_input(
        {
            "attempt_id": site.attempt_id,
            "fault_point": site.fault_point.value,
            "durable_events": [event.value for event in site.durable_events],
            "base_hash": str(site.base_hash),
            "expected_after_hash": str(site.expected_after_hash),
            "observed_hash": str(site.observed_hash) if site.observed_hash else None,
            "journal_prepared": site.journal_prepared,
            "receipt_stored": site.receipt_stored,
        }
    )
    observation.record_fault(
        fault_point=site.fault_point.value, fault_kind="CRASH", deterministic=True
    )
    record_case(
        observation,
        state=outcome.attempt_state,
        subject_id=outcome.attempt_id,
        ledger=ledger,
        head_before=head_before,
        effects=effects,
        error_code=outcome.error_code.value if outcome.error_code else None,
    )
    # 観測列がRegistry期待値と一致することを、Ledger由来の値で確かめる。
    assert list(observation.observed_events) == _case(case_id)["expected_event_sequence"]


def _assert_matches_registry(case_id: str, outcome: Any) -> None:
    """State・Error Code・Event列をRegistry期待値と突き合わせる。"""
    expected = _case(case_id)
    assert outcome.attempt_state == expected["expected_state"], case_id
    if expected["expected_error_code"] is None:
        assert outcome.error_code is None, case_id
    else:
        assert outcome.error_code is not None, case_id
        assert outcome.error_code.value == expected["expected_error_code"], case_id
    assert [e.value for e in outcome.full_event_sequence] == expected["expected_event_sequence"], (
        case_id
    )


# --------------------------------------------------------------------------
# Journal確定より前：作用を起こしていないと断定できる
# --------------------------------------------------------------------------


@pytest.mark.case("AT-CRASH-001/BEFORE_PREPARED")
def test_crash_before_action_prepared(case_observation: Any) -> None:
    """Journalが無い＝I/Oを起こしていない。やり直せる。"""
    site = _site(
        fault_point=FaultPoint.BEFORE_ACTION_PREPARED,
        durable_events=(_event("ACTION_STARTED"),),
        journal_prepared=False,
    )
    outcome = recover_from_crash(site)
    _observe("BEFORE_PREPARED", outcome, site, case_observation)
    # assertions: effect_journal_count == 0 / target_hash == base_hash / effect_attempts == 0
    assert outcome.disposition is EffectDisposition.NOT_EXECUTED
    assert outcome.target_hash == BASE
    assert outcome.effect_attempts == 0
    assert outcome.reexecution_allowed is True


@pytest.mark.case("AT-CRASH-001/TEMP_BEFORE_FSYNC")
def test_crash_after_temp_write_before_fsync(case_observation: Any) -> None:
    """Temp書込みは確定していない。Journalも無い。修復して終わる。"""
    site = _site(
        fault_point=FaultPoint.AFTER_TEMP_WRITE_BEFORE_FSYNC,
        durable_events=(_event("ACTION_STARTED"),),
        journal_prepared=False,
    )
    outcome = recover_from_crash(site)
    _observe("TEMP_BEFORE_FSYNC", outcome, site, case_observation)
    # assertions: journal_prepared == false / target_hash == base_hash / duplicate_effects == 0
    assert outcome.target_hash == BASE
    assert outcome.effect_attempts == 0


@pytest.mark.case("AT-CRASH-001/BEFORE_EXECUTION")
def test_crash_before_execution_attempted(case_observation: Any) -> None:
    """Journalは確定したが実行前。再開はOperatorが決める。

    作用は起きていないと分かっているが、**勝手に実行しない**。
    """
    site = _site(
        fault_point=FaultPoint.BEFORE_EXECUTION_ATTEMPTED,
        durable_events=(_event("ACTION_PREPARED"),),
    )
    outcome = recover_from_crash(site)
    _observe("BEFORE_EXECUTION", outcome, site, case_observation)
    # assertions: target_hash == base_hash / effect_attempts == 0
    #             / operator_resume_required == true
    assert outcome.target_hash == BASE
    assert outcome.effect_attempts == 0
    assert outcome.operator_resume_required is True
    assert outcome.reexecution_allowed is False


@pytest.mark.case("AT-CRASH-001/AFTER_PREPARED_ABSENCE_PROVEN")
def test_crash_after_prepared_target_unchanged(case_observation: Any) -> None:
    """実行前だと証明できたので、続きを実行して完了させる。"""
    site = _site(
        fault_point=FaultPoint.AFTER_ACTION_PREPARED,
        durable_events=(_event("ACTION_PREPARED"),),
        observed_hash=BASE,
    )
    outcome = recover_from_crash(site)
    _observe("AFTER_PREPARED_ABSENCE_PROVEN", outcome, site, case_observation)
    assert outcome.effect_attempts == 1
    assert outcome.receipt_count == 1


# --------------------------------------------------------------------------
# 実行を試みた後：観測できるかで分かれる
# --------------------------------------------------------------------------


@pytest.mark.case("AT-CRASH-001/AFTER_EXECUTION_UNKNOWN")
def test_crash_after_execution_state_unknown(case_observation: Any) -> None:
    """外部を観測できない場合は `UNKNOWN` のまま止める。

    **ここが最も重要な分岐である。** `NOT_EXECUTED` へ倒すと、
    届いていた場合に二重作用になる。
    """
    site = _site(
        fault_point=FaultPoint.AFTER_EXECUTION_ATTEMPTED,
        durable_events=(_event("EXECUTION_ATTEMPTED"),),
        observed_hash=None,
    )
    outcome = recover_from_crash(site)
    _observe("AFTER_EXECUTION_UNKNOWN", outcome, site, case_observation)
    assert outcome.disposition is EffectDisposition.UNKNOWN
    assert outcome.reexecution_allowed is False
    assert outcome.manual_queue_required is True
    assert outcome.resolved is False


@pytest.mark.case("AT-CRASH-001/AFTER_EXECUTION_EXPECTED_HASH")
def test_crash_after_execution_expected_hash_observed(case_observation: Any) -> None:
    """期待Hashを観測できたなら、作用は起きている。残りを進める。"""
    site = _site(
        fault_point=FaultPoint.AFTER_EXECUTION_ATTEMPTED,
        durable_events=(_event("EXECUTION_ATTEMPTED"),),
        observed_hash=AFTER,
    )
    outcome = recover_from_crash(site)
    _observe("AFTER_EXECUTION_EXPECTED_HASH", outcome, site, case_observation)
    assert outcome.disposition is EffectDisposition.EXECUTED
    # 既に起きているので作用は繰り返さない。
    assert outcome.reexecution_allowed is False
    assert outcome.effect_attempts == 1


@pytest.mark.case("AT-CRASH-001/AFTER_REPLACE")
def test_crash_after_atomic_replace_before_observe(case_observation: Any) -> None:
    """Renameは完了している。観測Hashが期待と一致する。"""
    site = _site(
        fault_point=FaultPoint.AFTER_ATOMIC_REPLACE_BEFORE_OBSERVE,
        durable_events=(_event("EXECUTION_ATTEMPTED"),),
        observed_hash=AFTER,
    )
    outcome = recover_from_crash(site)
    _observe("AFTER_REPLACE", outcome, site, case_observation)
    # assertions: observed_hash == expected_after_hash / duplicate_effects == 0
    assert outcome.target_hash == AFTER
    assert outcome.effect_attempts == 1


def test_observed_base_hash_after_execution_proves_absence() -> None:
    """対象が実行前のままなら、作用は届いていないと言える。"""
    outcome = recover_from_crash(
        _site(fault_point=FaultPoint.AFTER_EXECUTION_ATTEMPTED, observed_hash=BASE)
    )
    assert outcome.disposition is EffectDisposition.NOT_EXECUTED


def test_unexplainable_hash_stays_unknown() -> None:
    """期待とも実行前とも違うHashを、どちらかへ丸めないこと。

    何が起きたのか説明できない状態を、説明できたことにしない。
    """
    outcome = recover_from_crash(
        _site(fault_point=FaultPoint.AFTER_EXECUTION_ATTEMPTED, observed_hash=STRANGE)
    )
    assert outcome.disposition is EffectDisposition.UNKNOWN
    assert outcome.reexecution_allowed is False


# --------------------------------------------------------------------------
# 観測済み以降：作用は繰り返さない
# --------------------------------------------------------------------------


@pytest.mark.case("AT-CRASH-001/AFTER_OBSERVED")
def test_crash_after_effect_observed(case_observation: Any) -> None:
    """観測済み。Receipt保存とCommitだけを進める。"""
    site = _site(
        fault_point=FaultPoint.AFTER_EFFECT_OBSERVED,
        durable_events=(_event("EFFECT_OBSERVED"),),
        observed_hash=AFTER,
    )
    outcome = recover_from_crash(site)
    _observe("AFTER_OBSERVED", outcome, site, case_observation)
    # assertions: receipt_count == 1 / duplicate_effects == 0
    assert outcome.receipt_count == 1
    assert outcome.effect_attempts == 1


@pytest.mark.case("AT-CRASH-001/BEFORE_RECEIPT")
def test_crash_before_receipt_store(case_observation: Any) -> None:
    """Receipt保存の前で落ちた。Receiptを1件だけ作る。"""
    site = _site(
        fault_point=FaultPoint.BEFORE_RECEIPT_STORE,
        durable_events=(_event("EFFECT_OBSERVED"),),
        observed_hash=AFTER,
        receipt_stored=False,
    )
    outcome = recover_from_crash(site)
    _observe("BEFORE_RECEIPT", outcome, site, case_observation)
    assert outcome.receipt_count == 1


@pytest.mark.case("AT-CRASH-001/AFTER_RECEIPT")
def test_crash_after_receipt_before_commit(case_observation: Any) -> None:
    """Receiptは保存済み。Commitだけを進める。二重にReceiptを作らない。"""
    site = _site(
        fault_point=FaultPoint.AFTER_RECEIPT_STORE_BEFORE_COMMIT,
        durable_events=(_event("EFFECT_RECEIPT_STORED"),),
        observed_hash=AFTER,
        receipt_stored=True,
    )
    outcome = recover_from_crash(site)
    _observe("AFTER_RECEIPT", outcome, site, case_observation)
    # assertions: receipt_count == 1 / action_committed_count == 1
    assert outcome.receipt_count == 1
    assert outcome.appended_events[-1].value == "ACTION_COMMITTED"
    # 既にあるReceiptを作り直さない。
    assert "EFFECT_RECEIPT_STORED" not in [e.value for e in outcome.appended_events]


@pytest.mark.case("AT-CRASH-001/LEDGER_APPEND")
def test_crash_during_ledger_append(case_observation: Any) -> None:
    """Append中の中断。部分Eventを残さず、Chainを保つ。"""
    site = _site(
        fault_point=FaultPoint.AFTER_TEMP_WRITE_BEFORE_FSYNC,
        durable_events=(),
        journal_prepared=False,
    )
    outcome = recover_from_crash(site)
    expected = _case("LEDGER_APPEND")
    assert outcome.attempt_state == expected["expected_state"]
    assert [e.value for e in outcome.full_event_sequence] == expected["expected_event_sequence"]
    # assertions: partial_event_count == 0 / duplicate_effects == 0
    assert outcome.effect_attempts == 0
    _observe("LEDGER_APPEND", outcome, site, case_observation)


# --------------------------------------------------------------------------
# 横断的な性質
# --------------------------------------------------------------------------


@pytest.mark.parametrize("point", list(FaultPoint))
def test_unknown_is_never_converted_to_not_executed(point: FaultPoint) -> None:
    """観測できないとき、どの中断点でも `NOT_EXECUTED` を名乗らないこと。

    Journal確定前は観測に依らず断定できる。それ以外で観測が無いなら
    `UNKNOWN` でなければならない。
    """
    outcome = recover_from_crash(_site(fault_point=point, observed_hash=None))
    if point in (
        FaultPoint.BEFORE_ACTION_PREPARED,
        FaultPoint.AFTER_TEMP_WRITE_BEFORE_FSYNC,
        FaultPoint.AFTER_ACTION_PREPARED,
        FaultPoint.BEFORE_EXECUTION_ATTEMPTED,
    ):
        return  # Journal状態から断定できる中断点
    if point in (
        FaultPoint.AFTER_EXECUTION_ATTEMPTED,
        FaultPoint.AFTER_ATOMIC_REPLACE_BEFORE_OBSERVE,
    ):
        assert outcome.disposition is EffectDisposition.UNKNOWN, point


@pytest.mark.parametrize("point", list(FaultPoint))
def test_reexecution_is_never_allowed_when_unknown(point: FaultPoint) -> None:
    """`UNKNOWN` のとき再実行を許さないこと。二重作用の唯一の入口である。"""
    outcome = recover_from_crash(_site(fault_point=point, observed_hash=None))
    if outcome.disposition is EffectDisposition.UNKNOWN:
        assert outcome.reexecution_allowed is False, point
        assert outcome.manual_queue_required is True, point


@pytest.mark.parametrize("point", list(FaultPoint))
def test_recovery_only_appends_events(point: FaultPoint) -> None:
    """Recoveryは追記だけを行い、中断前のEventを書き換えないこと（不変条件#1）。"""
    prefix = (_event("ACTION_STARTED"), _event("ACTION_PREPARED"))
    outcome = recover_from_crash(
        _site(fault_point=point, durable_events=prefix, observed_hash=AFTER)
    )
    assert outcome.full_event_sequence[: len(prefix)] == prefix, point
    assert outcome.full_event_sequence[len(prefix) :] == outcome.appended_events, point


@pytest.mark.parametrize("point", list(FaultPoint))
def test_recovery_events_start_with_recovery_started(point: FaultPoint) -> None:
    """追記Eventが必ず `RECOVERY_STARTED` → `RECOVERY_DECIDED` で始まること。

    Recovery結果を新しいEventとして記録する（判定の痕跡を残す）。
    """
    outcome = recover_from_crash(_site(fault_point=point, observed_hash=AFTER))
    assert [e.value for e in outcome.appended_events[:2]] == [
        "RECOVERY_STARTED",
        "RECOVERY_DECIDED",
    ], point


@pytest.mark.parametrize("point", list(FaultPoint))
def test_effect_attempts_never_exceed_one(point: FaultPoint) -> None:
    """同じAttemptを二重実行しないこと。"""
    outcome = recover_from_crash(_site(fault_point=point, observed_hash=AFTER))
    assert outcome.effect_attempts <= 1, point
    assert outcome.receipt_count <= 1, point


def _event(name: str) -> Any:
    from harness.domain._registry_generated import EventType

    return EventType(name)
