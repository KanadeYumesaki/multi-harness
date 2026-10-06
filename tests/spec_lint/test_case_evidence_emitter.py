"""Evidence Emitter の契約試験。

守っている核は1つ。**PASSと書ける経路を作らない。**

`status` は引数に存在せず、実行結果とRegistry期待値の突合からだけ決まる。
そのうえで、生成条件（Clean Tree／Repository外出力／副作用観測／
再利用拒否／Traversal拒否）を満たさないEvidenceは1件も残さない。
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

from emit_case_evidence import (  # noqa: E402
    CaseRunResult,
    EvidenceEmissionError,
    SideEffectObservation,
    emit_case_evidence,
    verify_case_evidence,
)

REGISTRIES = REPO_ROOT / "design-source" / "registries"

# 実在するCase。期待値はRegistryから読まれる。
CASE_ID = "AT-POLICY-STALE-001/NEW_EXTERNAL_EFFECT"
EXPECTED_STATE = "BLOCKED_POLICY"
EXPECTED_ERROR = "POLICY_STALE_EXTERNAL_EFFECT_BLOCKED"
EXPECTED_EVENTS = (
    "POLICY_STALE_DETECTED",
    "POLICY_STALE_ACTION_BLOCKED",
    "ACTION_BLOCKED",
)

ZERO_EFFECTS = SideEffectObservation(
    network_calls=0,
    process_launches=0,
    workspace_commits=0,
    external_effects=0,
    ledger_effect_attempts=0,
)


@pytest.fixture
def clean_repo(tmp_path: Path) -> Path:
    """Commit済みでCleanな一時Repository。

    設計書と `registry-snapshot.json` を置く。Evidence は
    「どの設計・どのRegistryに対する観測か」を束縛するため、
    これらが無い木ではEvidenceを生成できない（Fail-Closed）。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "design-v1.25-runtime-go.md").write_text("# design\n", encoding="utf-8")
    (repo / "registry-snapshot.json").write_text(
        json.dumps(
            {
                "registry_snapshot_hash": "sha256:" + "a" * 64,
                "schema_catalog_hash": "sha256:" + "c" * 64,
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)  # noqa: S603,S607
    subprocess.run(  # noqa: S603
        ["git", "config", "user.email", "t@example.invalid"],  # noqa: S607
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)  # noqa: S603,S607
    (repo / "file.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)  # noqa: S603,S607
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)  # noqa: S603,S607
    return repo


@pytest.fixture
def evidence_dir(tmp_path: Path) -> Path:
    target = tmp_path / "evidence"
    target.mkdir()
    return target


@pytest.fixture
def fixture_file(tmp_path: Path) -> Path:
    path = tmp_path / "input-fixture.json"
    path.write_text('{"input": "value"}\n', encoding="utf-8")
    return path


@pytest.fixture
def env_manifest(tmp_path: Path) -> Path:
    path = tmp_path / "runtime-environment.json"
    path.write_text('{"is_wsl2": true}\n', encoding="utf-8")
    return path


def _result(fixture_file: Path, **overrides: object) -> CaseRunResult:
    base = CaseRunResult(
        case_id=CASE_ID,
        gate_ids=("GATE-POLICY-FRESHNESS",),
        input_fixture_path=fixture_file,
        observed_state=EXPECTED_STATE,
        observed_error_code=EXPECTED_ERROR,
        observed_events=EXPECTED_EVENTS,
        side_effects=ZERO_EFFECTS,
        actual_subject_id="attempt-under-test",
        # Ledger を観測した Case は Head を持つ。Schema 3.0 が整数を要求する。
        ledger_head_before=0,
        ledger_head_after=len(EXPECTED_EVENTS),
        test_node_ids=("tests/integration/policy/test_policy_stale_effects.py::x",),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _emit(result: CaseRunResult, clean_repo: Path, evidence_dir: Path, env: Path) -> Path:
    return emit_case_evidence(
        result,
        evidence_dir=evidence_dir,
        repo_root=clean_repo,
        environment_manifest=env,
        registries=REGISTRIES,
    )


# --------------------------------------------------------------------------
# 正常系：実測から PASS が導出される
# --------------------------------------------------------------------------


def test_emits_pass_when_observed_matches_expected(
    clean_repo: Path, evidence_dir: Path, fixture_file: Path, env_manifest: Path
) -> None:
    path = _emit(_result(fixture_file), clean_repo, evidence_dir, env_manifest)
    body = json.loads(path.read_text(encoding="utf-8"))

    assert body["status"] == "PASS"
    assert body["case_id"] == CASE_ID
    assert body["gate_ids"] == ["GATE-POLICY-FRESHNESS"]
    assert body["source_tree_clean"] is True
    assert len(body["implementation_commit_sha"]) == 40
    assert body["input_fixture_hash"].startswith("sha256:")
    assert body["environment_manifest_hash"].startswith("sha256:")
    assert body["evidence_hash"].startswith("sha256:")
    assert body["side_effects"] == ZERO_EFFECTS.as_dict()
    # 期待値はRegistryから来ている。
    assert body["expected"]["state"] == EXPECTED_STATE
    verify_case_evidence(path)


def test_status_is_not_an_input_parameter() -> None:
    """`status` を渡す口が無いこと。

    書ける口が無ければ、書き間違いも偽装もできない。
    """
    assert "status" not in CaseRunResult.__dataclass_fields__
    import inspect

    signature = inspect.signature(emit_case_evidence)
    assert "status" not in signature.parameters


def test_observed_mismatch_yields_fail_not_pass(
    clean_repo: Path, evidence_dir: Path, fixture_file: Path, env_manifest: Path
) -> None:
    """観測が期待と違えば FAIL になること。"""
    path = _emit(
        _result(fixture_file, observed_state="SUCCEEDED"), clean_repo, evidence_dir, env_manifest
    )
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["status"] == "FAIL"
    assert "state" in body["mismatched_fields"]


@pytest.mark.parametrize(
    "overrides,expected_field",
    [
        ({"observed_state": "SUCCEEDED"}, "state"),
        ({"observed_error_code": None}, "error_code"),
        ({"observed_events": ("ACTION_BLOCKED",)}, "event_sequence"),
    ],
)
def test_each_expected_field_is_compared(
    overrides: dict[str, object],
    expected_field: str,
    clean_repo: Path,
    evidence_dir: Path,
    fixture_file: Path,
    env_manifest: Path,
) -> None:
    """State・Error Code・Event列のいずれの改変も検出すること。"""
    path = _emit(_result(fixture_file, **overrides), clean_repo, evidence_dir, env_manifest)
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["status"] == "FAIL"
    assert expected_field in body["mismatched_fields"]


# --------------------------------------------------------------------------
# 副作用観測の欠落
# --------------------------------------------------------------------------


def test_missing_side_effect_observation_cannot_be_pass(
    clean_repo: Path, evidence_dir: Path, fixture_file: Path, env_manifest: Path
) -> None:
    """副作用を測っていないCaseはPASSにならないこと。

    観測していないものをPASSにしない（不変条件#16）。
    """
    path = _emit(_result(fixture_file, side_effects=None), clean_repo, evidence_dir, env_manifest)
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["status"] == "FAIL"
    assert "side_effect_observation_missing" in body["mismatched_fields"]
    assert body["side_effects"] is None


def test_zero_effects_and_unmeasured_are_not_the_same(
    clean_repo: Path, evidence_dir: Path, fixture_file: Path, env_manifest: Path
) -> None:
    """「0だった」と「測っていない」を同じ値へ潰さないこと。"""
    measured = _emit(_result(fixture_file), clean_repo, evidence_dir, env_manifest)
    unmeasured_dir = evidence_dir.parent / "evidence2"
    unmeasured_dir.mkdir()
    unmeasured = _emit(
        _result(fixture_file, side_effects=None), clean_repo, unmeasured_dir, env_manifest
    )
    a = json.loads(measured.read_text(encoding="utf-8"))
    b = json.loads(unmeasured.read_text(encoding="utf-8"))
    assert a["status"] == "PASS"
    assert b["status"] == "FAIL"
    assert a["side_effects"] is not None
    assert b["side_effects"] is None


# --------------------------------------------------------------------------
# Dirty Tree
# --------------------------------------------------------------------------


def test_dirty_tree_is_rejected_and_writes_nothing(
    clean_repo: Path, evidence_dir: Path, fixture_file: Path, env_manifest: Path
) -> None:
    """Dirty Treeでは生成せず、Fileも残さないこと。"""
    (clean_repo / "dirty.txt").write_text("y\n", encoding="utf-8")
    with pytest.raises(EvidenceEmissionError, match="DIRTY_TREE"):
        _emit(_result(fixture_file), clean_repo, evidence_dir, env_manifest)
    assert list(evidence_dir.rglob("*.json")) == []


# --------------------------------------------------------------------------
# 出力先
# --------------------------------------------------------------------------


def test_output_inside_the_repository_is_rejected(
    clean_repo: Path, fixture_file: Path, env_manifest: Path
) -> None:
    """Repository内への出力を拒否すること。"""
    inside = clean_repo / "runtime-evidence"
    inside.mkdir()
    with pytest.raises(EvidenceEmissionError, match="EVIDENCE_IN_REPOSITORY"):
        _emit(_result(fixture_file), clean_repo, inside, env_manifest)


@pytest.mark.parametrize(
    "bad_case_id",
    [
        "AT-X-001/../../escape",
        "../../etc/passwd",
        "AT-X-001/..",
        "AT-X-001/a/../../b",
    ],
)
def test_path_traversal_in_case_id_is_rejected(
    bad_case_id: str,
    clean_repo: Path,
    evidence_dir: Path,
    fixture_file: Path,
    env_manifest: Path,
) -> None:
    """Case IDを使ったPath Traversalを拒否すること。"""
    with pytest.raises(EvidenceEmissionError):
        _emit(_result(fixture_file, case_id=bad_case_id), clean_repo, evidence_dir, env_manifest)
    assert list(evidence_dir.rglob("*.json")) == []


def test_unknown_case_id_is_rejected(
    clean_repo: Path, evidence_dir: Path, fixture_file: Path, env_manifest: Path
) -> None:
    """Registryに無いCaseのEvidenceを作らないこと。"""
    with pytest.raises(EvidenceEmissionError, match="UNKNOWN_CASE"):
        _emit(
            _result(fixture_file, case_id="AT-NOT-REAL-001/NOPE"),
            clean_repo,
            evidence_dir,
            env_manifest,
        )


# --------------------------------------------------------------------------
# 再利用
# --------------------------------------------------------------------------


def test_reusing_the_same_evidence_path_is_rejected(
    clean_repo: Path, evidence_dir: Path, fixture_file: Path, env_manifest: Path
) -> None:
    """同じPathへ二度書かないこと。

    上書きを許すと、失敗したEvidenceを成功で塗り替えられる。
    """
    _emit(_result(fixture_file), clean_repo, evidence_dir, env_manifest)
    with pytest.raises(EvidenceEmissionError, match="EVIDENCE_REUSE"):
        _emit(_result(fixture_file), clean_repo, evidence_dir, env_manifest)


def test_reuse_rejection_preserves_the_first_evidence(
    clean_repo: Path, evidence_dir: Path, fixture_file: Path, env_manifest: Path
) -> None:
    """再利用を拒否したとき、最初のEvidenceが壊れていないこと。"""
    first = _emit(_result(fixture_file), clean_repo, evidence_dir, env_manifest)
    original = first.read_bytes()
    with pytest.raises(EvidenceEmissionError):
        _emit(
            _result(fixture_file, observed_state="SUCCEEDED"),
            clean_repo,
            evidence_dir,
            env_manifest,
        )
    assert first.read_bytes() == original
    verify_case_evidence(first)


# --------------------------------------------------------------------------
# Hash改変の検出
# --------------------------------------------------------------------------


def test_status_tampering_is_detected(
    clean_repo: Path, evidence_dir: Path, fixture_file: Path, env_manifest: Path
) -> None:
    """生成後にPASSへ書き換えたEvidenceを検出すること。"""
    path = _emit(
        _result(fixture_file, observed_state="SUCCEEDED"), clean_repo, evidence_dir, env_manifest
    )
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["status"] == "FAIL"
    body["status"] = "PASS"
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")

    with pytest.raises(EvidenceEmissionError, match="EVIDENCE_HASH_MISMATCH"):
        verify_case_evidence(path)


def test_input_fixture_hash_tampering_is_detected(
    clean_repo: Path, evidence_dir: Path, fixture_file: Path, env_manifest: Path
) -> None:
    """Fixture Hashの差し替えを検出すること。"""
    path = _emit(_result(fixture_file), clean_repo, evidence_dir, env_manifest)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["input_fixture_hash"] = "sha256:" + "9" * 64
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(EvidenceEmissionError, match="EVIDENCE_HASH_MISMATCH"):
        verify_case_evidence(path)


def test_side_effect_tampering_is_detected(
    clean_repo: Path, evidence_dir: Path, fixture_file: Path, env_manifest: Path
) -> None:
    """副作用観測値の改変を検出すること。"""
    path = _emit(_result(fixture_file), clean_repo, evidence_dir, env_manifest)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["side_effects"]["network_calls"] = 5
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(EvidenceEmissionError, match="EVIDENCE_HASH_MISMATCH"):
        verify_case_evidence(path)


def test_evidence_hash_field_itself_is_excluded_from_the_hash(
    clean_repo: Path, evidence_dir: Path, fixture_file: Path, env_manifest: Path
) -> None:
    """`evidence_hash` を自分自身の入力に含めないこと（自己参照の回避）。"""
    path = _emit(_result(fixture_file), clean_repo, evidence_dir, env_manifest)
    verify_case_evidence(path)
    body = json.loads(path.read_text(encoding="utf-8"))
    # Hashを消しても再計算値は同じ（＝含めていない）。
    recorded = body.pop("evidence_hash")
    import emit_case_evidence as module

    assert module._evidence_hash(body) == recorded


def test_input_fixture_change_changes_the_evidence_hash(
    clean_repo: Path, evidence_dir: Path, fixture_file: Path, env_manifest: Path, tmp_path: Path
) -> None:
    """Fixtureが変わればEvidence Hashも変わること。"""
    first = _emit(_result(fixture_file), clean_repo, evidence_dir, env_manifest)
    fixture_file.write_text('{"input": "CHANGED"}\n', encoding="utf-8")
    second_dir = tmp_path / "evidence3"
    second_dir.mkdir()
    second = _emit(_result(fixture_file), clean_repo, second_dir, env_manifest)
    a = json.loads(first.read_text(encoding="utf-8"))
    b = json.loads(second.read_text(encoding="utf-8"))
    assert a["input_fixture_hash"] != b["input_fixture_hash"]
    assert a["evidence_hash"] != b["evidence_hash"]


def test_missing_environment_manifest_is_rejected(
    clean_repo: Path, evidence_dir: Path, fixture_file: Path, tmp_path: Path
) -> None:
    """Environment Manifestが無ければ生成しないこと。"""
    with pytest.raises(EvidenceEmissionError, match="MISSING_ENVIRONMENT_MANIFEST"):
        _emit(_result(fixture_file), clean_repo, evidence_dir, tmp_path / "absent.json")


def test_missing_input_fixture_is_rejected(
    clean_repo: Path, evidence_dir: Path, env_manifest: Path, tmp_path: Path
) -> None:
    """Fixtureが無ければ生成しないこと。Hashを測れない。"""
    with pytest.raises(EvidenceEmissionError, match="MISSING_INPUT_FIXTURE"):
        _emit(
            _result(tmp_path / "absent.json", side_effects=ZERO_EFFECTS),
            clean_repo,
            evidence_dir,
            env_manifest,
        )


def test_cli_has_no_pass_writing_path() -> None:
    """CLIからPASSを書ける経路が無いこと。"""
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(REPO_ROOT / "tools" / "emit_case_evidence.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 4
    assert "PASSを手で書ける CLI 経路は用意しない" in result.stderr


# --------------------------------------------------------------------------
# v1.15：層と Event 観測有無（Owner Decision DCR-2／DCR-3／DCR-5）
# --------------------------------------------------------------------------

# 見本Caseは Registry から引く。Case ID を手入力すると、Registry が動いたときに
# 試験だけが古い前提を持ち続ける。
_NOT_APPLICABLE_CASE = "AT-TEST-MANIFEST-001/AMBIGUOUS_EXPECTED"
_REQUIRED_EMPTY_CASE = "AT-SCHEMA-CONDITIONAL-001/CONSUMED_MISSING_ATTEMPT"


def _registry_row(case_id: str) -> dict:
    test_id, _, name = case_id.partition("/")
    rows = yaml.safe_load((REGISTRIES / "tests.yaml").read_text(encoding="utf-8"))["test_cases"]
    return next(r for r in rows if r["test_id"] == test_id and r["case_id"] == name)


def _result_for(case_id: str, fixture_file: Path, events: tuple[str, ...]) -> CaseRunResult:
    row = _registry_row(case_id)
    return CaseRunResult(
        case_id=case_id,
        gate_ids=(),
        input_fixture_path=fixture_file,
        observed_state=row["expected_state"],
        observed_error_code=row["expected_error_code"],
        observed_events=events,
        side_effects=ZERO_EFFECTS,
        actual_subject_id="subject-under-test",
        # 観測しない Case は None のまま。**0 で埋めない。**
        ledger_head_before=None if events is None else 0,
        ledger_head_after=None if events is None else len(events),
        test_node_ids=("tests/unit/x.py::y",),
    )


def test_not_applicable_case_records_null_instead_of_an_empty_list(
    clean_repo: Path, evidence_dir: Path, fixture_file: Path, env_manifest: Path
) -> None:
    """非該当は `null`。`[]` を残すと「観測して0件」と読まれる（§26.2.1）。"""
    result = _result_for(_NOT_APPLICABLE_CASE, fixture_file, ())
    body = json.loads(
        _emit(result, clean_repo, evidence_dir, env_manifest).read_text(encoding="utf-8")
    )

    assert body["evidence_kind"] == "UNIT"
    assert body["event_observation"] == "NOT_APPLICABLE"
    assert body["observed"]["event_sequence"] is None
    assert body["status"] == "PASS"
    # 観測していない層を「一致した」と記録しない。
    assert "event_sequence" not in body["mismatched_fields"]


def test_required_empty_case_records_an_observed_empty_list(
    clean_repo: Path, evidence_dir: Path, fixture_file: Path, env_manifest: Path
) -> None:
    """見て0件だったCaseは `[]` を残す。`null` にしない。"""
    result = _result_for(_REQUIRED_EMPTY_CASE, fixture_file, ())
    body = json.loads(
        _emit(result, clean_repo, evidence_dir, env_manifest).read_text(encoding="utf-8")
    )

    assert body["evidence_kind"] == "ORCHESTRATION"
    assert body["event_observation"] == "OBSERVED_EMPTY"
    assert body["observed"]["event_sequence"] == []


def test_observed_events_yield_the_observed_marker(
    clean_repo: Path, evidence_dir: Path, fixture_file: Path, env_manifest: Path
) -> None:
    body = json.loads(
        _emit(_result(fixture_file), clean_repo, evidence_dir, env_manifest).read_text(
            encoding="utf-8"
        )
    )
    assert body["evidence_kind"] == "ORCHESTRATION"
    assert body["event_observation"] == "OBSERVED"
    assert body["observed"]["event_sequence"] == list(EXPECTED_EVENTS)


def test_exempt_case_carrying_events_is_rejected(
    clean_repo: Path, evidence_dir: Path, fixture_file: Path, env_manifest: Path
) -> None:
    """免除されたCaseがEventを持ち帰ったら止める。

    宣言と観測が食い違っている。どちらが本当か決められないものをPASSにしない。
    """
    result = _result_for(_NOT_APPLICABLE_CASE, fixture_file, ("SOME_EVENT",))
    with pytest.raises(EvidenceEmissionError, match="UNEXPECTED_EVENT_OBSERVATION"):
        _emit(result, clean_repo, evidence_dir, env_manifest)
    assert not list(evidence_dir.rglob("*.json"))


def _active_version() -> str:
    """書込み先の版を Registry から引く。**番号を書き写さない。**"""
    import yaml

    doc = yaml.safe_load(
        (REPO_ROOT / "design-source/registries/evidence-schemas.yaml").read_text(encoding="utf-8")
    )
    entry = next(e for e in doc["evidence_schemas"] if e["schema_name"] == "CaseEvidence")
    return str(entry["active_write_version"])


def test_emitted_evidence_validates_against_the_active_schema(
    clean_repo: Path, evidence_dir: Path, fixture_file: Path, env_manifest: Path
) -> None:
    """生成物が**現行**Schema を満たすこと。Schemaと実装がずれていないことを見る。

    どの版が現行かは Registry の `active_write_version` が正本である。
    ここへ番号を書き写すと、版を上げるたびに黙ってずれる。
    """
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (REPO_ROOT / f"schemas/evidence/CaseEvidence/{_active_version()}.schema.json").read_text(
            encoding="utf-8"
        )
    )
    body = json.loads(
        _emit(_result(fixture_file), clean_repo, evidence_dir, env_manifest).read_text(
            encoding="utf-8"
        )
    )
    jsonschema.Draft202012Validator(schema).validate(body)


def test_evidence_declares_the_active_schema_version(
    clean_repo: Path, evidence_dir: Path, fixture_file: Path, env_manifest: Path
) -> None:
    body = json.loads(
        _emit(_result(fixture_file), clean_repo, evidence_dir, env_manifest).read_text(
            encoding="utf-8"
        )
    )
    assert body["evidence_schema_version"] == _active_version()


def test_missing_policy_on_an_empty_expectation_is_rejected(
    clean_repo: Path, evidence_dir: Path, fixture_file: Path, env_manifest: Path, tmp_path: Path
) -> None:
    """期待Event列が空なのに Policy が無い Case を推測で埋めないこと。

    `REQUIRED_EMPTY`（見て0件）と `NOT_APPLICABLE`（見ない）は意味が正反対で、
    空配列だけからは決まらない。どちらかへ倒さず停止する。

    実 Registry には未宣言の Case が残っていない（Owner が決めた）。
    **その状態に依存しない。** Policy を外した Registry を組んで規則を確かめる。
    在庫を試験の前提にすると、在庫が変わるだけで規則の検査が消える。
    """
    document = yaml.safe_load((REGISTRIES / "tests.yaml").read_text(encoding="utf-8"))
    row = next(
        r
        for r in document["test_cases"]
        if "MVP0-A" in (r.get("phase_scope") or [])
        and not (r.get("expected_event_sequence") or [])
        and r.get("event_observation_policy") is not None
    )
    case_id = f"{row['test_id']}/{row['case_id']}"

    stripped = tmp_path / "registries"
    stripped.mkdir()
    for source in REGISTRIES.iterdir():
        if source.is_file():
            (stripped / source.name).write_bytes(source.read_bytes())
    for candidate in document["test_cases"]:
        if candidate["test_id"] == row["test_id"] and candidate["case_id"] == row["case_id"]:
            candidate.pop("event_observation_policy", None)
    (stripped / "tests.yaml").write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    result = _result_for(case_id, fixture_file, ())
    with pytest.raises(EvidenceEmissionError, match="EVENT_OBSERVATION_POLICY_MISSING"):
        emit_case_evidence(
            result,
            evidence_dir=evidence_dir,
            repo_root=clean_repo,
            environment_manifest=env_manifest,
            registries=stripped,
        )
    assert not list(evidence_dir.rglob("*.json"))


def test_evidence_kind_tampering_is_detected(
    clean_repo: Path, evidence_dir: Path, fixture_file: Path, env_manifest: Path
) -> None:
    """`evidence_kind` の書換えを Hash が捕まえること。

    層を後から書き換えられると、Orchestration の失敗を Unit の記録に見せかけられる。
    """
    path = _emit(_result(fixture_file), clean_repo, evidence_dir, env_manifest)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["evidence_kind"] = "UNIT"
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(EvidenceEmissionError, match="EVIDENCE_HASH_MISMATCH"):
        verify_case_evidence(path)


def test_event_observation_tampering_is_detected(
    clean_repo: Path, evidence_dir: Path, fixture_file: Path, env_manifest: Path
) -> None:
    path = _emit(_result(fixture_file), clean_repo, evidence_dir, env_manifest)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["event_observation"] = "NOT_APPLICABLE"
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(EvidenceEmissionError, match="EVIDENCE_HASH_MISMATCH"):
        verify_case_evidence(path)


def test_version_1_2_evidence_still_verifies(tmp_path: Path) -> None:
    """1.2 の Evidence を従来どおり検証できること。

    2.0 を出すようになっても、過去の記録を読めなくしない。Hash Profile を
    据え置いたのはこのためである。
    """
    import emit_case_evidence as emitter

    body = {
        "evidence_schema_version": "1.2",
        "case_id": "AT-X-001/Y",
        "observed": {"state": "ACCEPTED", "error_code": None, "event_sequence": []},
    }
    body["evidence_hash"] = emitter._evidence_hash(body)
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    verify_case_evidence(path)  # 例外が出ないこと

    body["observed"]["state"] = "REJECTED"
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(EvidenceEmissionError, match="EVIDENCE_HASH_MISMATCH"):
        verify_case_evidence(path)
