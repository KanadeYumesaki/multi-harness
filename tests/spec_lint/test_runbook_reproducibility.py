"""Runbook の手順が再現可能な形になっているかの静的検査。

## なぜ必要か

Runbook は**Evidenceを生成する環境そのもの**を作る手順である。ここが
再現不能なら、その環境で走らせたTestの結果もEvidenceとして使えない。

レビューで実際に指摘された。§2.2 の初期セットアップだけが
`pip install -r ...`（Hash非強制）のまま残り、後段の §5 だけが
`--require-hashes` を使っていた。厳格な手順を1箇所に書いても、
緩い手順が別の場所に残っていれば、緩い方で作った環境が使われる。

同じくSHA256SUMSを絶対Pathで生成していた。Hashは正しいが、受領者の
Directoryでは `sha256sum -c` が動かない。**検証されない検証手順は
無いのと同じ**である。

散文の規約は読み飛ばされる。機械で確かめる。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = REPO_ROOT / "docs" / "MVP0-A_Runtime_GO再開Runbookと未実装優先順位.md"
WORKFLOWS = (
    REPO_ROOT / ".github" / "workflows" / "ci.yml",
    REPO_ROOT / "ci" / "github-actions-ci.yml",
)


def _runbook() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _pip_install_lines(text: str) -> list[str]:
    """コメントを除いた `pip install` の行を集める。"""
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        if re.search(r"\bpip install\b", line):
            lines.append(line)
    return lines


def test_runbook_exists() -> None:
    assert RUNBOOK.is_file(), f"Runbookが見つからない: {RUNBOOK}"


def test_every_pip_install_enforces_hashes() -> None:
    """`--require-hashes` を伴わない依存導入を残さない。

    `-e .` だけは Hash を持てない（Local Source）。そちらは `--no-deps`
    でなければならない。付け忘れると、Lockの外から推移的依存を引く。
    """
    offenders = []
    for line in _pip_install_lines(_runbook()):
        if "-e ." in line:
            if "--no-deps" not in line:
                offenders.append(f"{line}  ← -e . には --no-deps が要る")
            continue
        if "--require-hashes" not in line:
            offenders.append(f"{line}  ← --require-hashes が無い")
    assert not offenders, "Hash強制でない依存導入がRunbookに残っている:\n  " + "\n  ".join(
        offenders
    )


def test_runbook_does_not_upgrade_pip_outside_the_lock() -> None:
    """`pip install --upgrade pip` を置かない。

    pip自身が requirements-dev.txt へHash固定で載っている。Lockの外から
    最新のpipを取ると、「固定した」と言いながらToolchainだけが毎回変わる。
    Environment Manifest に記録されるpipの版数と、実際に解決に使われた
    pipが食い違う。
    """
    offenders = [line for line in _pip_install_lines(_runbook()) if "--upgrade pip" in line]
    assert not offenders, f"Lock外のpip更新が残っている: {offenders}"


def test_runbook_install_matches_ci() -> None:
    """RunbookとCIが同じ導入方法であること。

    2箇所で違うと、Evidenceを見ても**どちらで作った環境か判別できない**。
    """
    runbook_installs = {
        re.sub(r"\s+", " ", line).replace("python -m pip", "pip")
        for line in _pip_install_lines(_runbook())
    }
    for workflow in WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        ci_installs = {re.sub(r"\s+", " ", line) for line in _pip_install_lines(text)}
        missing = ci_installs - runbook_installs
        assert not missing, f"{workflow.name} にあってRunbookに無い導入手順: {sorted(missing)}"


def test_sha256sums_is_generated_with_relative_paths() -> None:
    """SUMSへ生成側の絶対Pathを焼き付けない。

    受領者のDirectoryで `sha256sum -c` がそのまま動くこと。
    """
    text = _runbook()
    offenders = [
        line.strip()
        for line in text.splitlines()
        if "sha256sum" in line and not line.strip().startswith("#") and re.search(r"\s/[a-z]", line)
    ]
    assert not offenders, "SHA256SUMS を絶対Pathで生成している:\n  " + "\n  ".join(offenders)


def test_runbook_documents_how_to_verify_the_sums() -> None:
    """作り方だけでなく**確かめ方**も書く。

    受領側の手順が無いと、SUMSは添付されているだけで誰も検証しない。
    """
    text = _runbook()
    assert "sha256sum -c" in text, "SUMSの検証手順がRunbookに無い"


def test_mount_crossing_test_is_not_allowed_to_be_skipped() -> None:
    """実bind mount試験のSKIPをPASSとして扱わない旨が明記されていること。

    不変条件#16の運用面。環境が足りないときに緑になる余地を残さない。
    """
    text = _runbook()
    assert "SKIPPED" in text and "PASSではありません" in text
