"""CI が全 Test を走らせていることを固定する試験。

## なぜ要るか

`tests/` 直下の 4 File は、どの Suite Directory にも属さないため
`.github/workflows/ci.yml` と `ci/github-actions-ci.yml` のどちらの一覧にも
入っておらず、**29 件が CI のどこでも走っていなかった**。Directory を並べる方式
では、Directory に属さない Test が静かに落ちる。

同じ落ち方は次にも起きる。新しい Suite を作って正本へ書き忘れる、Test File を
`tests/` 直下へ足す、Suite Directory を改名する。**どれも「CI は緑だった」と
いう報告を保ったまま範囲を削れる。**

## File 名を固定しない

`test_package_release_zip.py` を名指しで探す検査では、Test の追加・分割・
Parametrize を追えない。ここで測るのは **Node ID の集合**と、正本が宣言した
Path の実在である。

## 監査 Report の実行結果を読まない

`test_test_scope.py` と同じ理由である。Report は 1 回の観測の記録であり、
その観測に含まれる試験は観測結果そのものを表明できない。ここでは Report を
読まず、**その場で収集して**測る。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY = REPO_ROOT / "ci/test-tiers.yaml"
GENERATOR = REPO_ROOT / "tools/ci_suites.py"
WORKFLOWS = (
    REPO_ROOT / ".github/workflows/ci.yml",
    REPO_ROOT / "ci/github-actions-ci.yml",
)

#: 収集範囲を狭める引数。`pytest` より後ろに出たら記録する。
NARROWING_ARGS = ("--ignore", "--ignore-glob", "--deselect", "-k", "-m", "--last-failed", "--lf")

#: Job 単位の `if` を持ってよい Job。**ここ以外に増えたら required check が消える。**
JOB_LEVEL_IF_ALLOWED = {"release-tag"}

sys.path.insert(0, str(REPO_ROOT / "tools"))
from ci_suites import (  # noqa: E402
    covered_paths_for,
    load_registry,
    paths_for,
    scripts_for,
    tiers,
)


def _collect(*paths: str) -> set[str]:
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
    return {line.strip() for line in result.stdout.splitlines() if "::" in line}


def _registry() -> dict[str, Any]:
    document: dict[str, Any] = load_registry(REGISTRY)
    return document


def _assigned_nodes() -> set[str]:
    """どこかの Tier が走らせる Node ID の和集合。"""
    document = _registry()
    found: set[str] = set()
    for tier in tiers(document):
        paths = covered_paths_for(document, tier, repo_root=REPO_ROOT)
        if paths:
            found |= _collect(*paths)
        for entry in scripts_for(document, tier):
            found |= _collect(entry["path"])
    return found


def _workflow(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return document


def _run_steps(document: dict[str, Any]) -> list[tuple[str, str, str]]:
    """`(job, step, run の中身)`。"""
    found: list[tuple[str, str, str]] = []
    for job_name, job in (document.get("jobs") or {}).items():
        for index, step in enumerate(job.get("steps") or []):
            command = step.get("run")
            if isinstance(command, str):
                found.append((job_name, str(step.get("name", index)), command))
    return found


def _pytest_lines(document: dict[str, Any]) -> list[tuple[str, str, str]]:
    """`(job, step, 行)`。`pytest` で始まる行だけを返す。"""
    found: list[tuple[str, str, str]] = []
    for job_name, step_name, command in _run_steps(document):
        for line in command.splitlines():
            stripped = line.strip()
            if stripped.startswith("pytest ") or stripped == "pytest":
                found.append((job_name, step_name, stripped))
    return found


_ARRAY_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\(")


def _suite_list_lines(document: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Suite の一覧が現れうる行。

    `pytest` の起動行と、Shell の配列代入の両方を見る。**書き戻しは配列代入の
    形で入ってくる**ので、`pytest` 行だけを見ていると素通りする。

    `-k` を伴う行は主 Suite ではなく狙い撃ちなので除く。
    """
    found: list[tuple[str, str, str]] = []
    for job_name, step_name, command in _run_steps(document):
        for line in command.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            is_pytest = stripped.startswith("pytest ") or stripped == "pytest"
            is_array = bool(_ARRAY_ASSIGN.match(stripped))
            if not (is_pytest or is_array):
                continue
            if "-k" in stripped.split():
                continue
            found.append((job_name, step_name, stripped))
    return found


# ---------------------------------------------------------------------------
# 1〜3, 9, 10: 未割当を 0 件にする
# ---------------------------------------------------------------------------


def test_every_collected_node_is_assigned_to_a_tier() -> None:
    """全 Node ID が、少なくとも 1 つの Tier へ割り当たっていること。

    **1 件でも余ったら失敗する。** Test File を `tests/` 直下へ足して正本へ
    書き忘れると、ここで落ちる。
    """
    unassigned = sorted(_collect() - _assigned_nodes())
    assert unassigned == [], (
        f"どの Tier にも属さない Node ID が {len(unassigned)} 件ある。"
        f"ci/test-tiers.yaml へ足すこと:\n" + "\n".join(unassigned[:20])
    )


def test_every_declared_required_path_exists_and_has_tests() -> None:
    """正本が宣言した必須 Path が実在し、Node を持っていること。

    File を消したり改名したりして「対象が減っただけ」にできないようにする。
    未割当検査だけでは、消えた Test を検出できない。
    """
    document = _registry()
    for entry in document["suites"]:
        if entry.get("optional", False):
            continue
        path = REPO_ROOT / entry["path"]
        assert path.exists(), f"正本にあるのに存在しない: {entry['path']}"
        assert _collect(entry["path"]), f"Node を 1 件も持たない: {entry['path']}"
    for entry in document["scripts"]:
        assert (REPO_ROOT / entry["path"]).exists(), f"存在しない: {entry['path']}"


def test_the_root_level_test_files_are_assigned() -> None:
    """`tests/` 直下の Test File が、どれも割り当たっていること。

    **File 名を固定しない。** 直下にある `test_*.py` を列挙し、その Node が
    全部どこかの Tier に入っていることを測る。File を増やしても効く。
    """
    root_files = sorted(p.name for p in REPO_ROOT.glob("tests/test_*.py"))
    assert root_files, "tests/ 直下に Test File が 1 つも無い"
    assigned = _assigned_nodes()
    for name in root_files:
        nodes = _collect(f"tests/{name}")
        assert nodes, f"tests/{name} が Node を持たない"
        missing = sorted(nodes - assigned)
        assert missing == [], f"tests/{name} の Node が未割当: {missing[:5]}"


# ---------------------------------------------------------------------------
# 4: Tier ごとの分類が読める
# ---------------------------------------------------------------------------


def test_each_tier_resolves_to_paths_that_exist() -> None:
    """各 Tier の Path が実在するものだけになること。

    存在しない Path を pytest へ渡すと「引数が無い」で落ち、CI が恒常的に
    赤くなって警報として機能しなくなる。
    """
    document = _registry()
    for tier in tiers(document):
        for path in paths_for(document, tier, repo_root=REPO_ROOT):
            assert (REPO_ROOT / path).exists(), f"{tier}: {path} が存在しない"


def test_draft_is_lighter_than_ready() -> None:
    """Draft が Ready より軽いこと。**Draft で重い検査を増やさない。**"""
    document = _registry()
    draft = set(paths_for(document, "draft", repo_root=REPO_ROOT))
    ready = set(paths_for(document, "ready", repo_root=REPO_ROOT))
    assert draft < ready, f"draft {sorted(draft)} が ready {sorted(ready)} の真部分集合でない"


def test_release_package_test_is_not_in_draft() -> None:
    """Release Package の検証を Draft ごとに繰り返さないこと。"""
    document = _registry()
    entry = next(
        item for item in document["suites"] if item["path"].endswith("test_package_release_zip.py")
    )
    assert "draft" not in entry["tiers"], "Release Package 検査が Draft に入っている"
    assert "full" in entry["tiers"], "Release Package 検査が Full に入っていない"


def test_deep_suites_are_not_in_the_main_step() -> None:
    """deep 専用 Suite が主 Step の Path へ入らないこと。

    入れると full の主 Step と nightly の両方で走り、同じ Test を二重に実行する。
    """
    document = _registry()
    deep_only = {entry["path"] for entry in document["suites"] if entry.get("group") == "deep"}
    assert deep_only, "deep 専用 Suite が 1 件も宣言されていない"
    for tier in tiers(document):
        # **存在で絞らない。** 未作成の Suite でも、宣言が主 Step 側にあれば
        # 作られた瞬間に二重実行が始まる。宣言そのものを見る。
        main = set(paths_for(document, tier, repo_root=REPO_ROOT, existing_only=False))
        assert not (main & deep_only), f"{tier}: deep Suite が主 Step へ入っている"


# ---------------------------------------------------------------------------
# 5〜8: Workflow の実解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_reads_the_suite_registry(path: Path) -> None:
    """Workflow が Suite の一覧を持たず、生成器から読むこと。

    **Directory が Workflow に書いてあることを「実行済み」の根拠にしない。**
    一覧を 2 つの Workflow へ写すと、片方だけ直したときに静かに食い違う。
    """
    document = _workflow(path)

    # Coverage を測る主 Step が、生成器から一覧を取っていること。
    # File のどこかに生成器の名があるだけでは足りない（別 Step に残っていても
    # 主 Step が直書きへ戻れる）。
    main_steps = [
        (job, step, command)
        for job, step, command in _run_steps(document)
        if "--cov-fail-under" in command
    ]
    assert main_steps, f"{path.name}: Coverage を測る Step が無い"
    for job, step, command in main_steps:
        assert "tools/ci_suites.py" in command, (
            f"{path.name} {job}/{step} が Suite 生成器を呼んでいない"
        )

    declared = {entry["path"] for entry in _registry()["suites"]}
    offenders = [
        (job, step, line, token)
        for job, step, line in _suite_list_lines(document)
        for token in line.split()
        if token.strip("\"'()") in declared
    ]
    assert offenders == [], f"{path.name} が正本 Path を直書きしている: {offenders}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_does_not_narrow_collection(path: Path) -> None:
    """主 Suite の実行が `--ignore`／`--deselect`／`-k` で削られていないこと。

    deep の狙い撃ちは別である。主 Suite を渡す行だけを見る。
    """
    document = _workflow(path)
    for job, step, line in _pytest_lines(document):
        if job == "nightly":
            # nightly は狙い撃ちで `-k` を使う。主 Suite ではない。
            continue
        tokens = line.split()
        narrowing = sorted({t.split("=", 1)[0] for t in tokens} & set(NARROWING_ARGS))
        assert narrowing == [], f"{path.name} {job}/{step} が収集を狭める: {narrowing}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_new_job_level_if_removes_a_required_check(path: Path) -> None:
    """Job 単位の `if` が増えていないこと。

    Job ごと skip すると required check が消える。対象外の Job は起動させて、
    先頭 Step で not-applicable として成功終了する。
    """
    document = _workflow(path)
    gated = {name for name, job in (document.get("jobs") or {}).items() if job.get("if")}
    assert gated <= JOB_LEVEL_IF_ALLOWED, f"{path.name}: Job 単位の if が増えた: {sorted(gated)}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_step_declares_the_same_key_twice(path: Path) -> None:
    """同じ Key を 2 度書いていないこと。

    YAML は後を採る。`if:` を 2 度書くと先に書いた Guard が **黙って捨てられる**。
    書いてあるのに効いていない、いちばん見つけにくい形である。
    `ci/github-actions-ci.yml` の nightly 4 Step が実際にこれだった。
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    duplicates = [
        (index + 1, lines[index].strip())
        for index in range(len(lines) - 1)
        if lines[index].strip().startswith("if:") and lines[index + 1].strip().startswith("if:")
    ]
    assert duplicates == [], f"{path.name}: `if:` が連続している: {duplicates}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_coverage_threshold_cannot_round_a_shortfall_up_to_success(path: Path) -> None:
    commands = [
        command for _, _, command in _run_steps(_workflow(path)) if "--cov-fail-under" in command
    ]
    assert commands
    for command in commands:
        assert "--cov-fail-under=90" in command
        assert "--cov-precision=2" in command


@pytest.mark.parametrize("covered_body_lines,expected_rc", [(446, 1), (448, 0)])
def test_actual_coverage_exit_code_rejects_the_observed_shortfall(
    tmp_path: Path, covered_body_lines: int, expected_rc: int
) -> None:
    """An independent subject measures 89.6% and 90%, not a mocked plugin outcome."""
    total_body_lines = 498
    lines = ["def covered():"]
    lines += ["    value = 1"] * (covered_body_lines - 1) + ["    return 1"]
    lines += ["def uncalled():"]
    lines += ["    value = 1"] * (total_body_lines - covered_body_lines - 1) + ["    return 1"]
    (tmp_path / "synthetic_subject.py").write_text("\n".join(lines) + "\n")
    (tmp_path / "test_subject.py").write_text(
        "from synthetic_subject import covered\n"
        "def test_observed_result():\n"
        "    assert covered() == 1\n"
    )
    result = subprocess.run(  # noqa: S603 - synthetic subject, fixed test program and argv.
        [
            sys.executable,
            "-m",
            "pytest",
            "test_subject.py",
            "--cov=synthetic_subject",
            "--cov-report=term",
            "--cov-fail-under=90",
            "--cov-precision=2",
            "-q",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == expected_rc, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert ("Coverage failure" in result.stdout) is (expected_rc != 0)
