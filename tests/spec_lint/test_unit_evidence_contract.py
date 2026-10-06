"""Unit Evidence 契約（設計書 v1.15 §19.1.1／§26.2／§26.2.1／§15.11）。

Owner Decision DCR-1〜DCR-5 を機械検査する。

## 件数を書かない

「42件」「7件」「4件」を試験へ書かない（不変条件#18）。書けば、Registry が
動いたときに試験のほうが古い数を「正しい」と主張する。ここで固定するのは
**規則**であって件数ではない。

## 未確定を「無い」ことにしない

Policy を持たない空 Case が残ること自体は正しい。正しくないのは、それが
**なぜ残っているのか判らない**ことである。塞がりの根拠として OPEN Block Record
を名指しできることを要求する。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

#: 根拠Hashの書式。真偽値ではなく書式まで見る。
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRIES = REPO_ROOT / "design-source" / "registries"
RECORDS = REPO_ROOT / "blocked" / "records"
SCHEMA_DIR = REPO_ROOT / "schemas" / "evidence" / "CaseEvidence"

sys.path.insert(0, str(REPO_ROOT / "tools"))

UNIT_AREA = "unit_case_suite"
POLICIES = ("REQUIRED_EMPTY", "NOT_APPLICABLE")
KINDS = ("UNIT", "ORCHESTRATION")
OBSERVATIONS = ("OBSERVED", "OBSERVED_EMPTY", "NOT_APPLICABLE")


def _load(name: str) -> Any:
    return yaml.safe_load((REGISTRIES / name).read_text(encoding="utf-8"))


def _cases() -> list[dict[str, Any]]:
    return _load("tests.yaml")["test_cases"]


def _areas() -> list[dict[str, Any]]:
    return _load("evidence-areas.yaml")["evidence_areas"]


def _open_records() -> list[dict[str, Any]]:
    return [
        record
        for record in (json.loads(p.read_text(encoding="utf-8")) for p in RECORDS.glob("*.json"))
        if record["status"] == "OPEN"
    ]


# -- DCR-1: Evidence Area ---------------------------------------------------


def test_unit_case_suite_area_is_registered() -> None:
    """Unit層の領域が正本にある。名前は本Fileの定数と一致する。"""
    areas = {area["area"]: area for area in _areas()}
    assert UNIT_AREA in areas, sorted(areas)
    entry = areas[UNIT_AREA]
    assert entry["human_measured"] is False
    assert entry["evidence_path"].endswith(".json")


def test_unit_case_suite_covers_every_implementation_phase() -> None:
    """Scopeを絞らない。絞ると後続Phaseで足し忘れが黙って「数えない」へ倒れる。

    実装Phaseの集合は他の機械計測領域から引く。**Phase名を書かない。**
    """
    areas = {area["area"]: area for area in _areas()}
    machine_measured = [
        area for area in _areas() if not area["human_measured"] and area["area"] != UNIT_AREA
    ]
    all_phases = set()
    for area in machine_measured:
        all_phases |= set(area["phase_scope"])
    assert set(areas[UNIT_AREA]["phase_scope"]) == all_phases


def test_evidence_area_names_are_unique() -> None:
    """領域名が重複しない。重複すると充足の数え方が二重になる。"""
    names = [area["area"] for area in _areas()]
    assert len(names) == len(set(names)), names


def test_snapshot_requires_the_unit_area_for_every_declared_phase() -> None:
    """Snapshotへ導出されている。Registryにあるだけで数えられない状態にしない。"""
    snapshot = json.loads((REPO_ROOT / "registry-snapshot.json").read_text(encoding="utf-8"))
    declared = {area["area"]: area for area in snapshot["evidence_areas"]}
    assert UNIT_AREA in declared
    for phase in declared[UNIT_AREA]["phase_scope"]:
        required = snapshot["scopes"][phase]["required_evidence_areas"]
        assert UNIT_AREA in required, f"{phase} に {UNIT_AREA} が無い"


# -- DCR-4: Case Registry ---------------------------------------------------


def test_event_observation_policy_vocabulary_is_closed() -> None:
    """語彙の外の値を許さない。"""
    for case in _cases():
        policy = case.get("event_observation_policy")
        if policy is not None:
            assert policy in POLICIES, f"{case['test_id']}/{case['case_id']}: {policy}"


def test_not_applicable_requires_an_empty_expectation() -> None:
    """作用を主張するCaseがEvent観測の免除を名乗れない。"""
    for case in _cases():
        if case.get("event_observation_policy") == "NOT_APPLICABLE":
            assert not (case.get("expected_event_sequence") or []), (
                f"{case['test_id']}/{case['case_id']} は NOT_APPLICABLE だが期待Event列が非空"
            )


def test_policy_is_only_declared_where_the_expectation_is_empty() -> None:
    """期待列が非空のCaseへPolicyを付けない。`[]`の曖昧さを解くためのFieldである。"""
    for case in _cases():
        if case.get("event_observation_policy") is not None:
            assert not (case.get("expected_event_sequence") or []), (
                f"{case['test_id']}/{case['case_id']} は期待Event列が非空なのにPolicyを持つ"
            )


def test_undeclared_empty_cases_are_blocked_by_an_open_record() -> None:
    """Policyの無い空Caseは、塞がりの根拠を名指しできること。

    **未確定であること自体は誤りではない。** 誤りは、なぜ未確定なのかが
    どこにも残っていないことである。既定値で埋めれば、決まっていないことが
    決まったように見える。
    """
    open_text = " ".join(json.dumps(record, ensure_ascii=False) for record in _open_records())
    undeclared = [
        f"{case['test_id']}/{case['case_id']}"
        for case in _cases()
        if "MVP0-A" in (case.get("phase_scope") or [])
        and not (case.get("expected_event_sequence") or [])
        and case.get("event_observation_policy") is None
    ]
    unexplained = [case_id for case_id in undeclared if case_id not in open_text]
    assert not unexplained, f"塞がりの根拠が無いまま未確定のCase: {unexplained}"


def test_declared_policies_are_bound_to_the_expectation_hash() -> None:
    """Policyが期待値Hashの導出対象に入っている。

    入っていないと、Policyだけを差し替えても期待値が変わっていないように見える。
    """
    from build_expectation_hashes import DESCRIPTOR_FIELDS, expectation_hash

    assert "event_observation_policy" in DESCRIPTOR_FIELDS

    case = next(c for c in _cases() if c.get("event_observation_policy") == "NOT_APPLICABLE")
    flipped = dict(case)
    flipped["event_observation_policy"] = "REQUIRED_EMPTY"
    assert expectation_hash(case) != expectation_hash(flipped)


# -- DCR-2 / DCR-3: Evidence Schema ----------------------------------------


def test_evidence_schema_registry_keeps_the_old_version_read_only() -> None:
    """旧版を上書きしない。read_onlyとして残す（§15.11.2）。"""
    entry = next(
        item
        for item in _load("evidence-schemas.yaml")["evidence_schemas"]
        if item["schema_name"] == "CaseEvidence"
    )
    versions = {str(v["version"]): v for v in entry["versions"]}
    active = str(entry["active_write_version"])

    # 旧版が消えていないこと。上書きせず残すのが規則である。
    assert "1.2" in versions, sorted(versions)
    assert len(versions) >= 2, sorted(versions)
    assert active in versions, (active, sorted(versions))

    # read_only は active_write_version 以外にだけ付く。**どの番号が active かは
    # 在庫であって規則ではない。** 書き写すと版を上げるたびに黙ってずれる。
    for version, spec in versions.items():
        assert spec["read_only"] is (version != active), version

    # 退役した版は理由を持つ。黙って落とさない。
    for version, spec in versions.items():
        if version != active:
            assert spec.get("retired_reason"), version


def test_both_schema_files_exist_and_declare_their_own_version() -> None:
    entry = next(
        item
        for item in _load("evidence-schemas.yaml")["evidence_schemas"]
        if item["schema_name"] == "CaseEvidence"
    )
    for spec in entry["versions"]:
        path = REPO_ROOT / spec["path"]
        assert path.is_file(), path
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["properties"]["evidence_schema_version"]["const"] == str(spec["version"])
        assert doc["$id"].endswith(f"{spec['version']}.schema.json")


def test_schema_2_0_requires_the_layer_and_observation_fields() -> None:
    doc = json.loads((SCHEMA_DIR / "2.0.schema.json").read_text(encoding="utf-8"))
    assert "evidence_kind" in doc["required"]
    assert "event_observation" in doc["required"]
    assert tuple(doc["properties"]["evidence_kind"]["enum"]) == KINDS
    assert tuple(doc["properties"]["event_observation"]["enum"]) == OBSERVATIONS


def test_schema_1_2_has_neither_new_field() -> None:
    """凍結版の意味を後から変えない。"""
    doc = json.loads((SCHEMA_DIR / "1.2.schema.json").read_text(encoding="utf-8"))
    assert "evidence_kind" not in doc["properties"]
    assert "event_observation" not in doc["properties"]
    assert doc["additionalProperties"] is False


def test_schema_catalog_hash_does_not_cover_evidence_schemas() -> None:
    """Evidence文書はCore Schema Catalogに含まれない（§15.11）。

    含めてしまうと、Evidence文書へFieldを足すたびに全PlanのSchema束縛が動く。
    """
    core = {item["schema_name"] for item in _load("schemas.yaml")["core_schemas"]}
    assert "CaseEvidence" not in core


@pytest.mark.parametrize(
    ("observation", "sequence", "valid"),
    [
        ("NOT_APPLICABLE", None, True),
        ("NOT_APPLICABLE", [], False),
        ("OBSERVED_EMPTY", [], True),
        ("OBSERVED_EMPTY", None, False),
        ("OBSERVED_EMPTY", ["X"], False),
        ("OBSERVED", ["X"], True),
        ("OBSERVED", [], False),
    ],
)
def test_observation_and_sequence_must_agree(
    observation: str, sequence: list[str] | None, valid: bool
) -> None:
    """`event_observation` と `event_sequence` の対応をSchemaが強制する。

    **空配列をNot Applicableの代用にできない。**
    """
    jsonschema = pytest.importorskip("jsonschema")
    doc = json.loads((SCHEMA_DIR / "2.0.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(doc)
    body = _minimal_evidence(observation, sequence)
    errors = [e for e in validator.iter_errors(body) if "observed" in str(e.json_path)]
    assert (not errors) is valid, [e.message for e in errors]


def test_orchestration_cannot_claim_not_applicable() -> None:
    """作用を主張する層がLedger観測の免除を名乗れない。"""
    jsonschema = pytest.importorskip("jsonschema")
    doc = json.loads((SCHEMA_DIR / "2.0.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(doc)
    body = _minimal_evidence("NOT_APPLICABLE", None)
    body["evidence_kind"] = "ORCHESTRATION"
    assert list(validator.iter_errors(body))


def _minimal_evidence(observation: str, sequence: list[str] | None) -> dict[str, Any]:
    sha = "sha256:" + "0" * 64
    return {
        "evidence_schema_version": "2.0",
        "evidence_kind": "UNIT" if observation == "NOT_APPLICABLE" else "ORCHESTRATION",
        "event_observation": observation,
        "case_id": "AT-X-001/Y",
        "test_id": "AT-X-001",
        "gate_ids": [],
        "status": "PASS",
        "mismatched_fields": [],
        "implementation_commit_sha": "0" * 40,
        "design_sha256": sha,
        "registry_snapshot_hash": sha,
        "schema_catalog_hash": sha,
        "fault_injection": None,
        "source_tree_clean": True,
        "input_fixture_path": "fixture.json",
        "input_fixture_hash": sha,
        "environment_manifest_hash": sha,
        "expected": {
            "state": "ACCEPTED",
            "error_code": None,
            "event_sequence": [],
            "expectation_descriptor_hash": sha,
        },
        "observed": {
            "state": "ACCEPTED",
            "error_code": None,
            "event_sequence": sequence,
            "actual_subject_id": "x",
        },
        "side_effects": None,
        "test_node_ids": [],
        "producer": "tools/emit_case_evidence.py",
        "evidence_hash": sha,
    }


# -- DCR-5: Runner 契約 -----------------------------------------------------


def test_runner_exempts_only_the_declared_policy() -> None:
    """免除の根拠がRegistry宣言だけであること。

    宣言の無いCaseまで免除されると、Ledgerを見ていないCaseが黙って通る。
    """
    from case_runner import _EVENT_OBSERVATION_POLICIES, _POLICY_NOT_APPLICABLE

    assert _EVENT_OBSERVATION_POLICIES == frozenset(POLICIES)
    source = (REPO_ROOT / "tools" / "case_runner.py").read_text(encoding="utf-8")
    assert f"self.event_observation_policy == {_POLICY_NOT_APPLICABLE!r}" in source or (
        "self.event_observation_policy == _POLICY_NOT_APPLICABLE" in source
    )
    # 既定で免除しない。
    assert "event_observation_policy: str | None = None" in source


def test_emitter_and_verifier_agree_on_the_active_version() -> None:
    """EmitterとVerifierが同じ版を指すこと。ずれるとEvidenceが常に拒否される。"""
    from emit_case_evidence import EVIDENCE_SCHEMA_VERSION

    entry = next(
        item
        for item in _load("evidence-schemas.yaml")["evidence_schemas"]
        if item["schema_name"] == "CaseEvidence"
    )
    assert EVIDENCE_SCHEMA_VERSION == str(entry["active_write_version"])

    verifier = (REPO_ROOT / "verify_runtime_go.py").read_text(encoding="utf-8")
    assert f"EVIDENCE_SCHEMA_VERSION = '{EVIDENCE_SCHEMA_VERSION}'" in verifier


# -- DCR-5: Runner の Ledger 観測必須と免除 ---------------------------------


def _observation_record(
    case_id: str, *, ledger_observed: bool, events: list[str]
) -> dict[str, Any]:
    """`case_observation` が書き出す1行と同じ形。試験側でEvent列を作らない。

    ここで作るのは「Runnerが受け取る観測記録」であって Event そのものではない。
    Ledger を触らずに Runner の判定だけを見る。
    """
    return {
        "case_id": case_id,
        "node_id": "tests/unit/x.py::y",
        "observed_state": "REJECTED",
        "observed_error_code": "SOME_ERROR",
        "observed_event_sequence": events,
        "ledger_observed": ledger_observed,
        "ledger_head_before": 0,
        "ledger_head_after": 0,
        "actual_subject_id": "subject-under-test",
        "side_effects": {
            "network_calls": 0,
            "process_launches": 0,
            "workspace_commits": 0,
            "external_effects": 0,
            "ledger_effect_attempts": 0,
        },
        "input_payload": {},
        "fault_injection": None,
        "recorded": True,
    }


def _adapter(tmp_path: Path, case_id: str, policy: str | None) -> Any:
    from case_runner import PytestCaseAdapter

    return PytestCaseAdapter(
        case_id=case_id,
        node_ids=("tests/unit/x.py::y",),
        repo_root=REPO_ROOT,
        fixtures_dir=tmp_path,
        event_observation_policy=policy,
    )


def test_unit_case_may_skip_ledger_observation(tmp_path: Path) -> None:
    """`NOT_APPLICABLE` の Case は Ledger 未観測でも通ること。"""
    case_id = next(
        f"{c['test_id']}/{c['case_id']}"
        for c in _cases()
        if c.get("event_observation_policy") == "NOT_APPLICABLE"
    )
    adapter = _adapter(tmp_path, case_id, "NOT_APPLICABLE")
    merged = adapter._merge([_observation_record(case_id, ledger_observed=False, events=[])])
    assert merged["observed_event_sequence"] == []
    # 未観測を「0件観測」へ書き換えていないこと。記録はそのまま残る。
    assert merged["ledger_observed"] is False


def test_required_empty_case_still_needs_ledger_observation(tmp_path: Path) -> None:
    """`REQUIRED_EMPTY` は従来どおり観測必須。免除しない。"""
    from case_runner import CaseRunnerError

    case_id = next(
        f"{c['test_id']}/{c['case_id']}"
        for c in _cases()
        if c.get("event_observation_policy") == "REQUIRED_EMPTY"
    )
    adapter = _adapter(tmp_path, case_id, "REQUIRED_EMPTY")
    with pytest.raises(CaseRunnerError, match="LEDGER_NOT_OBSERVED"):
        adapter._merge([_observation_record(case_id, ledger_observed=False, events=[])])


def test_case_without_a_policy_still_needs_ledger_observation(tmp_path: Path) -> None:
    """宣言の無い Case を既定で免除しないこと。

    既定で免除すると、Registry に何も書いていない Case が黙って Ledger を
    見ずに通る。免除は宣言があってはじめて効く。
    """
    from case_runner import CaseRunnerError

    adapter = _adapter(tmp_path, "AT-X-001/Y", None)
    with pytest.raises(CaseRunnerError, match="LEDGER_NOT_OBSERVED"):
        adapter._merge([_observation_record("AT-X-001/Y", ledger_observed=False, events=[])])


def test_exempt_case_carrying_events_is_rejected_by_the_runner(tmp_path: Path) -> None:
    """免除された Case が Event を持ち帰ったら止めること。"""
    from case_runner import CaseRunnerError

    adapter = _adapter(tmp_path, "AT-X-001/Y", "NOT_APPLICABLE")
    with pytest.raises(CaseRunnerError, match="UNEXPECTED_EVENT_OBSERVATION"):
        adapter._merge(
            [_observation_record("AT-X-001/Y", ledger_observed=False, events=["SOME_EVENT"])]
        )


# Outcome 語彙の正本は Block Record の Lifecycle 試験が持っている。
# ここで書き写すと、語彙が増えたときに片方だけ古くなる。
from test_blocked_record_lifecycle import _RESOLVING, _SUPERSEDING  # noqa: E402


def _assert_closed_records_carry_grounds(records: list[dict]) -> None:
    """閉じた Record が、決定だけでなく確かめられる根拠で閉じられていること。"""
    resolved = [r for r in records if r["status"] == "RESOLVED"]
    assert resolved, "閉じた Record が1件も無い"
    for record in resolved:
        resolution = record.get("resolution") or {}
        outcome = resolution.get("outcome")
        assert outcome in _SUPERSEDING | _RESOLVING, (
            f"{record['blocker_id']}: 未登録の outcome {outcome!r}"
        )
        if outcome in _RESOLVING:
            evidence = resolution.get("evidence") or []
            verified_by = resolution.get("verified_by") or []
            assert evidence or verified_by, f"{record['blocker_id']}: 根拠を挙げずに閉じている"
            for item in evidence:
                legacy = item.get("evidence_hash")
                declared = item.get("evidence_manifest_hash")
                measured = item.get("evidence_file_sha256")
                assert legacy or (declared and measured), (
                    f"{record['blocker_id']}: Evidence Hash が無い"
                )
                for value in (legacy, declared, measured):
                    if value is not None:
                        assert _SHA256.fullmatch(str(value)), (
                            f"{record['blocker_id']}: Hash 書式が不正 {value!r}"
                        )
                if declared or measured:
                    assert declared == measured, (
                        f"{record['blocker_id']}: Manifest束縛と実Bytesが一致していない"
                    )
        else:
            assert resolution.get("successor_blocker_id"), (
                f"{record['blocker_id']}: 後継 Record が無いまま引き継ぎで閉じている"
            )


def _synthetic_records(tmp_path: Path) -> list[dict]:
    from synthetic_blocked_records import build_checked_corpus

    design = REPO_ROOT / "design-v1.25-runtime-go.md"
    snapshot = REPO_ROOT / "registry-snapshot.json"
    written = build_checked_corpus(tmp_path / "records", design, snapshot)
    return list(written.values())


def test_a_decision_alone_never_closes_a_blockage(tmp_path: Path) -> None:
    """契約が決まったことと、実装が契約どおり動くことを混同しないこと。

    閉じるには確かめられる根拠が要る。`RESOLVED_BY_FIX` は根拠（Evidence か
    `verified_by`）を、引き継ぎは後継を要求する。保存 Record（非公開）への同じ
    規則は `tests/private_history/` が当てる。ここでは正規の検査器が受理した合成の
    一覧へ当てる。
    """
    _assert_closed_records_carry_grounds(_synthetic_records(tmp_path))


def test_a_fix_without_grounds_is_detected(tmp_path: Path) -> None:
    """根拠を消した `RESOLVED_BY_FIX` を見逃さないこと。**検出側を測る。**"""
    records = _synthetic_records(tmp_path)
    fixed = next(r for r in records if (r.get("resolution") or {}).get("outcome") in _RESOLVING)
    fixed["resolution"].pop("verified_by")
    with pytest.raises(AssertionError, match="根拠を挙げずに閉じている"):
        _assert_closed_records_carry_grounds(records)


def test_every_empty_expectation_case_now_declares_a_policy() -> None:
    """空の期待Event列を持つ Case が、宣言なしで残っていないこと。

    宣言が無いまま残ると Evidence 生成も Release 判定も止まる。止まること自体は
    正しいが、止まったままにしない。
    """
    undeclared = [
        f"{c['test_id']}/{c['case_id']}"
        for c in _cases()
        if "MVP0-A" in (c.get("phase_scope") or [])
        and not (c.get("expected_event_sequence") or [])
        and c.get("event_observation_policy") is None
    ]
    assert not undeclared, undeclared


# -- Policy を Release の信頼根へ束縛する（設計書 v1.16 §26.2.1）------------


def _snapshot() -> dict[str, Any]:
    return json.loads((REPO_ROOT / "registry-snapshot.json").read_text(encoding="utf-8"))


def test_snapshot_carries_every_declared_policy() -> None:
    """Registry の Policy が Snapshot へそのまま載っていること。

    Verifier が読むのは Snapshot だけである。載っていなければ照合できない。
    """
    snapshot = _snapshot()["expectations"]
    for case in _cases():
        key = f"{case['test_id']}/{case['case_id']}"
        if key not in snapshot:
            continue
        assert snapshot[key].get("event_observation_policy") == case.get(
            "event_observation_policy"
        ), key


def test_snapshot_keeps_undeclared_policies_absent() -> None:
    """未確定の Case が Snapshot で値を持たないこと。

    **欠落を値へ変換しない。** 変換すると、決まっていない Case が
    「Ledger を見なくてよい」と宣言されたことになる。
    """
    snapshot = _snapshot()["expectations"]
    undeclared = [
        f"{c['test_id']}/{c['case_id']}"
        for c in _cases()
        if c.get("event_observation_policy") is None
    ]
    assert undeclared
    for key in undeclared:
        if key in snapshot:
            assert snapshot[key]["event_observation_policy"] is None, key


def test_snapshot_hash_covers_the_policy() -> None:
    """Policy を書き換えると Snapshot の自己Hashが動くこと。

    動かなければ、束縛済み Release に対して後から Policy を足せてしまう。
    """
    import build_registry_snapshot as builder

    snapshot = _snapshot()
    recomputed = builder.domain_hash(
        "FDE-HARNESS/registry-snapshot/1/",
        {k: v for k, v in snapshot.items() if k not in ("registry_snapshot_hash", "generated_at")},
    )
    assert recomputed == snapshot["registry_snapshot_hash"]

    tampered = json.loads(json.dumps(snapshot))
    key = next(
        k
        for k, v in tampered["expectations"].items()
        if v.get("event_observation_policy") == "REQUIRED_EMPTY"
    )
    tampered["expectations"][key]["event_observation_policy"] = "NOT_APPLICABLE"
    after = builder.domain_hash(
        "FDE-HARNESS/registry-snapshot/1/",
        {k: v for k, v in tampered.items() if k not in ("registry_snapshot_hash", "generated_at")},
    )
    assert after != snapshot["registry_snapshot_hash"]


# -- Expectation Hash が Policy を束縛する ---------------------------------


def _hash_of(case: dict[str, Any]) -> str:
    from build_expectation_hashes import expectation_hash

    return expectation_hash(case)


def test_changing_the_policy_changes_the_expectation_hash() -> None:
    """`REQUIRED_EMPTY` → `NOT_APPLICABLE` で期待値Hashが動くこと。"""
    case = next(c for c in _cases() if c.get("event_observation_policy") == "REQUIRED_EMPTY")
    flipped = dict(case)
    flipped["event_observation_policy"] = "NOT_APPLICABLE"
    assert _hash_of(case) != _hash_of(flipped)


def test_removing_the_policy_changes_the_expectation_hash() -> None:
    """Policy 削除で期待値Hashが動くこと。

    欠落も期待値の一部である。後から勝手に足せないようにする。
    """
    case = next(c for c in _cases() if c.get("event_observation_policy") == "NOT_APPLICABLE")
    without = {k: v for k, v in case.items() if k != "event_observation_policy"}
    assert _hash_of(case) != _hash_of(without)


def test_policy_is_the_only_difference_and_is_still_detected() -> None:
    """Case ID・State・Error・Event列が同じでも Policy 差分を検出すること。"""
    case = next(c for c in _cases() if c.get("event_observation_policy") == "REQUIRED_EMPTY")
    other = dict(case)
    other["event_observation_policy"] = "NOT_APPLICABLE"
    for field in (
        "case_id",
        "test_id",
        "expected_state",
        "expected_error_code",
        "expected_event_sequence",
    ):
        assert case.get(field) == other.get(field)
    assert _hash_of(case) != _hash_of(other)


def test_field_order_does_not_change_the_expectation_hash() -> None:
    """並び順を変えても Hash が動かないこと。

    順序で動くと、Registry を整形し直しただけで束縛が切れる。
    """
    case = next(c for c in _cases() if c.get("event_observation_policy") is not None)
    reordered = dict(reversed(list(case.items())))
    assert list(case) != list(reordered)
    assert _hash_of(case) == _hash_of(reordered)


def test_recorded_expectation_hashes_are_derived() -> None:
    """Registry に記録された期待値Hashが導出値と一致すること。"""
    for case in _cases():
        assert case["expectation_descriptor_hash"] == _hash_of(case), (
            f"{case['test_id']}/{case['case_id']}"
        )
