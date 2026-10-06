"""CI の消費を抑える設定が入っていることの検査。

## なぜ試験にするか

Actions の月枠を使い切った。抑制の設定は **消しても静かに壊れる** ——
Workflow が動き続けるので、次に枠が尽きるまで誰も気付かない。

## 何を測っているか

* 2 つの Workflow（実物と正本）が同じ抑制設定を持つ
* PR 単位の `concurrency` があり、古い Run を止める
* main と Tag では止めない（Release の検証を消さない）
* push が main と Tag だけに絞られ、Feature Branch の二重実行が消えている
* `ready_for_review` が types にある（Draft から戻したとき完全検査へ戻る）
* 全 Job に `timeout-minutes` がある
* `setup-python` が依存 Cache を使う
* **安全検査の工程が 1 つも消えていない**
* check 名が変わっていない

## 通ることではなく通らないことを測る

`concurrency` を消した瞬間、`timeout-minutes` を落とした瞬間、安全検査の工程を
1 つ削った瞬間に落ちる。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = (
    REPO_ROOT / ".github/workflows/ci.yml",
    REPO_ROOT / "ci/github-actions-ci.yml",
)

#: Job 名。これが check 名になる。**変えると required check が外れる。**
EXPECTED_JOB_NAMES = {
    # 範囲判定の Job。**足しただけで、既存の check 名は 1 つも消していない。**
    "plan": "scope-plan",
    "release-tag": "release-tag-signature",
    "spec": "spec-foundation",
    "quality": "quality (py${{ matrix.python }})",
    "supply-chain": "license-and-sbom",
    "nightly": "nightly-deep",
}

#: 消してはいけない検査の工程。Tool 名で見る。名前が消えたら工程が消えている。
#: **実物**（.github/workflows/ci.yml）はこれを全部持っていなければならない。
SAFETY_STEPS = (
    "lint_spec.py",
    "check_design_generation_contract.py",
    "build_spec_shards.py",
    "generate_domain_registry_code.py",
    "build_core_schemas.py",
    "check_blocked_records.py",
    "validate_test_manifest.py",
    "check_case_coverage.py",
    "check_schema_contract.py",
    "check_forbidden_patterns.py",
    "verify_runtime_go",
    "pip-audit",
    "cyclonedx-py",
    "mypy",
    "ruff",
    "pytest",
)


def _document(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(document: dict) -> dict:
    # PyYAML は素の `on:` を真偽値 True として読む。
    return document[True] if True in document else document["on"]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_is_valid_yaml(path: Path) -> None:
    document = _document(path)
    assert isinstance(document, dict), path.name
    assert document["name"] == "ci"
    assert set(document["jobs"]) == set(EXPECTED_JOB_NAMES), path.name


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_check_names_are_unchanged(path: Path) -> None:
    """Job 名＝check 名を変えていないこと。

    名前が変わると、その名前で required にしている設定が外れる。
    """
    jobs = _document(path)["jobs"]
    for key, expected in EXPECTED_JOB_NAMES.items():
        assert jobs[key]["name"] == expected, f"{path.name}: {key} の check 名が変わった"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_concurrency_cancels_superseded_runs(path: Path) -> None:
    """PR 単位で古い Run を止めること。main と Tag は止めないこと。"""
    concurrency = _document(path)["concurrency"]
    group = concurrency["group"]
    assert "github.event.pull_request.number" in group, path.name
    assert "github.ref" in group, path.name
    cancel = str(concurrency["cancel-in-progress"])
    assert "refs/heads/main" in cancel, f"{path.name}: main を止めない条件が無い"
    assert "refs/tags/" in cancel, f"{path.name}: Tag を止めない条件が無い"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_push_does_not_duplicate_pull_request(path: Path) -> None:
    """push が main と Tag だけであること。

    Feature Branch の push は、同じ Commit へ pull_request が走るので二重になる。
    """
    triggers = _triggers(_document(path))
    push = triggers["push"]
    assert push["branches"] == ["main"], f"{path.name}: push が main 以外にも開いている"
    assert push["tags"], f"{path.name}: Tag の push が閉じている"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_ready_for_review_restores_full_checks(path: Path) -> None:
    """Draft から Ready へ戻したとき、完全検査が走ること。

    既定の types に `ready_for_review` は入っていない。足さないと、Draft の間の
    軽い検査だけで Ready になってしまう。
    """
    types = _triggers(_document(path))["pull_request"]["types"]
    assert "ready_for_review" in types, path.name
    for required in ("opened", "synchronize", "reopened"):
        assert required in types, f"{path.name}: {required} が外れている"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_python_versions_come_from_the_scope_plan(path: Path) -> None:
    """何版で回すかを範囲判定 Job が決めること。

    判定を YAML の式へ書くと試験できない。`tools/ci_scope.py` が決め、Job は
    その出力を読むだけにする。**判定そのものの試験は別 File が持つ。**
    """
    document = _document(path)
    matrix = document["jobs"]["quality"]["strategy"]["matrix"]
    expression = str(matrix["python"])
    assert "needs.plan.outputs.pythons" in expression, path.name
    assert "fromJSON" in expression, path.name
    assert document["jobs"]["quality"]["needs"] == ["plan"], path.name


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_job_has_a_timeout(path: Path) -> None:
    """上限が無いと、ハングした Job が最大まで課金され続ける。"""
    for name, job in _document(path)["jobs"].items():
        timeout = job.get("timeout-minutes")
        assert isinstance(timeout, int), f"{path.name}: {name} に timeout-minutes が無い"
        assert 0 < timeout <= 60, f"{path.name}: {name} の timeout が {timeout} 分"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_setup_python_uses_the_dependency_cache(path: Path) -> None:
    """依存の取得を毎回やらないこと。"""
    document = _document(path)
    found = 0
    for job in document["jobs"].values():
        for step in job["steps"]:
            if str(step.get("uses", "")).startswith("actions/setup-python"):
                found += 1
                with_block = step.get("with", {})
                assert with_block.get("cache") == "pip", f"{path.name}: Cache が無い"
                assert with_block.get("cache-dependency-path"), f"{path.name}: 対象が無い"
    assert found >= 5, f"{path.name}: setup-python が {found} 件しか無い"


#: 正本に元から無い工程。**この Task が消したのではない。**
#: 実測: HEAD の時点で `ci/github-actions-ci.yml` に無かった 3 件である。
#: 正本と実物の食い違いであり、直すなら CI 定義そのものを変える別 Task になる。
#: ここでは **食い違いを固定する**。広がれば落ちる。
CANONICAL_DIVERGENCE = frozenset(
    {
        "build_spec_shards.py",
        "generate_domain_registry_code.py",
        "build_core_schemas.py",
    }
)


def test_the_running_workflow_keeps_every_safety_step() -> None:
    """**実際に走る Workflow から安全検査が 1 つも消えていないこと。**

    実行回数を減らすのと、検査を消すのは別である。ここが両者を分ける。
    """
    text = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    missing = [step for step in SAFETY_STEPS if step not in text]
    assert not missing, f"安全検査の工程が消えている: {missing}"


def test_the_canonical_divergence_does_not_grow() -> None:
    """正本に無い工程が、記録した 3 件から増えていないこと。

    正本と実物の食い違いは前からある。**黙って広げない。**
    """
    text = (REPO_ROOT / "ci/github-actions-ci.yml").read_text(encoding="utf-8")
    missing = {step for step in SAFETY_STEPS if step not in text}
    assert missing == CANONICAL_DIVERGENCE, (
        f"正本と実物の食い違いが変わった: 増={sorted(missing - CANONICAL_DIVERGENCE)} "
        f"減={sorted(CANONICAL_DIVERGENCE - missing)}"
    )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_deep_suites_are_gated_not_skipped(path: Path) -> None:
    """deep が範囲判定で絞られること。**Job ごと skip しないこと。**

    job 単位の `if` で落とすと check が消える。Job は走らせて、対象外なら
    not-applicable で成功終了する。
    """
    document = _document(path)
    nightly = document["jobs"]["nightly"]
    assert "if" not in nightly, f"{path.name}: nightly が job 単位で skip される"
    assert nightly["needs"] == ["plan"], path.name
    first = nightly["steps"][0]
    assert first["id"] == "scope", f"{path.name}: 先頭が範囲判定でない"
    assert first["env"]["CI_INPUT_0"] == "${{ needs.plan.outputs.run_deep }}", path.name
    assert '"${CI_INPUT_0}"' in first["run"], path.name
    assert "not applicable" in first["run"], f"{path.name}: not-applicable を名乗らない"
    assert "workflow_dispatch" in _triggers(document), f"{path.name}: 手動起動できない"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_job_is_skipped_at_the_job_level(path: Path) -> None:
    """check 名を持つ Job を job 単位で skip しないこと。

    前の形は schedule のとき `if` で落としていた。**skip された Job は check として
    現れない。** required にしている名前が消える。Job は走らせて、対象外なら
    not-applicable で成功終了する。

    Tag 専用の `release-tag` だけは例外である。Tag が無いときに走らせても
    「Tag が無い」以外の結果を出さない。
    """
    jobs = _document(path)["jobs"]
    for name in ("plan", "spec", "quality", "supply-chain", "nightly"):
        assert "if" not in jobs[name], f"{path.name}: {name} が job 単位で skip される"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_out_of_scope_jobs_end_as_not_applicable(path: Path) -> None:
    """対象外の Job が、短い not-applicable の成功で終わること。

    先頭の判定 step だけが無条件で走り、残りは全部その結果で絞られる。
    **1 つでも `if` が漏れると、対象外でも重い工程が走る。**
    """
    gated = {"spec": "run_spec", "supply-chain": "run_supply", "nightly": "run_deep"}
    jobs = _document(path)["jobs"]
    for name, output in gated.items():
        steps = jobs[name]["steps"]
        first = steps[0]
        assert first.get("id") == "scope", f"{path.name}: {name} の先頭が判定でない"
        assert first["env"]["CI_INPUT_0"] == "${{ needs.plan.outputs." + output + " }}"
        assert '"${CI_INPUT_0}"' in first["run"], f"{path.name}: {name}"
        assert "if" not in first, f"{path.name}: {name} の判定 step に if が付いている"
        unguarded = [step.get("name") or step.get("uses") for step in steps[1:] if "if" not in step]
        assert not unguarded, f"{path.name}: {name} に条件の無い step がある: {unguarded}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_scope_plan_decides_with_the_tested_tool(path: Path) -> None:
    """範囲判定が `tools/ci_scope.py` に置かれていること。

    YAML の中で判定すると試験できない。判定は Python 側に置き、Job は読むだけにする。
    """
    plan = _document(path)["jobs"]["plan"]
    assert plan["name"] == "scope-plan", path.name
    body = json.dumps(plan, ensure_ascii=False)
    assert "tools/ci_scope.py" in body, path.name
    assert "fetch-depth" in body, f"{path.name}: 差分を取るのに履歴が要る"
    for output in (
        "tier",
        "pythons",
        "run_spec",
        "run_reference",
        "run_supply",
        "run_integration",
        "run_deep",
        "run_full_tests",
    ):
        assert output in plan["outputs"], f"{path.name}: 出力 {output} が無い"


def test_both_workflows_agree() -> None:
    """実物と正本が同じ抑制設定を持つこと。

    片方だけ直すと、正本と実物が食い違ったまま気付かない。
    """
    documents = [_document(path) for path in WORKFLOWS]
    triggers = [_triggers(document) for document in documents]
    assert triggers[0] == triggers[1], "trigger が食い違っている"
    assert documents[0]["concurrency"] == documents[1]["concurrency"], "concurrency が違う"
    for key in EXPECTED_JOB_NAMES:
        left, right = documents[0]["jobs"][key], documents[1]["jobs"][key]
        assert left["name"] == right["name"], key
        assert left["timeout-minutes"] == right["timeout-minutes"], key
        assert left.get("if") == right.get("if"), key
    left_matrix = json.dumps(documents[0]["jobs"]["quality"]["strategy"]["matrix"])
    right_matrix = json.dumps(documents[1]["jobs"]["quality"]["strategy"]["matrix"])
    assert left_matrix == right_matrix, "quality の matrix が食い違っている"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
@pytest.mark.parametrize("job", ["spec", "supply-chain", "nightly"])
@pytest.mark.parametrize("enabled", ["true", "false"])
def test_scope_gate_executes_both_branches(
    path: Path, job: str, enabled: str, tmp_path: Path
) -> None:
    """環境変数へ移した後も対象内/外の実行判定が反転・固定されないこと。"""
    step = _document(path)["jobs"][job]["steps"][0]
    output = tmp_path / "github-output"
    result = subprocess.run(  # noqa: S603 - repository workflow, inputs supplied via env
        ["/bin/bash", "-eu", "-c", step["run"]],
        env=dict(os.environ, CI_INPUT_0=enabled, CI_INPUT_1="unit", GITHUB_OUTPUT=str(output)),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    expected = "false" if enabled == "true" else "true"
    assert output.read_text() == f"skip={expected}\n"
    assert ("not applicable" in result.stdout) == (enabled == "false")
