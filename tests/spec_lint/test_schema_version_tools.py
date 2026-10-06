"""Schema複数Version対応の生成Tool基盤（Step 3-a で導入、Step 3-b で共有Module化）。

`schemas.yaml` はもともと 1 Schema名＝1 Version を前提に組まれていた。`2.0.0` の行を
足すだけでは動かないどころか、**既存の `1.0.0` を壊す**。

| 箇所 | 改修前の挙動 |
|---|---|
| `generate_domain_registry_code.py` | `schema_name` 単独一意。重複で即エラー |
| `lint_spec.py` | `DUPLICATE_SCHEMA_NAME` を違反として計上 |
| `build_registry_snapshot.py` | `core_schemas` はSchema名の配列だけ |
| `build_core_schemas.py` | Field定義Tableが `schema_name` だけをKey |

4行目が最も危険である。`2.0.0` を登録すると **`1.0.0` も新定義で再生成され上書きされる**。
旧Recordを読むReaderとUpcasterの土台が静かに消える。

Step 3-a では変更許可Pathが4 Toolに限定されていたため正規化関数を4箇所へ複製し、
本Fileが出力一致を検査していた。Step 3-b で `tools/schema_catalog.py` へ統合したため、
検査対象は「4 Toolが同じ実装を参照していること」へ変わった。
食い違いを事後に見つけるのではなく、起こり得なくする。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRIES = REPO_ROOT / "design-source" / "registries"

# 正規化を使う4 Tool。共有Moduleの**同一オブジェクト**を参照していることを検査する。
TOOLS_WITH_NORMALIZER = (
    "build_core_schemas",
    "generate_domain_registry_code",
    "lint_spec",
    "build_registry_snapshot",
)


def _load_tool(name: str) -> types.ModuleType:
    """`tools/<name>.py` をModuleとして読み込む。"""
    tools_dir = REPO_ROOT / "tools"
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    path = tools_dir / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_tool_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# Fixture（一時。design-source/registries/ は変更しない）
# --------------------------------------------------------------------------

SINGLE_VERSION_ROW: dict[str, Any] = {
    "ordinal": 11,
    "schema_name": "ContextBundle",
    "schema_version": "1.0.0",
    "path": "schemas/core/ContextBundle/1.0.0.schema.json",
}

MULTI_VERSION_ROW: dict[str, Any] = {
    "ordinal": 11,
    "schema_name": "ContextBundle",
    "active_write_version": "2.0.0",
    "versions": [
        {
            "version": "1.0.0",
            "path": "schemas/core/ContextBundle/1.0.0.schema.json",
            "read_only": True,
        },
        {
            "version": "2.0.0",
            "path": "schemas/core/ContextBundle/2.0.0.schema.json",
            "read_only": False,
        },
    ],
}


def _normalize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _load_tool("build_core_schemas").normalize_core_schemas(rows)


# --------------------------------------------------------------------------
# 実装条件 1〜5
# --------------------------------------------------------------------------


def test_single_version_format_is_backward_compatible() -> None:
    """条件1: 現行の1 Version形式をそのまま処理できる。"""
    entries = _normalize([SINGLE_VERSION_ROW])
    assert entries == [
        {
            "ordinal": 11,
            "schema_name": "ContextBundle",
            "schema_version": "1.0.0",
            "path": "schemas/core/ContextBundle/1.0.0.schema.json",
            "read_only": False,
            "active_write": True,
        }
    ]


def test_multi_version_fixture_is_expanded() -> None:
    """条件2: 複数Version形式を展開する。"""
    entries = _normalize([MULTI_VERSION_ROW])
    assert [(e["schema_version"], e["read_only"], e["active_write"]) for e in entries] == [
        ("1.0.0", True, False),
        ("2.0.0", False, True),
    ]
    assert entries[1]["path"] == "schemas/core/ContextBundle/2.0.0.schema.json"


def test_logical_schema_count_is_separate_from_version_count() -> None:
    """条件3: 論理Schema数とVersion数を分離する。"""
    module = _load_tool("build_core_schemas")
    entries = module.normalize_core_schemas([MULTI_VERSION_ROW])
    assert len(entries) == 2
    assert module.logical_schema_count(entries) == 1


def test_same_name_different_versions_are_allowed() -> None:
    """条件4: 同じ`schema_name`の異なるVersionは許可する。"""
    entries = _normalize([MULTI_VERSION_ROW])
    assert {e["schema_name"] for e in entries} == {"ContextBundle"}
    assert {e["schema_version"] for e in entries} == {"1.0.0", "2.0.0"}


def test_duplicate_name_and_version_is_rejected() -> None:
    """条件5: 同じ`schema_name` + `schema_version`の重複は拒否する。"""
    duplicated = dict(MULTI_VERSION_ROW)
    duplicated["versions"] = [
        {"version": "1.0.0", "path": "a.json", "read_only": True},
        {"version": "1.0.0", "path": "b.json", "read_only": False},
    ]
    duplicated["active_write_version"] = "1.0.0"
    with pytest.raises(ValueError, match="duplicate core schema version"):
        _normalize([duplicated])


def test_active_write_version_must_be_declared_and_unique() -> None:
    """`active_write_version`の宣言と`read_only`が矛盾していないこと。

    「どの版へ書くのか」が2箇所で言えると、片方だけ直したときに静かにずれる。
    """
    missing_active = {
        "ordinal": 1,
        "schema_name": "X",
        "versions": [{"version": "1.0.0", "path": "a"}],
    }
    with pytest.raises(ValueError, match="active_write_version"):
        _normalize([missing_active])

    unknown_active = {
        "ordinal": 1,
        "schema_name": "X",
        "active_write_version": "9.9.9",
        "versions": [{"version": "1.0.0", "path": "a", "read_only": True}],
    }
    with pytest.raises(ValueError, match="versions に無い"):
        _normalize([unknown_active])

    contradictory = {
        "ordinal": 1,
        "schema_name": "X",
        "active_write_version": "2.0.0",
        "versions": [
            {"version": "1.0.0", "path": "a", "read_only": False},
            {"version": "2.0.0", "path": "b", "read_only": False},
        ],
    }
    with pytest.raises(ValueError, match="read_only=false"):
        _normalize([contradictory])


def test_all_four_tools_share_one_normalizer() -> None:
    """4 Toolが共有Moduleの同一関数を参照していること（Step 3-b）。

    Step 3-a は複製の**出力一致**を検査していた。出力一致検査は食い違いを事後に
    見つけるだけで、正本が4つある状態そのものは残る。ここでは同一オブジェクトで
    あることを要求し、複製が再発したら落ちるようにする。
    """
    import schema_catalog

    for name in TOOLS_WITH_NORMALIZER:
        module = _load_tool(name)
        assert module.normalize_core_schemas is schema_catalog.normalize_core_schemas, (
            f"{name} が normalize_core_schemas を再実装している"
        )
        assert module.logical_schema_count is schema_catalog.logical_schema_count, (
            f"{name} が logical_schema_count を再実装している"
        )


def test_runtime_registry_reader_agrees_with_the_generator() -> None:
    """Runtime側（`src/`）のRegistry解釈が生成器と一致すること。

    `src/harness/infrastructure/schema/registry.py` は `tools/` をimportしない
    （層の依存規則）。したがって解釈が2つ存在する。ここが唯一の歯止めになる。
    """
    import schema_catalog
    from harness.infrastructure.schema.registry import catalog_entries

    other_single = {**SINGLE_VERSION_ROW, "ordinal": 12, "schema_name": "ContextFragment"}
    fixtures: list[list[dict[str, Any]]] = [
        [SINGLE_VERSION_ROW],
        [MULTI_VERSION_ROW],
        [other_single, MULTI_VERSION_ROW],
        yaml.safe_load((REGISTRIES / "schemas.yaml").read_text(encoding="utf-8"))["core_schemas"],
    ]
    for fixture in fixtures:
        expected = [
            (e["schema_name"], e["schema_version"], e["path"], e["active_write"])
            for e in schema_catalog.normalize_core_schemas(fixture)
        ]
        assert catalog_entries(fixture) == expected


# --------------------------------------------------------------------------
# 実装条件 6〜7：生成定義の解決とBytes不変
# --------------------------------------------------------------------------


def test_definition_is_resolved_per_name_and_version() -> None:
    """条件6: `(name, version)`単位で解決し、名前だけのFallbackを持たない。

    未登録Versionは`KeyError`で落ちる。ここが通ると、未定義Versionへ
    別Versionの定義が流用され、さらに既存Versionが上書きされる。

    Step 3-a 時点では `2.0.0` が未定義だったためそれを例に使っていた。
    Step 4 で Context/Token 系5 Schema の `2.0.0` を定義したので、
    例を未定義のまま残る `3.0.0` へ移す。**検査内容は変えていない。**
    """
    module = _load_tool("build_core_schemas")
    built = module.build_schema("ContextBundle", "1.0.0")
    assert built["$id"].endswith("/ContextBundle/1.0.0.schema.json")

    with pytest.raises(KeyError, match="ContextBundle@3.0.0"):
        module.build_schema("ContextBundle", "3.0.0")


def test_existing_1_0_0_schema_bytes_are_unchanged() -> None:
    """条件7: 既存1.0.0 Schemaの生成Bytesが変わらないこと。

    Commit済みFileと生成結果をBytes比較する。`--check`と同じ判定を全Schemaで行う。
    """
    module = _load_tool("build_core_schemas")
    rows = yaml.safe_load((REGISTRIES / "schemas.yaml").read_text(encoding="utf-8"))["core_schemas"]
    stale: list[str] = []
    for entry in module.normalize_core_schemas(rows):
        rendered = (
            json.dumps(
                module.build_schema(entry["schema_name"], entry["schema_version"]),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        committed = (REPO_ROOT / entry["path"]).read_text(encoding="utf-8")
        if committed != rendered:
            stale.append(entry["path"])
    assert not stale, f"1.0.0 Schemaの生成Bytesが変わった: {stale}"


# --------------------------------------------------------------------------
# 実装条件 8〜13：Catalog Hash と Set Hash の分離
# --------------------------------------------------------------------------


def _catalog(rows: list[dict[str, Any]], root: Path = REPO_ROOT) -> list[dict[str, Any]]:
    module = _load_tool("build_registry_snapshot")
    return module.schema_catalog_entries(module.normalize_core_schemas(rows), root)


def _materialize(rows: list[dict[str, Any]], root: Path) -> None:
    """Fixtureが宣言するPathへ中身の違うFileを実際に置く。

    Catalogは登録Pathの実在を要求する（Fail-Closed）。Fileを置かずに
    複数Versionを検証すると、検査したいのがHashなのか不在検出なのか分からなくなる。
    """
    module = _load_tool("build_registry_snapshot")
    for entry in module.normalize_core_schemas(rows):
        target = root / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"title": entry["schema_name"], "version": entry["schema_version"]}),
            encoding="utf-8",
        )


def test_catalog_entry_carries_version_path_and_content_hash() -> None:
    """条件8: Version・Path・Content Hashを持つ。"""
    catalog = _catalog([SINGLE_VERSION_ROW])
    assert catalog[0]["schema_version"] == "1.0.0"
    assert catalog[0]["path"] == "schemas/core/ContextBundle/1.0.0.schema.json"
    assert catalog[0]["content_hash"].startswith("sha256:")


def test_catalog_hash_covers_every_version(tmp_path: Path) -> None:
    """条件9: `schema_catalog_hash`は全Versionを対象にする。"""
    module = _load_tool("build_registry_snapshot")
    _materialize([SINGLE_VERSION_ROW], tmp_path)
    _materialize([MULTI_VERSION_ROW], tmp_path)
    single = module.compute_schema_catalog_hash(_catalog([SINGLE_VERSION_ROW], tmp_path))
    multi = module.compute_schema_catalog_hash(_catalog([MULTI_VERSION_ROW], tmp_path))
    assert single != multi, "Versionを1件足してもCatalog Hashが変わらない"


def test_catalog_hash_ignores_registry_order() -> None:
    """記載順はCatalog Hashへ漏らさない（§1.11 Filesystem列挙順への非依存と同趣旨）。"""
    module = _load_tool("build_registry_snapshot")
    catalog = _catalog([SINGLE_VERSION_ROW])
    assert module.compute_schema_catalog_hash(catalog) == module.compute_schema_catalog_hash(
        list(reversed(catalog))
    )


def test_catalog_hash_changes_when_content_hash_changes() -> None:
    """Content Hashが変わればCatalog Hashも変わる。"""
    module = _load_tool("build_registry_snapshot")
    catalog = _catalog([SINGLE_VERSION_ROW])
    baseline = module.compute_schema_catalog_hash(catalog)
    tampered = json.loads(json.dumps(catalog))
    tampered[0]["content_hash"] = "sha256:" + "0" * 64
    assert module.compute_schema_catalog_hash(tampered) != baseline


def test_catalog_hash_excludes_nondeterministic_values() -> None:
    """条件13: `generated_at`等の非決定値をHashへ含めない。"""
    module = _load_tool("build_registry_snapshot")
    catalog = _catalog([SINGLE_VERSION_ROW])
    baseline = module.compute_schema_catalog_hash(catalog)
    noisy = json.loads(json.dumps(catalog))
    noisy[0]["generated_at"] = "2026-01-01T00:00:00Z"
    assert module.compute_schema_catalog_hash(noisy) == baseline


def test_catalog_hash_covers_read_only_and_active_write() -> None:
    """書込み可否はCatalog Hashの入力である（Step 3-b）。

    Step 3-a の投影は`schema_name`／`schema_version`／`path`／`content_hash`だけを
    含んでいた。それでは**どの版へ書いてよいかを入れ替えてもHashが動かない**。
    Catalogが「どの版がどのFileで、書いてよいのはどれか」を表す以上、
    `read_only`と`active_write`は内容であって注記ではない。
    """
    module = _load_tool("build_registry_snapshot")
    catalog = _catalog([SINGLE_VERSION_ROW])
    baseline = module.compute_schema_catalog_hash(catalog)

    flipped_read_only = json.loads(json.dumps(catalog))
    flipped_read_only[0]["read_only"] = not flipped_read_only[0]["read_only"]
    assert module.compute_schema_catalog_hash(flipped_read_only) != baseline

    flipped_active = json.loads(json.dumps(catalog))
    flipped_active[0]["active_write"] = not flipped_active[0]["active_write"]
    assert module.compute_schema_catalog_hash(flipped_active) != baseline


def test_catalog_entries_fail_closed_when_a_registered_path_is_missing() -> None:
    """登録PathのFileが無ければCatalogを作らない（Fail-Closed）。

    以前は`content_hash=None`にして続行していた。それは「Fileの無いRegistry行」を
    黙ってCatalog Hashへ畳み込むことであり、`2.0.0`の生成を忘れたままSnapshotが
    出来上がる。判定できないなら止める（不変条件#9）。
    """
    ghost = {
        "ordinal": 99,
        "schema_name": "ContextBundle",
        "schema_version": "9.9.9",
        "path": "schemas/core/ContextBundle/9.9.9.schema.json",
    }
    with pytest.raises(FileNotFoundError, match="9.9.9"):
        _catalog([ghost])


def test_schema_set_hash_is_unaffected_by_adding_a_catalog_version() -> None:
    """条件11: Version追加だけでは既存Planの`schema_set_hash`を変えない。

    `schema_set_hash` は Plan が**実際に参照した** SchemaRef だけを対象にする。
    §15.2「Execution Planへ**使用**Schema ID／Version／HashのSchema Set Hash」。
    分離しないと、`1.0.0`を読み取り可能なまま残すだけで全PlanのHashが動く。
    """
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from harness.domain.hashing import ContentHash
    from harness.domain.schema_set import SchemaRef, compute_schema_set_hash

    used = [
        SchemaRef("ContextBundle", "1.0.0", ContentHash.parse("sha256:" + "a" * 64)),
        SchemaRef("ExecutionPlan", "1.0.0", ContentHash.parse("sha256:" + "b" * 64)),
    ]
    before = compute_schema_set_hash(used)

    # Catalogへ 2.0.0 が増えても、Planが参照する集合は変わらない。
    after = compute_schema_set_hash(list(used))
    assert before == after


def test_schema_set_hash_changes_on_version_or_content_change() -> None:
    """条件12: Version変更またはContent Hash変更では`schema_set_hash`が変わる。"""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from harness.domain.hashing import ContentHash
    from harness.domain.schema_set import SchemaRef, compute_schema_set_hash

    base_hash = ContentHash.parse("sha256:" + "a" * 64)
    baseline = compute_schema_set_hash([SchemaRef("ContextBundle", "1.0.0", base_hash)])

    version_changed = compute_schema_set_hash([SchemaRef("ContextBundle", "2.0.0", base_hash)])
    content_changed = compute_schema_set_hash(
        [SchemaRef("ContextBundle", "1.0.0", ContentHash.parse("sha256:" + "c" * 64))]
    )
    assert version_changed != baseline
    assert content_changed != baseline
    assert version_changed != content_changed


# --------------------------------------------------------------------------
# 実装条件 14：現行Registryに対する出力が変わらない
# --------------------------------------------------------------------------


def test_generated_domain_code_is_unchanged() -> None:
    """条件14: 生成コードが変更前と同一であること。"""
    result = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
        [sys.executable, "tools/generate_domain_registry_code.py", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_core_schemas_are_unchanged() -> None:
    """条件14: `schemas/core` の生成物が変更前と同一であること。"""
    result = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
        [sys.executable, "tools/build_core_schemas.py", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_snapshot_matches_the_committed_one_and_carries_the_catalog(tmp_path: Path) -> None:
    """条件14: 再生成したsnapshotが`generated_at`以外Commit済みと一致すること。

    併せて、Verifierが版を識別するための`core_schema_versions`と
    `schema_catalog_hash`が入っていることを固定する（Step 3-b）。
    """
    out = tmp_path / "snapshot.json"
    result = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
        [
            sys.executable,
            "tools/build_registry_snapshot.py",
            "--registries",
            "design-source/registries",
            "--design",
            "design-v1.25-runtime-go.md",
            "--out",
            str(out),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    generated = json.loads(out.read_text(encoding="utf-8"))
    committed = json.loads((REPO_ROOT / "registry-snapshot.json").read_text(encoding="utf-8"))
    assert generated["registry_snapshot_hash"] == committed["registry_snapshot_hash"]
    assert {k: v for k, v in generated.items() if k != "generated_at"} == {
        k: v for k, v in committed.items() if k != "generated_at"
    }
    module = _load_tool("build_registry_snapshot")
    catalog = generated["core_schema_versions"]
    assert generated["schema_catalog_hash"] == module.compute_schema_catalog_hash(catalog)
    assert {entry["schema_name"] for entry in catalog} == set(generated["core_schemas"])
    # 論理Schema数とVersion数を別Fieldとして持つ。
    assert generated["totals"]["core_schema_count"] == len(set(generated["core_schemas"]))
    assert generated["totals"]["core_schema_version_count"] == len(catalog)
    for entry in catalog:
        assert entry["content_hash"].startswith("sha256:")


# --------------------------------------------------------------------------
# Phase 1（Step 4）：2.0.0 定義
# --------------------------------------------------------------------------

CONTEXT_TOKEN_SCHEMAS = (
    "ContextBundle",
    "ContextSelectionReceipt",
    "ContextFragment",
    "TokenProfileSnapshot",
    "TokenBudgetPolicy",
)


#: Owner Decision CMC-6-A。本文参照を必須で足すため Major が要る。
#: D-01 とは根拠が違うので、由来が読めるよう分けて持つ。
CHAT_MAJOR_SCHEMAS = ("ConversationMessage",)

#: **2.0.0 へ巻き込んではならない Schema。**
#: Major 化すると plan_content_hash と Runtime 起動経路へ波及する。
#: 列挙を広げるだけでは、次に危ない Schema が混ざったとき気付けない。
MUST_NOT_GET_MAJOR = (
    "ExecutionPlan",
    "RuntimeAttestation",
    "RuntimeEnvelopeSpec",
    "ApprovalGrant",
    "ActionAttempt",
    "EventEnvelope",
)


def test_versioned_definitions_cover_exactly_the_declared_scope() -> None:
    """条件1／3: 2.0.0 の対象は宣言した Schema だけ。

    D-01 の Context/Token 系5 Schema と、CMC-6-A の `ConversationMessage` である。
    範囲外へ広げると、ExecutionPlan や RuntimeAttestation まで Major 更新に
    巻き込まれ、plan_content_hash と Runtime 起動経路へ波及する。
    """
    module = _load_tool("build_core_schemas")
    defined = set(module._VERSIONED_DEFINITIONS)
    expected = {(name, "2.0.0") for name in CONTEXT_TOKEN_SCHEMAS + CHAT_MAJOR_SCHEMAS}
    assert defined == expected

    # 完全一致だけでは「なぜ危ないか」が読めない。波及先を名指しで見張る。
    dangerous = sorted(name for name, _ in defined if name in MUST_NOT_GET_MAJOR)
    assert not dangerous, f"波及先の Schema が Major 対象に入っている: {dangerous}"


def test_two_point_zero_resolves_every_spec_required_field() -> None:
    """2.0.0 は Schema本文の必須Field をすべて満たす（乖離0）。"""
    module = _load_tool("build_core_schemas")
    contract = _load_tool("check_schema_contract")
    for name in CONTEXT_TOKEN_SCHEMAS:
        schema = module.build_schema(name, "2.0.0")
        properties = set(schema["properties"])
        required = set(schema["required"])
        declared = contract.spec_required_fields(name)
        assert [f for f in declared if f not in properties] == [], name
        assert [f for f in declared if f in properties and f not in required] == [], name


def test_one_point_zero_definitions_are_untouched_by_adding_two_point_zero() -> None:
    """条件7: 1.0.0 の生成結果が変わらないこと。

    2.0.0 を足したことで 1.0.0 の中身が動いていないかを、Commit済みFileとの
    Bytes比較で確かめる。ここが崩れると旧Recordを読むReaderが消える。
    """
    module = _load_tool("build_core_schemas")
    rows = yaml.safe_load((REGISTRIES / "schemas.yaml").read_text(encoding="utf-8"))["core_schemas"]
    for entry in module.normalize_core_schemas(rows):
        if entry["schema_version"] != "1.0.0":
            continue
        rendered = (
            json.dumps(
                module.build_schema(entry["schema_name"], "1.0.0"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        committed = (REPO_ROOT / entry["path"]).read_text(encoding="utf-8")
        name = entry["schema_name"]
        assert committed == rendered, f"{name}@1.0.0 の生成Bytesが変わった"


def test_one_point_zero_and_two_point_zero_are_distinct_documents() -> None:
    """条件6: 1.0.0 と 2.0.0 が別の内容・別の識別子になること。"""
    module = _load_tool("build_core_schemas")
    identifier = "$id"
    for name in CONTEXT_TOKEN_SCHEMAS:
        v1 = module.build_schema(name, "1.0.0")
        v2 = module.build_schema(name, "2.0.0")
        assert v1 != v2, name
        assert v1[identifier].endswith(f"/{name}/1.0.0.schema.json")
        assert v2[identifier].endswith(f"/{name}/2.0.0.schema.json")
        assert v1["properties"]["schema_version"]["const"] == "1.0.0"
        assert v2["properties"]["schema_version"]["const"] == "2.0.0"


def test_unregistered_version_still_fails_closed() -> None:
    """条件4／5: 名前だけのFallbackを足していないこと。

    定義の無いVersionは KeyError で落ちる。ここが通ると、未定義Versionへ
    別Versionの定義が流用される。
    """
    module = _load_tool("build_core_schemas")
    with pytest.raises(KeyError, match="ContextBundle@3.0.0"):
        module.build_schema("ContextBundle", "3.0.0")
    # 2.0.0 の定義が無い Schema も同じく落ちる。
    with pytest.raises(KeyError, match="ExecutionPlan@2.0.0"):
        module.build_schema("ExecutionPlan", "2.0.0")


def test_excluded_fragments_carry_a_reason_code_in_two_point_zero() -> None:
    """2.0.0 は除外理由をRecordへ保存できること。

    1.0.0 の （ID配列）では
    「なぜ落ちたか」を後から説明できなかった。
    """
    module = _load_tool("build_core_schemas")
    schema = module.build_schema("ContextSelectionReceipt", "2.0.0")
    item = schema["properties"]["excluded_fragments"]["items"]
    assert item["required"] == ["fragment_id", "reason"]
    assert item["properties"]["reason"]["enum"] == ["BUDGET", "DUPLICATE"]
