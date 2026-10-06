"""P1 Case と Gate Evidence の Emitter 契約試験。

* Fault Injection設定・Design Hash・Registry Snapshot Hash がEvidenceへ入ること
* Gate の status が参照Case の実測からだけ決まること
* Case／Gate が全件そろうまで Release Evidence を生成しないこと
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

from emit_case_evidence import (  # noqa: E402
    CaseRunResult,
    EvidenceEmissionError,
    FaultInjectionSetting,
    SideEffectObservation,
    emit_case_evidence,
)
from emit_gate_evidence import (  # noqa: E402
    assert_release_evidence_complete,
    emit_gate_evidence,
    verify_gate_evidence,
)

REGISTRIES = REPO_ROOT / "design-source" / "registries"

CASE_ID = "AT-CRASH-001/AFTER_EXECUTION_UNKNOWN"
EXPECTED_STATE = "EFFECT_UNKNOWN"
EXPECTED_ERROR = "EFFECT_UNKNOWN"
EXPECTED_EVENTS = (
    "EXECUTION_ATTEMPTED",
    "RECOVERY_STARTED",
    "RECOVERY_DECIDED",
    "EFFECT_UNKNOWN",
)

ZERO = SideEffectObservation(
    network_calls=0,
    process_launches=0,
    workspace_commits=0,
    external_effects=0,
    ledger_effect_attempts=0,
)

FAULT = FaultInjectionSetting(
    fault_point="AFTER_EXECUTION_ATTEMPTED",
    fault_kind="CRASH",
    deterministic=True,
)


@pytest.fixture
def clean_repo(tmp_path: Path) -> Path:
    """設計書とsnapshotを備えたCleanな一時Repository。"""
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
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)  # noqa: S603,S607
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=repo, check=True)  # noqa: S603,S607
    return repo


@pytest.fixture
def evidence_dir(tmp_path: Path) -> Path:
    target = tmp_path / "evidence"
    target.mkdir()
    return target


@pytest.fixture
def support(tmp_path: Path) -> tuple[Path, Path]:
    fixture = tmp_path / "fixture.json"
    fixture.write_text('{"a": 1}\n', encoding="utf-8")
    env = tmp_path / "env.json"
    env.write_text('{"is_wsl2": true}\n', encoding="utf-8")
    return fixture, env


def _result(fixture: Path, **overrides: object) -> CaseRunResult:
    base = CaseRunResult(
        case_id=CASE_ID,
        gate_ids=("MVP0A-GATE-08",),
        input_fixture_path=fixture,
        observed_state=EXPECTED_STATE,
        observed_error_code=EXPECTED_ERROR,
        observed_events=EXPECTED_EVENTS,
        side_effects=ZERO,
        actual_subject_id="attempt-under-test",
        fault_injection=FAULT,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _emit(result: CaseRunResult, repo: Path, evidence: Path, support: tuple[Path, Path]) -> Path:
    return emit_case_evidence(
        result,
        evidence_dir=evidence,
        repo_root=repo,
        environment_manifest=support[1],
        registries=REGISTRIES,
    )


# --------------------------------------------------------------------------
# P1 Case Evidence の必須項目
# --------------------------------------------------------------------------


def test_p1_evidence_carries_every_required_field(
    clean_repo: Path, evidence_dir: Path, support: tuple[Path, Path]
) -> None:
    """指示が列挙した必須項目がすべて入ること。"""
    path = _emit(_result(support[0]), clean_repo, evidence_dir, support)
    body = json.loads(path.read_text(encoding="utf-8"))

    for field in (
        "case_id",
        "test_id",
        "gate_ids",
        "implementation_commit_sha",
        "design_sha256",
        "registry_snapshot_hash",
        "input_fixture_hash",
        "expected",
        "observed",
        "fault_injection",
        "side_effects",
        "evidence_hash",
    ):
        assert field in body, field

    assert body["status"] == "PASS"
    assert body["test_id"] == "AT-CRASH-001"
    assert body["fault_injection"]["fault_point"] == "AFTER_EXECUTION_ATTEMPTED"
    assert body["fault_injection"]["deterministic"] is True
    assert body["design_sha256"].startswith("sha256:")
    assert body["registry_snapshot_hash"].startswith("sha256:")


def test_fault_injection_setting_is_bound_to_the_hash(
    clean_repo: Path, evidence_dir: Path, support: tuple[Path, Path], tmp_path: Path
) -> None:
    """Fault設定が変わればEvidence Hashも変わること。

    どの条件下での観測かを差し替えられないようにする。
    """
    first = _emit(_result(support[0]), clean_repo, evidence_dir, support)
    other = tmp_path / "evidence-b"
    other.mkdir()
    second = _emit(
        _result(support[0], fault_injection=replace(FAULT, fault_kind="DISK_FULL")),
        clean_repo,
        other,
        support,
    )
    a = json.loads(first.read_text(encoding="utf-8"))
    b = json.loads(second.read_text(encoding="utf-8"))
    assert a["evidence_hash"] != b["evidence_hash"]


def test_missing_design_binding_is_rejected(
    tmp_path: Path, evidence_dir: Path, support: tuple[Path, Path]
) -> None:
    """設計書／snapshotが無ければEvidenceを作らないこと。"""
    repo = tmp_path / "bare"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)  # noqa: S603,S607
    subprocess.run(  # noqa: S603
        ["git", "config", "user.email", "t@example.invalid"],  # noqa: S607
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)  # noqa: S603,S607
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)  # noqa: S603,S607
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=repo, check=True)  # noqa: S603,S607

    with pytest.raises(EvidenceEmissionError, match="MISSING_DESIGN_BINDING"):
        _emit(_result(support[0]), repo, evidence_dir, support)


def test_side_effects_none_is_still_fail_for_p1(
    clean_repo: Path, evidence_dir: Path, support: tuple[Path, Path]
) -> None:
    """P1でも副作用観測の欠落はFAILであること。"""
    path = _emit(_result(support[0], side_effects=None), clean_repo, evidence_dir, support)
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["status"] == "FAIL"
    assert "side_effect_observation_missing" in body["mismatched_fields"]


# --------------------------------------------------------------------------
# Gate Evidence
# --------------------------------------------------------------------------


def _gate(evidence_dir: Path, repo: Path, cases: list[Path], gate_id: str = "MVP0A-GATE-08"):
    return emit_gate_evidence(
        gate_id,
        case_evidence_paths=cases,
        evidence_dir=evidence_dir,
        repo_root=repo,
        registries=REGISTRIES,
    )


def test_gate_passes_only_when_every_referenced_case_passes(
    clean_repo: Path, evidence_dir: Path, support: tuple[Path, Path]
) -> None:
    case = _emit(_result(support[0]), clean_repo, evidence_dir, support)
    result = _gate(evidence_dir, clean_repo, [case])
    assert result.status == "PASS"
    body = json.loads(result.path.read_text(encoding="utf-8"))
    assert body["referenced_case_ids"] == [CASE_ID]
    assert body["referenced_test_ids"] == ["AT-CRASH-001"]
    assert body["failing_case_ids"] == []
    assert body["registry_expectation_match"] is True
    verify_gate_evidence(result.path)


def test_gate_fails_when_a_referenced_case_fails(
    clean_repo: Path, evidence_dir: Path, support: tuple[Path, Path]
) -> None:
    """Caseが落ちているのにGateがPASSする経路を作らないこと。"""
    case = _emit(_result(support[0], observed_state="SUCCEEDED"), clean_repo, evidence_dir, support)
    result = _gate(evidence_dir, clean_repo, [case])
    assert result.status == "FAIL"
    body = json.loads(result.path.read_text(encoding="utf-8"))
    assert body["failing_case_ids"] == [CASE_ID]
    assert body["registry_expectation_match"] is False


def test_gate_rejects_tampered_case_evidence(
    clean_repo: Path, evidence_dir: Path, support: tuple[Path, Path]
) -> None:
    """改変されたCase Evidenceの上にGateを立てないこと。"""
    case = _emit(_result(support[0], observed_state="SUCCEEDED"), clean_repo, evidence_dir, support)
    body = json.loads(case.read_text(encoding="utf-8"))
    body["status"] = "PASS"
    case.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(EvidenceEmissionError, match="EVIDENCE_HASH_MISMATCH"):
        _gate(evidence_dir, clean_repo, [case])


def test_gate_with_no_referenced_cases_is_rejected(clean_repo: Path, evidence_dir: Path) -> None:
    """参照Caseが無いGateをPASSにしないこと。"""
    with pytest.raises(EvidenceEmissionError, match="NO_REFERENCED_CASES"):
        _gate(evidence_dir, clean_repo, [])


def test_unknown_gate_id_is_rejected(
    clean_repo: Path, evidence_dir: Path, support: tuple[Path, Path]
) -> None:
    case = _emit(_result(support[0]), clean_repo, evidence_dir, support)
    with pytest.raises(EvidenceEmissionError, match="UNKNOWN_GATE"):
        _gate(evidence_dir, clean_repo, [case], gate_id="MVP0A-GATE-999")


def test_gate_evidence_reuse_is_rejected(
    clean_repo: Path, evidence_dir: Path, support: tuple[Path, Path]
) -> None:
    case = _emit(_result(support[0]), clean_repo, evidence_dir, support)
    _gate(evidence_dir, clean_repo, [case])
    with pytest.raises(EvidenceEmissionError, match="EVIDENCE_REUSE"):
        _gate(evidence_dir, clean_repo, [case])


def test_gate_status_is_not_an_input_parameter() -> None:
    """GateのstatusもCLI／引数から指定できないこと。"""
    import inspect

    assert "status" not in inspect.signature(emit_gate_evidence).parameters
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(REPO_ROOT / "tools" / "emit_gate_evidence.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 4
    assert "StatusをCLIから指定する経路は無い" in result.stderr


# --------------------------------------------------------------------------
# Release完全性
# --------------------------------------------------------------------------


def test_partial_case_evidence_cannot_produce_release_evidence(
    clean_repo: Path, evidence_dir: Path, support: tuple[Path, Path]
) -> None:
    """全件そろわないうちはRelease Evidenceを生成しないこと。

    80件でも足りない。110 Case／45 Gate すべてが要る。
    """
    _emit(_result(support[0]), clean_repo, evidence_dir, support)
    with pytest.raises(EvidenceEmissionError) as excinfo:
        assert_release_evidence_complete(evidence_dir, REGISTRIES)
    message = str(excinfo.value)
    assert "INCOMPLETE_CASE_EVIDENCE" in message
    assert "INCOMPLETE_GATE_EVIDENCE" in message


def test_release_completeness_counts_come_from_the_registry() -> None:
    """必要件数をCodeへ手入力していないこと（不変条件#18）。"""
    import emit_gate_evidence as module

    cases, gates = module._required_counts(REGISTRIES)

    # 期待値も Registry から採る。数字を書くと、Registry が動いたときに
    # 「Code へ手入力していない」ことを確かめるはずの試験自身が手入力になる。
    snapshot = json.loads((REPO_ROOT / "registry-snapshot.json").read_text(encoding="utf-8"))
    scope = snapshot["scopes"]["MVP0-A"]
    assert cases == scope["required_case_count"], cases
    assert gates == scope["required_gate_count"], gates


def test_empty_evidence_dir_is_incomplete(evidence_dir: Path) -> None:
    """Evidenceが1件も無い状態を完全とみなさないこと。"""
    with pytest.raises(EvidenceEmissionError, match="INCOMPLETE_CASE_EVIDENCE"):
        assert_release_evidence_complete(evidence_dir, REGISTRIES)
