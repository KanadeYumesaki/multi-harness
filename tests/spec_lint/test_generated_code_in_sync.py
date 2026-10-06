"""生成物が正本Registryと同期していることの検査。

不変条件#18「件数を本文・コードへ手入力しない」は、生成器を用意するだけでは守れない。
Registryを編集して再生成を忘れた状態を検出できて初めて機構になる。
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATED = REPO_ROOT / "src" / "harness" / "domain" / "_registry_generated.py"


def test_generated_registry_code_is_up_to_date() -> None:
    """Registry編集後に再生成を忘れた状態を検出する。"""
    result = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
        [
            sys.executable,
            "tools/generate_domain_registry_code.py",
            "--check",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"生成物がRegistryと乖離している。\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_generated_file_is_ruff_format_clean() -> None:
    """生成物が `ruff format` の出力と一致する。

    CIは `ruff format --check .` と生成物の鮮度検査を両方要求する。生成器の出力が
    整形器と1文字でも食い違うと、この2つを同時に満たせないデッドロックになる。
    """
    result = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
        [sys.executable, "-m", "ruff", "format", "--check", str(GENERATED)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "生成物が ruff format の出力と一致しない。生成器側の出力を整形器へ合わせること。\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_generation_is_deterministic() -> None:
    """同一Registryから2回生成して同一Bytesになる（生成器自身の決定性）。"""
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    try:
        from generate_domain_registry_code import render  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    registries = REPO_ROOT / "design-source" / "registries"
    assert render(registries) == render(registries)


def _registry(name: str) -> object:
    import yaml

    path = REPO_ROOT / "design-source" / "registries" / name
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _all_state_names() -> set[str]:
    names: set[str] = set()
    for members in _registry("states.yaml")["state_namespaces"].values():  # type: ignore[index]
        names.update(members)
    return names


def test_domain_modules_do_not_hardcode_event_or_error_names() -> None:
    """domain/ の非生成FileにEvent名・Error Codeの文字列Literalを書かない。

    Event と Error Code には生成済みEnum（`EventType` / `ErrorCode`）があるため、
    文字列Literalで書く理由がない。`EventType.ACTION_COMMITTED` と書くべき箇所を
    `"ACTION_COMMITTED"` と書くと、Registryから消えても気付けない。

    **State名は例外とする。** `states.yaml` のState名にはEnumが無く
    （`STATE_NAMESPACES` は名前空間→文字列tupleの写像）、文字列Literalが唯一の
    参照手段である。`EFFECT_UNKNOWN` や `CANCEL_UNKNOWN` のようにState名と
    Event名／Error Codeが同名のものがあるため、State名として登録済みの値は除外する。
    除外した分は下の `test_state_literals_in_domain_are_registered` が受け持つ。
    """
    forbidden: set[str] = set()
    forbidden.update(_registry("events.yaml")["event_types"])  # type: ignore[index]
    forbidden.update(
        row["error_code"]
        for row in _registry("errors.yaml")["error_codes"]  # type: ignore[index]
    )
    forbidden -= _all_state_names()

    violations: list[str] = []
    domain = REPO_ROOT / "src" / "harness" / "domain"
    for path in sorted(domain.rglob("*.py")):
        if path == GENERATED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in forbidden:
                    violations.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno} "
                        f"hardcodes {node.value!r}; use the generated Enum"
                    )
    assert not violations, "Event名／Error Codeの手入力:\n" + "\n".join(violations)


def test_state_literals_in_domain_are_registered() -> None:
    """domain/ が使うState名リテラルが全てstates.yamlに存在する。

    上の検査でState名を除外した分をこちらで挟み、実在しないState名を書けなくする。
    Enumが無いState名を取りこぼさないための対。
    """
    state_names = _all_state_names()
    transitions = REPO_ROOT / "src" / "harness" / "domain" / "transitions.py"
    tree = ast.parse(transitions.read_text(encoding="utf-8"), filename=str(transitions))

    # `__all__` の要素はexport名であってState名ではない。
    exported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            for element in ast.walk(node.value):
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    exported.add(element.value)

    candidates = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.isupper()
        and node.value.replace("_", "").isalnum()
        and len(node.value) > 3
    }
    # §1.7の分類名はError Code未登録のため生成Enumに現れない（transitions.py参照）。
    # PolicyOutcome の値も State ではない。
    non_state = {"RATE_LIMITED", "TRANSIENT_PROVIDER_ERROR", "TIMEOUT", "ALLOW", "DENY"}
    unknown = sorted(candidates - state_names - non_state - exported)
    assert not unknown, f"states.yamlに存在しないState名リテラル: {unknown}"


def test_no_numeric_count_literals_in_domain_docstrings() -> None:
    """Registry件数を本文へ手入力していないことを確認する。

    件数(63/52/25/20/86/70/36)がdomain層のSource中に裸で現れた場合、
    Registry変更時に静かに乖離する。`len(...)`から導出すること。
    """
    forbidden_counts = {"63", "52", "25", "20", "86", "70", "36", "37", "29"}
    violations: list[str] = []
    domain = REPO_ROOT / "src" / "harness" / "domain"
    for path in sorted(domain.rglob("*.py")):
        if path == GENERATED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, int):
                if str(node.value) in forbidden_counts:
                    violations.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno} literal {node.value}"
                    )
    assert not violations, "件数の手入力:\n" + "\n".join(violations)
