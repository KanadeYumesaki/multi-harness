"""CI専用だった検査をローカル試験からも呼ぶ（不変条件#16の運用面）。

## なぜ必要か

`design-generation-contract` と `license-allowlist` は **CIにしか無い工程**だった。
そのためローカルで pytest / mypy / ruff / Spec Lint が全て緑でも、
Push して初めて赤が分かる状態が続いた。実際に2回続けてこの形で
CIを落としている（README Hash 陳腐化、requirements解析）。

「ローカルが全部緑」と「CIが緑」は同じではない。**差があるなら、
その差を試験で埋める**。ここはその置き場である。

## 何を確かめるか

CIが実行する `tools/check_design_generation_contract.py` を同じ引数で呼び、
終了Codeが0であること。ToolのLogicを再実装しない。再実装すると
Tool側の変更に追随せず、また差が生まれる。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]


def _design() -> Path:
    design = REPO_ROOT / "design-v1.25-runtime-go.md"
    assert design.is_file(), f"ASCII固定名の設計書が見つからない: {design}"
    return design


def test_design_generation_contract_passes() -> None:
    """設計書・Snapshot・spec-manifest・READMEのHashが揃っている。

    落ちたときは対象を再生成し、READMEのHashを実測値へ同期する。
    """
    result = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
        [
            sys.executable,
            "tools/check_design_generation_contract.py",
            "--design",
            _design().name,
            "--snapshot",
            "registry-snapshot.json",
            "--spec-manifest",
            "spec/spec-manifest.json",
            "--readme",
            "README.md",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "design-generation-contract が不合格。CIでも同じ検査が走る。\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_license_allowlist_passes() -> None:
    """CIの `license-allowlist` 工程と同じToolを呼ぶ。"""
    result = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
        [sys.executable, "tools/check_licenses.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"license-allowlist が不合格:\n{result.stdout}{result.stderr}"


# CLAUDE.md §「機械検査」が定める型検査対象。CIもここと同じでなければならない。
_MYPY_LAYERS = (
    "src/harness/domain",
    "src/harness/ports",
    "src/harness/infrastructure",
    "src/harness/application",
    "src/harness/presentation",
)


def test_ci_type_check_covers_every_layer() -> None:
    """CIのmypy対象がCLAUDE.mdの4層と一致する。

    対象から漏れた層は**CIでしか型検査されない**という以前に、
    そもそも一度も検査されない。実際 `application/` を新設した際、
    ローカル手順の対象リストが古いままで、CIだけが型エラーを検出した。

    ここで固定するのは「CIの定義」であり、実行手順の写しではない。
    手順書とCIが食い違えば、どちらが正かを人が判断することになる。
    """
    for workflow in (".github/workflows/ci.yml", "ci/github-actions-ci.yml"):
        text = (REPO_ROOT / workflow).read_text(encoding="utf-8")
        missing = [layer for layer in _MYPY_LAYERS if layer not in text]
        assert not missing, f"{workflow} のmypy対象に不足がある: {missing}"


def test_all_source_layers_are_type_checked() -> None:
    """`src/harness/` 直下のPackageが全て検査対象に入っている。

    層を新設したときに対象へ足し忘れると、その層だけ型検査から外れる。
    Package側を正として、対象リストの不足を検出する。
    """
    packages = {
        f"src/harness/{path.name}"
        for path in (REPO_ROOT / "src" / "harness").iterdir()
        if path.is_dir() and (path / "__init__.py").exists()
    }
    uncovered = packages - set(_MYPY_LAYERS)
    assert not uncovered, (
        f"型検査対象に入っていない層がある: {sorted(uncovered)}\n"
        "CLAUDE.md と両CI定義、本試験の _MYPY_LAYERS へ追加すること。"
    )


def test_coverage_data_file_is_not_tracked() -> None:
    """`.coverage` は試験のたびに変わる。追跡すると差分がノイズで埋まる。"""
    git = shutil.which("git")
    assert git is not None, "gitが見つからない"
    result = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
        [git, "ls-files", "--error-unmatch", ".coverage"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, ".coverage がGit管理下にある。.gitignoreへ追加すること。"
