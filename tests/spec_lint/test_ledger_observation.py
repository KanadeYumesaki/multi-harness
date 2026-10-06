"""Ledger観測とAdapter契約の否定系。

ここで固定するのは「観測できていない状態を観測できたことにしない」である。
Adapter が緑になる経路を、壊した入力で1つずつ塞ぐ。

**試験がPASSしただけで Evidence PASS にしない。** 実観測が無い、Ledgerを
見ていない、副作用を測っていない、のいずれでも Evidence 生成は拒否される。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRIES = REPO_ROOT / "design-source" / "registries"

sys.path.insert(0, str(REPO_ROOT / "tests" / "support"))
sys.path.insert(0, str(REPO_ROOT / "tools"))

from case_observation import CaseObservation  # noqa: E402
from ledger_probe import LedgerProbe, SideEffectProbe, record_case  # noqa: E402

from case_runner import CaseRunnerError, PytestCaseAdapter, build_pytest_adapters  # noqa: E402


def _observation(case_id: str = "AT-MIGRATION-001/FRESH_INSTALL") -> CaseObservation:
    return CaseObservation(case_id=case_id, node_id="tests/x.py::test_x")


# ---------------------------------------------------------------- LedgerProbe


def test_probe_keeps_head_and_appended_together() -> None:
    """Append列とHeadが同時に進むこと。"""
    probe = LedgerProbe()
    assert probe.head == 0
    assert probe.appended == []
    probe.append_names(["PLAN_RESOLVED", "POLICY_DECIDED"])
    assert probe.head == 2
    assert probe.appended == ["PLAN_RESOLVED", "POLICY_DECIDED"]


def test_probe_rejects_head_and_list_divergence() -> None:
    """Headと列が食い違ったら読み出しを拒否すること。

    列だけを見ると、拒否されたはずのAppendが実は成功していた場合を見逃す。
    """
    probe = LedgerProbe()
    probe.append_names(["PLAN_RESOLVED"])
    probe._head = 5  # 記録漏れを注入する
    with pytest.raises(AssertionError, match="不整合"):
        _ = probe.appended
    with pytest.raises(AssertionError, match="不整合"):
        _ = probe.head


def test_probe_rejects_cas_mismatch() -> None:
    """Stream headとCASが噛み合わないAppendを拒否すること。"""
    probe = LedgerProbe()
    probe.append_names(["PLAN_RESOLVED"])
    with pytest.raises(AssertionError, match="CAS"):
        probe.append(
            [type("E", (), {"event_type": "POLICY_DECIDED"})()], expected_stream_sequence=0
        )


def test_probe_rejects_empty_append() -> None:
    """Eventの無いAppend要求を黙って受理しないこと。"""
    probe = LedgerProbe()
    with pytest.raises(ValueError, match="空のAppend"):
        probe.append([])


def test_reject_leaves_head_and_list_untouched() -> None:
    """拒否されたAppendはLedgerへ何も残さないこと。"""
    probe = LedgerProbe()
    probe.append_names(["PLAN_RESOLVED"])
    before = probe.head
    probe.reject()
    assert probe.head == before
    assert probe.appended == ["PLAN_RESOLVED"]


def test_separate_probes_do_not_mix_events() -> None:
    """Case ごとに Probe を分ければ Event が混ざらないこと。

    Probe を共有すると他CaseのEventが混ざり、どのCaseの観測か決まらない。
    """
    first, second = LedgerProbe(), LedgerProbe()
    first.append_names(["PLAN_RESOLVED"])
    second.append_names(["POLICY_DECIDED", "ACTION_BLOCKED"])
    assert first.appended == ["PLAN_RESOLVED"]
    assert second.appended == ["POLICY_DECIDED", "ACTION_BLOCKED"]
    assert first.head == 1
    assert second.head == 2


def test_shared_probe_would_mix_events() -> None:
    """共有すると実際に混ざることを示す（分離の必要性の実証）。"""
    shared = LedgerProbe()
    shared.append_names(["PLAN_RESOLVED"])
    shared.append_names(["POLICY_DECIDED"])
    assert shared.appended == ["PLAN_RESOLVED", "POLICY_DECIDED"]


# ------------------------------------------------------------ CaseObservation


def test_record_does_not_accept_an_event_list() -> None:
    """`record(events=...)` を復活させないこと。

    任意の列を渡せる限り、Request列でもVerdict予測列でも期待値でも書けてしまう。
    """
    observation = _observation()
    with pytest.raises(TypeError):
        observation.record(  # type: ignore[call-arg]
            state="ACCEPTED", subject_id="s-1", events=["PLAN_RESOLVED"]
        )


def test_observation_is_incomplete_without_ledger() -> None:
    """Ledgerを観測しなければ complete にならないこと。"""
    observation = _observation()
    observation.record(state="ACCEPTED", subject_id="s-1")
    observation.observe_side_effects(
        network_calls=0,
        process_launches=0,
        workspace_commits=0,
        external_effects=0,
        ledger_effect_attempts=0,
    )
    assert observation.complete is False
    assert observation.as_dict()["ledger_observed"] is False


def test_observation_is_incomplete_without_side_effects() -> None:
    """副作用を測らなければ complete にならないこと。"""
    observation = _observation()
    probe = LedgerProbe()
    observation.record(state="ACCEPTED", subject_id="s-1")
    observation.observe_ledger(probe, head_before=0)
    assert observation.complete is False
    assert observation.as_dict()["side_effects"] is None


def test_zero_append_is_recorded_as_an_actual_observation() -> None:
    """Append 0件でも「観測した」ことが残ること。

    `[]` は「見て0件だった」であり、未観測とは意味が正反対である。
    """
    observation = _observation()
    probe = LedgerProbe()
    record_case(
        observation,
        state="ACCEPTED",
        subject_id="s-1",
        ledger=probe,
        head_before=0,
        effects=SideEffectProbe(),
    )
    body = observation.as_dict()
    assert body["observed_event_sequence"] == []
    assert body["ledger_observed"] is True
    assert body["ledger_head_before"] == 0
    assert body["ledger_head_after"] == 0
    assert observation.complete is True


def test_record_requires_state_and_subject() -> None:
    """Stateと Subject の片方でも欠ければ拒否すること。"""
    with pytest.raises(ValueError, match="observed_state"):
        _observation().record(state="", subject_id="s-1")
    with pytest.raises(ValueError, match="actual_subject_id"):
        _observation().record(state="ACCEPTED", subject_id="")


class _InconsistentLedger:
    """Head と Append 列が食い違う Ledger。

    `LedgerProbe` は自分で整合を保つので、`record_case` の照合は Probe 経由では
    決して発火しない。この照合が守っているのは **Probe 以外の Ledger** である。
    実際のSQLite Ledgerや手書きFakeが来たときに Fail-Closed にする。
    """

    def __init__(self, *, head: int, appended: list[str]) -> None:
        self.head = head
        self.appended = appended
        self.effect_attempts = 0


def test_record_case_rejects_head_grown_without_new_events() -> None:
    """Headが動いたのにEvent列が伸びていない状態を拒否すること。

    途中Eventの欠落がこの形で現れる。Headだけ一致しても通さない。
    """
    with pytest.raises(AssertionError, match="Headと Event列が食い違う"):
        record_case(
            _observation(),
            state="ACCEPTED",
            subject_id="s-1",
            ledger=_InconsistentLedger(head=3, appended=[]),
            head_before=0,
            effects=SideEffectProbe(),
        )


def test_record_case_rejects_events_without_head_movement() -> None:
    """Event列が伸びたのにHeadが動いていない状態を拒否すること。"""
    with pytest.raises(AssertionError, match="Headと Event列が食い違う"):
        record_case(
            _observation(),
            state="ACCEPTED",
            subject_id="s-1",
            ledger=_InconsistentLedger(head=0, appended=["PLAN_RESOLVED", "POLICY_DECIDED"]),
            head_before=0,
            effects=SideEffectProbe(),
        )


def test_record_case_accepts_a_consistent_ledger() -> None:
    """整合しているLedgerは通ること。否定系だけでなく肯定系も固定する。"""
    observation = _observation()
    probe = LedgerProbe()
    probe.append_names(["PLAN_RESOLVED"])
    head_before = probe.head
    probe.append_names(["POLICY_DECIDED"])
    record_case(
        observation,
        state="ACCEPTED",
        subject_id="s-1",
        ledger=probe,
        head_before=head_before,
        effects=SideEffectProbe(),
    )
    assert observation.observed_events == ("PLAN_RESOLVED", "POLICY_DECIDED")
    assert observation.ledger_head_before == 1
    assert observation.ledger_head_after == 2


# ------------------------------------------------------------------- Adapter


def test_adapter_rejects_unobserved_ledger(tmp_path: Path) -> None:
    """Ledger未観測の記録から Evidence を作らないこと。"""
    adapter = PytestCaseAdapter(
        case_id="AT-MIGRATION-001/FRESH_INSTALL",
        node_ids=("tests/x.py::test_x",),
        repo_root=REPO_ROOT,
        fixtures_dir=tmp_path,
    )
    records = [
        {
            "case_id": "AT-MIGRATION-001/FRESH_INSTALL",
            "recorded": True,
            "ledger_observed": False,
            "observed_state": "ACCEPTED",
            "observed_error_code": None,
            "observed_event_sequence": [],
            "actual_subject_id": "mr-1",
            "side_effects": {
                "network_calls": 0,
                "process_launches": 0,
                "workspace_commits": 0,
                "external_effects": 0,
                "ledger_effect_attempts": 0,
            },
            "input_payload": {"x": 1},
        }
    ]
    with pytest.raises(CaseRunnerError, match="LEDGER_NOT_OBSERVED"):
        adapter._merge(records)


def test_adapter_rejects_conflicting_observations(tmp_path: Path) -> None:
    """同一Caseで観測が食い違ったら、どちらが正かを実装側で選ばないこと。"""
    adapter = PytestCaseAdapter(
        case_id="AT-MIGRATION-001/FRESH_INSTALL",
        node_ids=("tests/x.py::test_x", "tests/y.py::test_y"),
        repo_root=REPO_ROOT,
        fixtures_dir=tmp_path,
    )
    base = {
        "case_id": "AT-MIGRATION-001/FRESH_INSTALL",
        "recorded": True,
        "ledger_observed": True,
        "observed_state": "ACCEPTED",
        "observed_error_code": None,
        "observed_event_sequence": [],
        "actual_subject_id": "mr-1",
        "side_effects": {
            "network_calls": 0,
            "process_launches": 0,
            "workspace_commits": 0,
            "external_effects": 0,
            "ledger_effect_attempts": 0,
        },
        "input_payload": {"x": 1},
        "ledger_head_before": 0,
        "ledger_head_after": 0,
    }
    other = dict(base, observed_state="REJECTED")
    with pytest.raises(CaseRunnerError, match="AMBIGUOUS_OBSERVATION"):
        adapter._merge([base, other])


def test_adapter_rejects_missing_input_fixture(tmp_path: Path) -> None:
    """Input Fixture が無ければ Evidence を作らないこと。"""
    adapter = PytestCaseAdapter(
        case_id="AT-MIGRATION-001/FRESH_INSTALL",
        node_ids=("tests/x.py::test_x",),
        repo_root=REPO_ROOT,
        fixtures_dir=tmp_path,
    )
    with pytest.raises(CaseRunnerError, match="INPUT_FIXTURE_MISSING"):
        adapter._write_fixture({"input_payload": None})


def test_adapter_rejects_a_result_for_another_case(tmp_path: Path) -> None:
    """Adapterが別Caseの結果を返したら止めること。"""
    from case_runner import _validate_result
    from emit_case_evidence import CaseRunResult, SideEffectObservation

    result = CaseRunResult(
        case_id="AT-MIGRATION-001/BACKUP_RESTORE",
        gate_ids=(),
        input_fixture_path=tmp_path / "f.json",
        observed_state="ACCEPTED",
        observed_error_code=None,
        observed_events=(),
        side_effects=SideEffectObservation(0, 0, 0, 0, 0),
        actual_subject_id="br-1",
    )
    with pytest.raises(CaseRunnerError, match="ADAPTER_CASE_MISMATCH"):
        _validate_result("AT-MIGRATION-001/FRESH_INSTALL", result)


def test_adapter_rejects_unmeasured_side_effects(tmp_path: Path) -> None:
    """副作用未測定を0へ変換しないこと。"""
    from case_runner import _validate_result
    from emit_case_evidence import CaseRunResult

    result = CaseRunResult(
        case_id="AT-MIGRATION-001/FRESH_INSTALL",
        gate_ids=(),
        input_fixture_path=tmp_path / "f.json",
        observed_state="ACCEPTED",
        observed_error_code=None,
        observed_events=(),
        side_effects=None,
        actual_subject_id="mr-1",
    )
    with pytest.raises(CaseRunnerError, match="SIDE_EFFECTS_NOT_OBSERVED"):
        _validate_result("AT-MIGRATION-001/FRESH_INSTALL", result)


# ------------------------------------------------------------------- P2 Batch


def test_p2_cases_are_wired(tmp_path: Path) -> None:
    """P2 の4 Case すべてに Adapter があること。

    件数は書かず、`tests/integration/p2/` 配下のMarkerから導出する。
    """
    adapters = build_pytest_adapters(
        registries=REGISTRIES,
        scope="MVP0-A",
        repo_root=REPO_ROOT,
        fixtures_dir=tmp_path,
    )
    p2 = {
        case_id: adapter
        for case_id, adapter in adapters.items()
        if any(node.startswith("tests/integration/p2/") for node in adapter.node_ids)
    }
    assert p2, "P2 の Adapter が1件も無い"
    for case_id, adapter in sorted(p2.items()):
        assert adapter.node_ids, case_id
        for node in adapter.node_ids:
            path_part, _, func = node.partition("::")
            assert (REPO_ROOT / path_part).is_file(), case_id
            assert func, case_id


def test_evidence_reuse_is_refused(tmp_path: Path) -> None:
    """既存Evidenceを上書きしないこと。

    上書きを許すと、失敗を成功で塗り替える経路になる。
    """
    from emit_case_evidence import EvidenceEmissionError, _resolve_output

    target = _resolve_output(tmp_path, REPO_ROOT, "AT-MIGRATION-001/FRESH_INSTALL")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"status": "FAIL"}), encoding="utf-8")
    with pytest.raises(EvidenceEmissionError, match="EVIDENCE_REUSE"):
        _resolve_output(tmp_path, REPO_ROOT, "AT-MIGRATION-001/FRESH_INSTALL")


def test_unknown_case_id_is_refused(tmp_path: Path) -> None:
    """Registryに無いCase IDでEvidenceを作らないこと。"""
    from emit_case_evidence import EvidenceEmissionError, _expected_case

    with pytest.raises((EvidenceEmissionError, AssertionError, KeyError, SystemExit)):
        _expected_case(REGISTRIES, "AT-NO-SUCH-001/NOPE")


def test_path_traversal_is_refused(tmp_path: Path) -> None:
    """Case ID に `..` を含む Evidence Path を作らせないこと。

    Evidence Root の外へ書けると、判定対象外のFileをEvidenceと名乗れる。
    """
    from emit_case_evidence import EvidenceEmissionError, _resolve_output

    for hostile in ("../escape/CASE", "AT-X-001/../../escape", "/abs/AT-X-001/CASE"):
        with pytest.raises(
            EvidenceEmissionError, match="INVALID_CASE_ID|PATH_TRAVERSAL|EVIDENCE_ROOT"
        ):
            _resolve_output(tmp_path, REPO_ROOT, hostile)


def test_evidence_hash_detects_body_tampering(tmp_path: Path) -> None:
    """本文を書き換えたら `evidence_hash` の再計算で検出されること。

    status を PASS へ書き換える経路を塞ぐ。
    """
    from emit_case_evidence import EvidenceEmissionError, verify_case_evidence

    body = {
        "case_id": "AT-GC-001/REFERENCED_KEEP",
        "status": "FAIL",
        "observed": {"state": "REJECTED"},
    }
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    from emit_case_evidence import _evidence_hash

    body["evidence_hash"] = _evidence_hash(body)
    target = tmp_path / "evidence.json"
    target.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    verify_case_evidence(target)  # 改変前は通る

    body["status"] = "PASS"
    target.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(EvidenceEmissionError, match="EVIDENCE_HASH_MISMATCH"):
        verify_case_evidence(target)


def test_evidence_hash_excludes_itself(tmp_path: Path) -> None:
    """`evidence_hash` 自身をHash対象へ含めないこと。

    含めると値が自己参照になり、決して再計算できない。
    """
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    from emit_case_evidence import _evidence_hash

    body = {"case_id": "AT-GC-001/REFERENCED_KEEP", "status": "PASS"}
    first = _evidence_hash(body)
    body["evidence_hash"] = first
    assert _evidence_hash(body) == first


def test_cross_case_event_contamination_is_detectable() -> None:
    """他CaseのEventが混ざれば観測列が変わること。

    Probe を Case ごとに分ける理由の実証である。分けなければ、
    前のCaseのEventが次のCaseのEvidenceへ入る。
    """
    isolated_a, isolated_b = LedgerProbe(), LedgerProbe()
    isolated_a.append_names(["ACTION_BLOCKED"])
    isolated_b.append_names(["POLICY_STALE_RECOVERY_ONLY"])
    assert isolated_a.appended != isolated_b.appended

    shared = LedgerProbe()
    shared.append_names(["ACTION_BLOCKED"])
    shared.append_names(["POLICY_STALE_RECOVERY_ONLY"])
    # 共有すると、片方のCaseの観測に他方のEventが現れる。
    assert shared.appended == ["ACTION_BLOCKED", "POLICY_STALE_RECOVERY_ONLY"]
    assert shared.appended != isolated_a.appended
    assert shared.appended != isolated_b.appended


def test_p3_cases_are_wired(tmp_path: Path) -> None:
    """P3 の Case すべてに Adapter があること。件数は書かず導出する。"""
    adapters = build_pytest_adapters(
        registries=REGISTRIES,
        scope="MVP0-A",
        repo_root=REPO_ROOT,
        fixtures_dir=tmp_path,
    )
    p3 = {
        case_id: adapter
        for case_id, adapter in adapters.items()
        if any(node.startswith("tests/integration/p3/") for node in adapter.node_ids)
    }
    assert p3, "P3 の Adapter が1件も無い"
    for case_id, adapter in sorted(p3.items()):
        assert adapter.node_ids, case_id


def test_no_human_measured_approval_ux_evidence_is_produced(tmp_path: Path) -> None:
    """MVP0-A で人手計測 Evidence を作っていないこと。

    `NO_BYPASS` は `SOURCE_SCAN` であってUX計測ではない。UI未実装を
    PASSの根拠にしない。
    """
    snapshot = json.loads((REPO_ROOT / "registry-snapshot.json").read_text(encoding="utf-8"))
    catalog = {a["area"]: a for a in snapshot["evidence_areas"]}
    assert catalog["approval_ux"]["human_measured"] is True
    assert "approval_ux" not in snapshot["scopes"]["MVP0-A"]["required_evidence_areas"]
    assert "approval_ux" in snapshot["scopes"]["MVP0-B"]["required_evidence_areas"]
