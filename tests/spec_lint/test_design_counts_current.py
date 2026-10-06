"""設計書に旧件数が現行仕様として残っていないことの検査。

`lint_spec.py` はTest Manifest表とState表をRegistryと突合するが、**散文中の件数**は
検査しない。

## 文字列ブラックリストをやめた理由

初版は「実際に残っていた文字列」を列挙して不在を確認する方式だった。
この方式は**列挙した言い回しだけ**を守る。実際、v1.8適用後のレビューで

    | MVP0-A | 70 | 29 | 36 |     （Release Scope表。現行は 110 / 31 / 45）
    20 Core Schema                （現行は 22）

の2種類が、列挙に無い言い回しだったため素通りしていた。生成specにも同じ値が
展開されており、新規スレッドが古い表を見て40 Caseを実装対象から落としかねない状態だった。

そこで **Registryから導出した値と設計書の記述を突き合わせる構造的検査** へ変えた。
表記が変わっても、件数そのものを比較するため取りこぼさない。

歴史記述（「v1.6では86 Case全PASSを要求していた」等）は残してよい。
禁止するのは**現行仕様としての件数の主張**である。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRIES = REPO_ROOT / "design-source" / "registries"
SPEC_DIR = REPO_ROOT / "spec"


def _design() -> Path:
    """ZIP展開時の文字コード差異を避け、機械処理はASCII固定名を使用する。"""
    path = REPO_ROOT / "design-v1.25-runtime-go.md"
    assert path.is_file(), f"ASCII設計書が存在しない: {path}"
    return path


@pytest.fixture(scope="module")
def design_text() -> str:
    return _design().read_text(encoding="utf-8")


def _registry(name: str) -> Any:
    return yaml.safe_load((REGISTRIES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def derived() -> dict[str, dict[str, int]]:
    """Registryから Release Scope ごとの必要数を導出する。**唯一の正本。**"""
    cases = _registry("tests.yaml")["test_cases"]
    gates = _registry("gates.yaml")["gates"]
    result: dict[str, dict[str, int]] = {}
    phases = {phase for case in cases for phase in case["phase_scope"]}
    for phase in phases:
        scope = [case for case in cases if phase in case["phase_scope"]]
        result[phase] = {
            "case": len(scope),
            "test_id": len({case["test_id"] for case in scope}),
            "gate": len([gate for gate in gates if gate["phase"] == phase]),
        }
    return result


# ---------------------------------------------------------------------------
# Release Scope 表の構造的照合
# ---------------------------------------------------------------------------

# 例: | MVP0-A | 110 | 31 | 45 |   /   | MVP0-B | 68 | 22 | 別途定義 |
_SCOPE_ROW = re.compile(
    r"^\|\s*(MVP[0-9A-Za-z-]+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+|別途定義)\s*\|\s*$",
    re.MULTILINE,
)


def test_release_scope_table_is_present(design_text: str) -> None:
    """表そのものが消えていないこと。0件でも成功する検査にしない。"""
    assert _SCOPE_ROW.findall(design_text), "Release Scope表が設計書に見当たらない"


# 設計書のRelease Scope表が扱うべきScope。Registryには MVP1-E や ENTERPRISE も
# 現れるが、本表は「Release判定単位」を並べたものであり全Phaseの一覧ではない。
_EXPECTED_SCOPE_ROWS = frozenset({"MVP0-A", "MVP0-B", "MVP1-A", "MVP0-C", "MVP1-D"})


def test_release_scope_table_covers_the_expected_scopes(design_text: str) -> None:
    """行を**消せば通る**検査にしない。

    値の照合だけだと、都合の悪い行を削除して合格させられる。
    期待するScope集合と一致すること、重複が無いことを併せて確認する。
    """
    found = [phase for phase, _c, _i, _g in _SCOPE_ROW.findall(design_text)]
    assert len(found) == len(set(found)), f"Release Scope表に重複行がある: {found}"
    assert set(found) == _EXPECTED_SCOPE_ROWS, (
        f"Release Scope表のScope集合が期待と違う\n"
        f"  設計書: {sorted(set(found))}\n"
        f"  期待  : {sorted(_EXPECTED_SCOPE_ROWS)}"
    )


def test_release_scope_table_matches_registry(
    design_text: str, derived: dict[str, dict[str, int]]
) -> None:
    """表の各行がRegistry導出値と一致する。"""
    mismatches: list[str] = []
    for phase, case_text, id_text, gate_text in _SCOPE_ROW.findall(design_text):
        expected = derived.get(phase)
        if expected is None:
            mismatches.append(f"{phase}: Registryに存在しないScope")
            continue
        if int(case_text) != expected["case"]:
            mismatches.append(f"{phase} 必要Case: 設計書={case_text} Registry={expected['case']}")
        if int(id_text) != expected["test_id"]:
            mismatches.append(
                f"{phase} 必要Test ID: 設計書={id_text} Registry={expected['test_id']}"
            )
        if gate_text != "別途定義" and int(gate_text) != expected["gate"]:
            mismatches.append(f"{phase} 必要Gate: 設計書={gate_text} Registry={expected['gate']}")
    assert not mismatches, "Release Scope表がRegistryと不一致（不変条件#18）:\n" + "\n".join(
        mismatches
    )


def test_scope_counts_block_matches_registry(
    design_text: str, derived: dict[str, dict[str, int]]
) -> None:
    """§3.13のScope規模ブロックがRegistry導出値と一致する。"""
    expected = derived["MVP0-A"]
    for label, value in (
        ("必要Case", expected["case"]),
        ("必要Test ID", expected["test_id"]),
        ("必要Gate", expected["gate"]),
    ):
        assert f"{label}      : {value}" in design_text or f"{label}   : {value}" in design_text, (
            f"§3.13のScope規模ブロックが {label}={value} と一致しない"
        )


# ---------------------------------------------------------------------------
# Schema件数の主張
# ---------------------------------------------------------------------------

# 「20 Core Schema」「20 Core Domain Schema」のような**Core Schema総数**の主張だけを対象にする。
#
# `(?<![.\d])` は節番号を除くために要る。これが無いと
# `## 15.9 Core Schema Catalog v1` の "9" を件数主張として拾う。
#
# 「11 Schemaの最小垂直スライス」は総数の主張ではなく、22件のうち
# 11件を選ぶというScope宣言である（§16 Step 4に対象名が列挙されている）。
# `Core` を必須にすることでこれを対象外にする。
_SCHEMA_COUNT_CLAIM = re.compile(
    r"(?<![.\d])(\d+)\s*(?:Core(?:\s+Domain)?\s+Schema|JSON\s+Schema(?:実ファイル|種)?)"
)
_VALID_FIXTURE_COUNT = re.compile(r"valid_fixture_count\s*==\s*(\d+)")


def _assert_fixture_counts_match_registry(body: str, current: int, source: str) -> list[str]:
    return [
        f"{source}: valid_fixture_count == {found} != {current}"
        for found in _VALID_FIXTURE_COUNT.findall(body)
        if int(found) != current
    ]


def test_schema_count_claims_match_registry(design_text: str) -> None:
    """設計書中の「N Core (Domain) Schema」がRegistry件数と一致する。

    件数を書かない（「Core Schema全件」）のが望ましいが、書くなら合っていること。
    """
    current = len(_registry("schemas.yaml")["core_schemas"])
    wrong = [
        int(found) for found in _SCHEMA_COUNT_CLAIM.findall(design_text) if int(found) != current
    ]
    fixture_problems = _assert_fixture_counts_match_registry(
        design_text, current, "design-v1.25-runtime-go.md"
    )
    assert not wrong and not fixture_problems, (
        f"Registry件数({current})と異なるSchema件数の主張が残っている: {sorted(set(wrong))}\n"
        + "\n".join(fixture_problems)
        + "\n件数を書かず『Core Schema全件』とするか、Registry導出値へ揃えること（不変条件#18）。"
    )


def test_schema_registry_catalog_files_gate_and_test_manifest_agree() -> None:
    """Registry、実Schema、Catalog、Gate、Test Manifestの22件主張を同時に検査する。"""
    from schema_catalog import normalize_core_schemas

    schemas = normalize_core_schemas(_registry("schemas.yaml")["core_schemas"])
    names = {entry["schema_name"] for entry in schemas}
    paths = {entry["path"] for entry in schemas}
    actual_schema_paths = {
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / "schemas" / "core").glob("*/*.schema.json")
    }
    catalog_names = {path.stem for path in (SPEC_DIR / "20-schemas").glob("*.md")}
    tests = _registry("tests.yaml")["test_cases"]
    gates = _registry("gates.yaml")["gates"]
    # 「22 Core Schema」は論理Schema数の主張である。Version数と混ぜない（§15.9）。
    current = len(names)

    assert len(schemas) == len(paths), "Schema Registryに重複または欠落がある"
    assert paths == actual_schema_paths, "schemas/coreの実ファイルがRegistryと不一致"
    assert names == catalog_names, "spec/20-schemasのCatalogがRegistryと不一致"
    assert any(
        f"valid_fixture_count == {current}" in assertion
        for case in tests
        if case["test_id"] == "AT-SCHEMA-COMPLETE-001" and case["case_id"] == "ALL_VALID"
        for assertion in case["assertions"]
    ), "AT-SCHEMA-COMPLETE-001/ALL_VALIDがRegistry件数を主張していない"
    assert any(
        f"{current} Core Schema" in gate["condition"]
        for gate in gates
        if gate["gate_id"] == "MVP0A-GATE-30"
    ), "MVP0A-GATE-30がRegistry件数を主張していない"


# ---------------------------------------------------------------------------
# 生成物への波及
# ---------------------------------------------------------------------------


def test_generated_spec_shards_carry_no_stale_counts(
    derived: dict[str, dict[str, int]],
) -> None:
    """specは設計書からの生成物。設計書が直っていても再生成漏れがあれば残る。

    新規スレッドは `spec/` を読むことがあるため、ここが古いと実装対象を取り違える。
    """
    current_schema = len(_registry("schemas.yaml")["core_schemas"])
    problems: list[str] = []
    for shard in sorted(SPEC_DIR.rglob("*.md")):
        body = shard.read_text(encoding="utf-8")
        for phase, case_text, id_text, gate_text in _SCOPE_ROW.findall(body):
            expected = derived.get(phase)
            if expected is None:
                continue
            if int(case_text) != expected["case"] or int(id_text) != expected["test_id"]:
                problems.append(
                    f"{shard.relative_to(REPO_ROOT)}: {phase} "
                    f"{case_text}/{id_text}/{gate_text} != "
                    f"{expected['case']}/{expected['test_id']}/{expected['gate']}"
                )
        for found in _SCHEMA_COUNT_CLAIM.findall(body):
            if int(found) != current_schema:
                problems.append(
                    f"{shard.relative_to(REPO_ROOT)}: {found} Schema != {current_schema}"
                )
        problems.extend(
            _assert_fixture_counts_match_registry(
                body, current_schema, str(shard.relative_to(REPO_ROOT))
            )
        )
    assert not problems, "生成specに旧件数が残っている:\n" + "\n".join(problems)


# ---------------------------------------------------------------------------
# 歴史記述は残す
# ---------------------------------------------------------------------------


def test_historical_v16_description_is_preserved(design_text: str) -> None:
    """歴史記述まで消してはならない。v1.6の問題説明は残す。

    「なぜRelease Scope単位判定にしたか」の根拠が本文から失われると、
    新規スレッドが同じ設計判断を再現できない。
    """
    assert "v1.6では" in design_text
    assert "86 Case" in design_text, "v1.6の86 Case問題の説明が失われている"
