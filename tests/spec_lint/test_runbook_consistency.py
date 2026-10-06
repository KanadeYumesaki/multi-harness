"""Runbook自体のPath・Hash・設計Version参照を検査する。

Runbookは**人が読んで手で実行する**手順である。だから壊れていても
どの検査も落ちない。落ちないまま、実行した人の手元でだけ失敗する。

特に危ないのは、Runbookが**自分自身の前提を壊す**形の矛盾である。
改修前は次の2つを同じ手順のなかで要求していた。

1. Evidenceを `runtime-evidence/<release-id>/`（Repository内）へ出力する
2. `git status --short` が空であること

両立しない。1を実行した時点で2が壊れる。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = REPO_ROOT / "docs" / "MVP0-A_Runtime_GO再開Runbookと未実装優先順位.md"
TEXT = RUNBOOK.read_text(encoding="utf-8")

sys.path.insert(0, str(REPO_ROOT / "tools"))
from check_design_reference_currency import CURRENT_DESIGN, LEGACY_NAMES  # noqa: E402


def test_runbook_exists() -> None:
    assert RUNBOOK.is_file()


def _shell_lines() -> list[str]:
    """Code Fence内の実行行だけを返す。

    禁止しているのは**実行されるPath**であって、説明文がPathを引用することでは
    ない。「Repository内へ出力しない」と書いた注意書き自体が引っかかるようでは、
    検査が説明を書けなくする。
    """
    lines: list[str] = []
    inside = False
    for raw in TEXT.splitlines():
        stripped = raw.strip()
        if stripped.startswith("```"):
            inside = stripped != "```"
            continue
        if inside:
            lines.append(raw)
    return lines


def test_evidence_is_never_written_inside_the_repository() -> None:
    """実行行がRepository**相対**のEvidence Pathを持たないこと。

    `runtime-evidence/<release-id>/...` への出力が1つでもあれば、
    その手順は自分で `git status --short` を汚す。

    `$HOME/runtime-evidence/...` と `$EVIDENCE_DIR/...` は Repository外であり
    正しい。禁じるのは接頭辞の無い相対Pathだけである。
    """
    relative_output = re.compile(r"(?<![\w/${}\-])runtime-evidence/")
    offenders = [
        line.strip()
        for line in _shell_lines()
        if relative_output.search(line) and "$HOME/runtime-evidence/" not in line
    ]
    assert offenders == [], f"Repository内へEvidenceを出力している: {offenders}"


def test_no_shell_redirect_targets_the_repository_tree() -> None:
    """Redirect先がRepository相対Pathでないこと。

    `> runtime-evidence/...` のような行が残っていれば、実行した瞬間に
    ソースツリーが汚れる。
    """
    bad = [
        line.strip()
        for line in TEXT.splitlines()
        if re.search(r">\s*(?!\"?\$)(?:\./)?runtime-evidence/", line)
    ]
    assert bad == [], f"Repository相対へRedirectしている: {bad}"


def test_evidence_dir_is_defined_before_first_use() -> None:
    """`EVIDENCE_DIR` を最初の使用より前に定義していること。

    定義前に使うと、空文字へ展開されて予期しない場所へ書き込む。
    """
    define = TEXT.find("export EVIDENCE_DIR=")
    assert define != -1, "EVIDENCE_DIR の定義が無い"
    first_use = TEXT.find('"$EVIDENCE_DIR')
    assert first_use != -1, "EVIDENCE_DIR を使っていない"
    assert define < first_use, "EVIDENCE_DIR の定義が最初の使用より後にある"


def test_release_id_is_defined_before_first_use() -> None:
    define = TEXT.find("export RELEASE_ID=")
    assert define != -1, "RELEASE_ID の定義が無い"
    # 定義行より前に `$RELEASE_ID` を参照していないこと。
    before = TEXT[:define]
    assert "$RELEASE_ID" not in before, "RELEASE_ID を定義前に参照している"


def test_evidence_dir_points_outside_the_repository() -> None:
    """`EVIDENCE_DIR` が `$HOME` 配下であること。"""
    match = re.search(r'export EVIDENCE_DIR="([^"]+)"', TEXT)
    assert match is not None
    assert match.group(1).startswith("$HOME/"), match.group(1)


def test_runbook_references_the_current_design() -> None:
    """現行設計書を参照し、旧版を指していないこと。"""
    assert CURRENT_DESIGN in TEXT, f"{CURRENT_DESIGN} を参照していない"
    for legacy in LEGACY_NAMES:
        assert legacy not in TEXT, f"旧設計書名を参照している: {legacy}"


def test_runbook_forbids_windows_mounts_explicitly() -> None:
    """`/mnt/c`・drvfs・9p・overlay 上でEvidenceを生成しないと明記していること。"""
    for token in ("/mnt/c", "drvfs", "9p", "overlay"):
        assert token in TEXT, f"禁止filesystemの明記が無い: {token}"
    assert "Evidenceを生成しない" in TEXT


def test_referenced_tools_and_paths_exist() -> None:
    """Runbookが指すTool・Fileが実在すること。

    存在しないPathを指す手順は、実行した人の手元でだけ失敗する。
    """
    referenced = set(re.findall(r"(tools/[A-Za-z0-9_./-]+\.py)", TEXT))
    referenced |= set(re.findall(r"\b(run_verification\.sh)\b", TEXT))
    referenced |= set(re.findall(r"\b(requirements(?:-dev)?\.txt)\b", TEXT))
    referenced |= set(re.findall(r"\b(runtime-go-manifest\.[A-Za-z0-9-]+\.template\.json)\b", TEXT))
    assert referenced, "Runbookが1つもToolを参照していない"
    missing = sorted(name for name in referenced if not (REPO_ROOT / name).exists())
    assert missing == [], f"Runbookが存在しないPathを指している: {missing}"


def test_evidence_destination_checker_is_wired_in() -> None:
    """出力先の機械検査を手順へ組み込んでいること。

    文章で「Repository外へ置く」と書くだけでは守られない。
    """
    assert "tools/check_evidence_destination.py" in TEXT
    assert (REPO_ROOT / "tools" / "check_evidence_destination.py").is_file()


def test_checker_rejects_repository_internal_destination(tmp_path: Path) -> None:
    """検査器がRepository内出力を実際に拒否すること。"""
    workspace = tmp_path / "repo"
    inside = workspace / "runtime-evidence"
    inside.mkdir(parents=True)
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "check_evidence_destination.py"),
            "--evidence-dir",
            str(inside),
            "--workspace",
            str(workspace),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 3
    assert "EVIDENCE_IN_REPOSITORY" in result.stderr


def test_checker_accepts_a_sibling_destination(tmp_path: Path) -> None:
    """Repository外の兄弟Directoryを受け入れること。"""
    workspace = tmp_path / "repo"
    outside = tmp_path / "evidence"
    workspace.mkdir()
    outside.mkdir()
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "check_evidence_destination.py"),
            "--evidence-dir",
            str(outside),
            "--workspace",
            str(workspace),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_synthetic_historical_documents_are_not_rewritten(tmp_path: Path, monkeypatch) -> None:
    """合成の履歴文書を検査器が書換えず、旧参照の消失は拒否する。私的履歴は別監査。"""
    import check_design_reference_currency as checker

    document = tmp_path / "synthetic-history.md"
    original = ("SYNTHETIC HISTORY\n" + LEGACY_NAMES[0] + "\n").encode()
    document.write_bytes(original)
    (tmp_path / CURRENT_DESIGN).write_text("synthetic current design\n")
    monkeypatch.setattr(
        checker,
        "ALLOWED_HISTORICAL_REFERENCES",
        {document.name: "synthetic historical reference fixture"},
    )
    assert checker.main(["--root", str(tmp_path)]) == 0
    assert document.read_bytes() == original
    altered = b"synthetic historical reference incorrectly erased\n"
    document.write_bytes(altered)
    assert checker.main(["--root", str(tmp_path)]) == 1
    assert document.read_bytes() == altered, "Checker must not silently repair history"
