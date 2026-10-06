"""pytest の収集範囲が狭まっていないことを固定する試験。

## なぜ要るか

PR #59 の本文が、`tests/integration/ui` と `tests/spec_lint` を渡した実行の
件数（768）を「tests/ 全体」と書いた。**部分実行を全体実行として表示した**。
収集範囲そのものは狭まっていなかったが、報告が事実と食い違った。

同じ取り違えは、報告の書き方だけでなく **設定が実際に狭まったとき**にも起きる。
`testpaths` を狭める、`norecursedirs` を足す、`addopts` へ `-k` を入れる。
どれも「pytest は緑だった」という報告を保ったまま範囲を削れる。

ここで測るのは次である。

* Path を渡さない収集が `tests` を渡した収集と同じ件数であること
* 収集を狭める ini 設定が無いこと
* skip／xfail が 0 件であること
* 検査 Script が `tests/` 全体を pytest へ渡していること
* 監査 Report の内部整合（非公開の監査記録。`tests/private_history/` で確かめる）

## 監査 Report の実行結果を読まない

Report は **1 回の実行を観測した記録**である。その実行の中にいる試験は、観測
結果そのものを表明できない。表明すれば「記録が書かれる前の記録」を読むことに
なり、件数が動いた直後は実装が正しくても必ず落ちる。

読んでよいのは収集・File 走査・Base との差から決まる値だけで、`run_*` の
passed／failed と `verdict` は読まない。それらの鮮度は生成器が自分で確かめる。

## 件数を書き写さない

期待値に絶対数を置かない。**2 つの実行を比べる**か、**0 であること**を測る。
絶対数を書くと、Test が増えるたびにこの File を書き換えることになり、
書き換えるうちに範囲の縮小を通してしまう。
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 収集範囲を狭める ini 設定。**1 つでもあれば範囲が削れる。**
NARROWING_KEYS = ("norecursedirs", "collect_ignore", "collect_ignore_glob")

#: pytest の起動行で収集範囲を狭める引数。
NARROWING_ARGS = ("--ignore", "--ignore-glob", "--deselect", "-k", "-m", "--last-failed", "--lf")

_COLLECTED = re.compile(r"(\d+)\s+tests?\s+collected")


def _collect(*paths: str) -> int:
    """`--collect-only` の件数。**実際に走らせて数える。**"""
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            *paths,
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout[-2000:]
    match = _COLLECTED.search(result.stdout)
    assert match is not None, result.stdout[-2000:]
    return int(match.group(1))


def _ini() -> dict[str, Any]:
    document = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    options: dict[str, Any] = document["tool"]["pytest"]["ini_options"]
    return options


def test_default_collection_covers_the_whole_tests_tree() -> None:
    """Path を渡さない収集が `tests` を渡した収集と同じであること。

    **絶対数を書かない。** 2 つの実行を比べる。
    """
    default = _collect()
    with_path = _collect("tests")
    assert default == with_path, f"Path 指定なし {default} 件、`tests` 指定 {with_path} 件"
    assert default > 0


def test_testpaths_is_the_whole_tests_tree() -> None:
    """`testpaths` が `tests` のままであること。狭められていないこと。"""
    assert _ini()["testpaths"] == ["tests"]


def test_no_narrowing_ini_option_is_declared() -> None:
    """収集を狭める ini 設定が無いこと。"""
    ini = _ini()
    present = sorted(key for key in NARROWING_KEYS if key in ini)
    assert present == [], f"収集を狭める設定がある: {present}"


def test_addopts_does_not_narrow_collection() -> None:
    """`addopts` が収集を狭めないこと。

    Token 一致で見る。`--strict-markers` の中の `-m` を数えない。
    """
    tokens = str(_ini().get("addopts", "")).split()
    narrowing = sorted({t.split("=", 1)[0] for t in tokens} & set(NARROWING_ARGS))
    assert narrowing == [], f"addopts が収集を狭める: {narrowing}"


def test_a_subdirectory_run_is_smaller_than_the_whole_tree() -> None:
    """部分実行が全体より小さいこと。**両者を混同していないことの下限。**"""
    whole = _collect()
    ui = _collect("tests/integration/ui")
    assert 0 < ui < whole, f"UI {ui} 件、全体 {whole} 件"


# ---------------------------------------------------------------------------
# skip / xfail
# ---------------------------------------------------------------------------


def test_no_skip_or_xfail_hides_a_failure() -> None:
    """skip／xfail が 1 件も無いこと（CLAUDE.md #17）。"""
    patterns = (
        re.compile(r"\bpytest\.skip\s*\("),
        re.compile(r"\bpytest\.xfail\s*\("),
        re.compile(r"@pytest\.mark\.skip\b"),
        re.compile(r"@pytest\.mark\.skipif\b"),
        re.compile(r"@pytest\.mark\.xfail\b"),
    )
    hits: list[str] = []
    for path in sorted((REPO_ROOT / "tests").rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if any(pattern.search(line) for pattern in patterns):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert hits == [], "skip／xfail で失敗を隠している:\n" + "\n".join(hits)


# ---------------------------------------------------------------------------
# 検査 Script
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    ["run-checks.sh"],
)
def test_check_scripts_run_the_whole_tests_tree(script: str) -> None:
    """検査 Script が `tests/` 全体を pytest へ渡していること。

    Path を絞った行に差し替えられたら落ちる。
    """
    source = (REPO_ROOT / script).read_text(encoding="utf-8")
    _assert_whole_tree_invocation(source, script)


def _assert_whole_tree_invocation(source: str, script: str) -> None:
    lines = [
        line.strip()
        for line in source.splitlines()
        if "pytest" in line and not line.strip().startswith("#")
    ]
    assert lines, f"{script} が pytest を呼んでいない"
    runs = [line for line in lines if "-m pytest" in line]
    assert len(runs) == 1, f"{script} の pytest 起動が {len(runs)} 行ある: {runs}"
    invocation = runs[0]
    assert " tests/ " in f"{invocation} ", f"{script} が `tests/` 全体を渡していない: {invocation}"
    tokens = invocation.split()
    tail = tokens[tokens.index("pytest", tokens.index("-m")) + 1 :]
    narrowing = sorted({t.split("=", 1)[0] for t in tail} & set(NARROWING_ARGS))
    assert narrowing == [], f"{script} が収集を狭める: {narrowing}"


@pytest.mark.parametrize("replacement", [" tests/unit/ ", " tests/ -k selected "])
def test_narrowed_synthetic_script_is_rejected(replacement: str) -> None:
    """旧実行Scriptの在庫確認は非公開側。公開版では収集縮小を確実に拒否する。"""
    source = "python -m pytest tests/ -q"
    _assert_whole_tree_invocation(source, "synthetic-full")
    narrowed = source.replace(" tests/ ", replacement)
    with pytest.raises(AssertionError, match="全体|狭める"):
        _assert_whole_tree_invocation(narrowed, "synthetic-narrowed")


# ---------------------------------------------------------------------------
# 監査 Report
# ---------------------------------------------------------------------------


# CI が走らせない Test が 0 件であることは、ここでは測らない。
#
# かつては 97 件の差があった（`tests/` 直下の Test File が Workflow の一覧に
# 入っていなかった）。`ci/test-tiers.yaml` を正本にして塞いだ。
#
# その表明をここへ置くと、**その値が動く Commit で必ず 1 世代遅れる**。監査 Tool
# の全体実行はこの試験を走らせ、Report はその後に書かれるからである。実際に
# 29 件から 0 件へ変えたとき、正しい実装のまま落ちた。
#
# `tests/spec_lint/test_ci_suite_coverage.py::test_every_collected_node_is_assigned_to_a_tier`
# が同じことを **その場で収集して**測っている。Report を経由しないので遅れない。
# 所有者をそちらへ一本化する。
