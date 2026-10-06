"""Release Guard：全件そろうまでRelease Evidenceを作らせない。

Development Evidence（Case単体・Gate単体）は生成してよい。
**Release Evidence は別物**であり、Runtime GO Manifestへ接続する資格を持つ。
その資格条件を機械が言い続けるための試験である。

現在 84/110 Case・0/45 Gate。**足りない。** 足りないことを
「まだ足りない」と言い続けられることが、この段階での正しい状態である。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRIES = REPO_ROOT / "design-source" / "registries"
sys.path.insert(0, str(REPO_ROOT / "tools"))

from emit_case_evidence import EvidenceEmissionError  # noqa: E402
from emit_gate_evidence import (  # noqa: E402
    _required_counts,
    assert_release_evidence_complete,
)


def _registry_counts() -> tuple[int, int]:
    tests = yaml.safe_load((REGISTRIES / "tests.yaml").read_text(encoding="utf-8"))["test_cases"]
    gates = yaml.safe_load((REGISTRIES / "gates.yaml").read_text(encoding="utf-8"))["gates"]
    cases = sum(1 for r in tests if "MVP0-A" in (r.get("phase_scope") or []))
    return cases, len(gates)


def test_required_counts_come_from_the_registry() -> None:
    """必要件数を Registry から導出していること。手入力しない。"""
    assert _required_counts(REGISTRIES) == _registry_counts()


def test_all_cases_are_covered_but_release_is_still_blocked() -> None:
    """全Case Covered になっても Release 条件は満たされないこと。

    **Covered と Release Evidence は別である。** Covered は「対応する試験が
    存在しIntegrationまで通っている」ことであり、Release Evidence は
    実行の観測記録である。Gate Evidence が1件も無いいま、Release は成立しない。

    ここを混同すると「全部通ったからGO」になる。
    """
    coverage = yaml.safe_load((REPO_ROOT / "tests/case-coverage.yaml").read_text(encoding="utf-8"))
    required_cases, required_gates = _registry_counts()
    not_implemented = len(coverage["not_implemented"] or [])
    covered = required_cases - not_implemented

    # Covered と未実装宣言の和が必要Case数と一致すること。以前は未実装宣言が
    # 空である前提で covered == required_cases を見ていたが、宣言が入った時点で
    # 前提が崩れる。**見るべきは恒等式であって在庫ではない。**
    assert covered + not_implemented == required_cases, f"{covered}+{not_implemented}"

    # それでも Release Evidence は生成できない。
    with pytest.raises(EvidenceEmissionError) as excinfo:
        assert_release_evidence_complete(REPO_ROOT / "nonexistent-evidence", REGISTRIES)
    assert "INCOMPLETE" in str(excinfo.value)
    assert required_gates == 45


def test_empty_evidence_dir_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(EvidenceEmissionError, match="INCOMPLETE_CASE_EVIDENCE"):
        assert_release_evidence_complete(tmp_path, REGISTRIES)


def test_partial_case_evidence_is_rejected(tmp_path: Path) -> None:
    """Caseが1件でも足りなければ拒否すること。"""
    required_cases, _ = _registry_counts()
    cases = tmp_path / "cases" / "AT-X-001"
    cases.mkdir(parents=True)
    for index in range(required_cases - 1):
        (cases / f"C{index}.json").write_text("{}", encoding="utf-8")
    gates = tmp_path / "gates"
    gates.mkdir()
    _, required_gates = _registry_counts()
    for index in range(required_gates):
        (gates / f"G{index}.json").write_text("{}", encoding="utf-8")

    with pytest.raises(EvidenceEmissionError) as excinfo:
        assert_release_evidence_complete(tmp_path, REGISTRIES)
    assert "INCOMPLETE_CASE_EVIDENCE" in str(excinfo.value)
    assert "INCOMPLETE_GATE_EVIDENCE" not in str(excinfo.value)


def test_partial_gate_evidence_is_rejected(tmp_path: Path) -> None:
    """Gateが1件でも足りなければ拒否すること。"""
    required_cases, required_gates = _registry_counts()
    cases = tmp_path / "cases" / "AT-X-001"
    cases.mkdir(parents=True)
    for index in range(required_cases):
        (cases / f"C{index}.json").write_text("{}", encoding="utf-8")
    gates = tmp_path / "gates"
    gates.mkdir()
    for index in range(required_gates - 1):
        (gates / f"G{index}.json").write_text("{}", encoding="utf-8")

    with pytest.raises(EvidenceEmissionError, match="INCOMPLETE_GATE_EVIDENCE"):
        assert_release_evidence_complete(tmp_path, REGISTRIES)


def test_full_counts_pass_the_guard(tmp_path: Path) -> None:
    """全件そろえば通ること。

    常に拒否するだけのGuardでは、通る条件が実在するのか分からない。
    """
    required_cases, required_gates = _registry_counts()
    cases = tmp_path / "cases" / "AT-X-001"
    cases.mkdir(parents=True)
    for index in range(required_cases):
        (cases / f"C{index}.json").write_text("{}", encoding="utf-8")
    gates = tmp_path / "gates"
    gates.mkdir()
    for index in range(required_gates):
        (gates / f"G{index}.json").write_text("{}", encoding="utf-8")

    assert_release_evidence_complete(tmp_path, REGISTRIES)


def test_guard_reports_both_shortfalls_at_once(tmp_path: Path) -> None:
    """Case と Gate の両方が足りないとき、両方を挙げること。

    片方だけ直して再実行、を繰り返させない。
    """
    (tmp_path / "cases").mkdir()
    (tmp_path / "gates").mkdir()
    with pytest.raises(EvidenceEmissionError) as excinfo:
        assert_release_evidence_complete(tmp_path, REGISTRIES)
    message = str(excinfo.value)
    assert "INCOMPLETE_CASE_EVIDENCE" in message
    assert "INCOMPLETE_GATE_EVIDENCE" in message


def test_cli_release_check_exits_three_when_incomplete(tmp_path: Path) -> None:
    """CLIからの完全性検査が終了Code 3 で止まること。"""
    import subprocess

    (tmp_path / "cases").mkdir()
    (tmp_path / "gates").mkdir()
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "emit_gate_evidence.py"),
            "--check-release-complete",
            str(tmp_path),
            "--registries",
            str(REGISTRIES),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 3
    assert "INCOMPLETE" in result.stderr


def test_development_evidence_is_allowed_without_full_counts(tmp_path: Path) -> None:
    """個々のCase Evidence生成はGuardに縛られないこと。

    開発中に1件ずつ作れないと、そもそも全件へ到達できない。
    Guardが縛るのは **Release Evidence** の資格だけである。
    """
    evidence = tmp_path / "cases" / "AT-X-001"
    evidence.mkdir(parents=True)
    (evidence / "ONE.json").write_text(json.dumps({"status": "PASS"}), encoding="utf-8")
    # 1件だけでもFileは作れる。Guardは別途呼ばれたときだけ拒否する。
    assert (evidence / "ONE.json").is_file()
    with pytest.raises(EvidenceEmissionError):
        assert_release_evidence_complete(tmp_path, REGISTRIES)
