"""変更範囲から実行層を決める判定の試験。

## ここが Fail-Closed の要である

判定を間違えると、**安全に関わる変更が軽い検査だけで通る**。
だから「通ること」より「分からないときに完全検査へ回ること」を測る。

* 分類できない Path が 1 つでもあれば完全検査
* 差分を読めなければ完全検査
* 変更が 0 件でも完全検査（影響なしと決めつけない）
* `ci:full` ラベル・`workflow_dispatch`・main push・Release tag・Release 候補は完全検査
* 正本が動いたら Draft でも spec と reference を回す
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "tools/ci_scope.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("_ci_scope", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scope = _module()


def decide(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "changed": ["docs/note.md"],
        "event": "pull_request",
        "is_draft": True,
        "labels": [],
        "ref": "refs/pull/1/merge",
        "head_ref": "feat/x",
        "diff_ok": True,
    }
    base.update(overrides)
    return scope.decide(**base)


# ---------------------------------------------------------------------------
# Fail-Closed
# ---------------------------------------------------------------------------


def test_unknown_path_forces_the_full_tier() -> None:
    """分類できない Path があれば完全検査へ回すこと。

    **「たぶん関係ない」で省かない。** 新しい置き場所が増えたとき、判定表を
    更新するまでは重い側へ倒す。
    """
    result = decide(changed=["docs/note.md", "some/new/place.txt"])
    assert result["tier"] == "full"
    assert any("分類できない" in reason for reason in result["reasons"])
    assert result["run_spec"] and result["run_supply"] and result["run_deep"]


def test_unreadable_diff_forces_the_full_tier() -> None:
    """差分を読めなければ完全検査へ回すこと。

    読めていないことを「影響なし」と書かない。
    """
    result = decide(diff_ok=False, changed=[])
    assert result["tier"] == "full"
    assert any("読めなかった" in reason for reason in result["reasons"])


def test_empty_change_set_forces_the_full_tier() -> None:
    """変更が 0 件でも完全検査へ回すこと。"""
    result = decide(changed=[])
    assert result["tier"] == "full"


# ---------------------------------------------------------------------------
# 完全検査へ上げる条件
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("workflow_dispatch", {"event": "workflow_dispatch"}),
        ("main push", {"event": "push", "ref": "refs/heads/main"}),
        ("Release tag", {"event": "push", "ref": "refs/tags/v1.0.0"}),
        ("ci:full ラベル", {"labels": ["ci:full"]}),
        ("Release 候補 Branch", {"head_ref": "release/1.0"}),
    ],
)
def test_full_tier_triggers(label: str, overrides: dict[str, Any]) -> None:
    result = decide(**overrides)
    assert result["tier"] == "full", label
    assert result["pythons"] == scope.FULL_PYTHONS, label
    assert result["run_full_tests"] and result["run_supply"] and result["run_deep"], label


# ---------------------------------------------------------------------------
# 3 層の中身
# ---------------------------------------------------------------------------


def test_draft_runs_the_minimum() -> None:
    """Draft は 1 版・最小検査であること。"""
    result = decide(changed=["src/harness/domain/x.py"], is_draft=True)
    assert result["tier"] == "draft"
    assert result["pythons"] == scope.LIGHT_PYTHONS
    assert result["run_integration"] is False
    assert result["run_deep"] is False
    assert result["run_full_tests"] is False


def test_ready_adds_spec_and_integration() -> None:
    """Ready for Review では spec・reference・Integration まで回すこと。"""
    result = decide(changed=["src/harness/domain/x.py"], is_draft=False)
    assert result["tier"] == "ready"
    assert result["run_spec"] and result["run_reference"] and result["run_integration"]
    assert result["run_full_tests"] is True
    assert result["run_deep"] is False


def test_canon_change_runs_spec_even_on_draft() -> None:
    """正本が動いたら Draft でも spec と reference を回すこと。

    **正本の壊れを Ready まで持ち越さない。**
    """
    result = decide(changed=["design-source/registries/errors.yaml"], is_draft=True)
    assert result["tier"] == "draft"
    assert result["canon_paths"] is True
    assert result["run_spec"] is True
    assert result["run_reference"] is True


def test_dependency_change_runs_license_and_sbom() -> None:
    """依存が動いたら license／SBOM を回すこと。"""
    result = decide(changed=["requirements-dev.txt"], is_draft=True)
    assert result["dependency_paths"] is True
    assert result["run_supply"] is True


def test_docs_only_draft_stays_minimal() -> None:
    """文書だけの Draft は最小のままであること。"""
    result = decide(changed=["docs/CI-POLICY.md", "README.md"], is_draft=True)
    assert result["tier"] == "draft"
    assert result["run_spec"] is False
    assert result["run_supply"] is False


def test_schedule_runs_only_the_deep_suites() -> None:
    """schedule は deep だけであること。"""
    result = decide(changed=["docs/note.md"], event="schedule", is_draft=False)
    assert result["tier"] == "deep"
    assert result["run_deep"] is True
    assert result["run_spec"] is False
    assert result["run_supply"] is False


# ---------------------------------------------------------------------------
# 分類そのもの
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/harness/domain/x.py", "safety"),
        ("tests/unit/x.py", "safety"),
        ("tools/x.py", "safety"),
        (".github/workflows/ci.yml", "safety"),
        ("design-source/registries/errors.yaml", "canon"),
        ("schemas/core/Run/1.0.0.schema.json", "canon"),
        ("spec/10-mvp0a.md", "canon"),
        ("registry-snapshot.json", "canon"),
        ("design-v1.25-runtime-go.md", "canon"),
        ("requirements-dev.txt", "dependency"),
        ("pyproject.toml", "dependency"),
        ("docs/x.md", "docs"),
        ("runtime-evidence/x.md", "docs"),
        ("README.md", "docs"),
        ("some/unmapped/thing.txt", "unknown"),
    ],
)
def test_classify(path: str, expected: str) -> None:
    assert scope.classify(path) == expected


def test_safety_prefixes_cover_the_task_list() -> None:
    """Task が名指しした安全関連 Path が全部入っていること。"""
    required = ("src/", "schemas/", "spec/", "design-source/", "tools/", "tests/", ".github/")
    covered = set(scope.SAFETY_PREFIXES) | set(scope.CANON_PREFIXES)
    missing = [prefix for prefix in required if prefix not in covered]
    assert not missing, f"安全関連 Path が漏れている: {missing}"
    for prefix in required:
        assert scope.classify(f"{prefix}x") in {"safety", "canon"}, prefix
