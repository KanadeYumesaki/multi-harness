#!/usr/bin/env python3
"""design-source/registries/ から Domain層の列挙コードを生成する。

不変条件#18「件数を本文・コードへ手入力しない」を機構で強制するための生成器である。
State名、Event Type、Error Code、Core Schemaの正本はRegistry YAMLだけであり、
`src/harness/domain/_registry_generated.py` は本スクリプトの出力に過ぎない。

生成物のヘッダには各Registry YAMLのSHA-256を埋め込む。`--check` は再生成結果と
チェックイン済みファイルをBytes比較し、乖離を非0で報告する。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml

# `schemas.yaml` の正規化は4 Tool共有の `tools/schema_catalog.py` が正本である
# （Step 3-b でTool内の複製を削除した）。Script実行時は sys.path[0] が tools/ に
# なるが、Testが importlib で読み込む経路では解決されないため明示的に足す。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from schema_catalog import (
    active_write_versions,
    logical_schema_count,
    normalize_core_schemas,
)

# 共有Moduleの関数をそのまま公開する。再実装が入り込んでいないことを
# `tests/spec_lint/test_schema_version_tools.py` が同一性で検査する。
__all__ = ["logical_schema_count", "main", "normalize_core_schemas", "render"]

REGISTRY_FILES = ("states.yaml", "events.yaml", "errors.yaml", "schemas.yaml")

DEFAULT_REGISTRIES = Path("design-source/registries")
DEFAULT_OUT = Path("src/harness/domain/_registry_generated.py")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load(registries: Path, name: str) -> tuple[Any, str]:
    raw = (registries / name).read_bytes()
    return yaml.safe_load(raw.decode("utf-8")), f"sha256:{_sha256_hex(raw)}"


def _py_str(value: str) -> str:
    """Python source literal。

    `ruff format` の既定に合わせて二重引用符で出力する。生成物が整形器と食い違うと
    `ruff format --check` と `--check`（生成物の鮮度検査）が同時に満たせなくなる。
    Registry値はASCII識別子だが、`json.dumps` で確実にEscapeする。
    """
    return json.dumps(value, ensure_ascii=False)


def render(registries: Path) -> str:
    states_doc, states_hash = _load(registries, "states.yaml")
    events_doc, events_hash = _load(registries, "events.yaml")
    errors_doc, errors_hash = _load(registries, "errors.yaml")
    schemas_doc, schemas_hash = _load(registries, "schemas.yaml")

    state_namespaces: dict[str, list[str]] = states_doc["state_namespaces"]
    event_types: list[str] = events_doc["event_types"]
    error_rows: list[dict[str, str]] = errors_doc["error_codes"]
    schema_rows: list[dict[str, Any]] = schemas_doc["core_schemas"]

    # Registry順序を正本とする。列挙順に意味があるためsortしない。
    _assert_unique("state namespace", list(state_namespaces))
    _assert_unique("event type", event_types)
    _assert_unique("error code", [r["error_code"] for r in error_rows])
    # 同一 schema_name の異なるVersionは許可し、(name, version) の重複だけを拒否する
    # （Owner Decision D-06）。以前は schema_name 単独一意だったため、2.0.0 を
    # schemas.yaml へ登録した時点で生成が落ちていた。
    schema_entries = normalize_core_schemas(schema_rows)
    _assert_unique(
        "core schema version",
        [f"{e['schema_name']}@{e['schema_version']}" for e in schema_entries],
    )
    # 「どの版へ書くのか」をDomain層が知らないと、複数Version登録後の書込み先が
    # YAMLの中だけの取り決めになる。論理Schemaごとにちょうど1版であることを
    # ここでFail-Closedに確かめてから生成する。
    active_versions = active_write_versions(schema_entries)

    lines: list[str] = []
    add = lines.append

    add('"""design-source/registries/ からの生成物。直接編集しない。')
    add("")
    add("再生成: python tools/generate_domain_registry_code.py")
    add("整合検査: python tools/generate_domain_registry_code.py --check")
    add("")
    add("source registries:")
    add(f"  states.yaml  {states_hash}")
    add(f"  events.yaml  {events_hash}")
    add(f"  errors.yaml  {errors_hash}")
    add(f"  schemas.yaml {schemas_hash}")
    add('"""')
    add("")
    add("from __future__ import annotations")
    add("")
    add("from collections.abc import Mapping")
    add("from enum import Enum")
    add("from types import MappingProxyType")
    add("from typing import Final")
    add("")
    add("REGISTRY_SOURCE_HASHES: Final[Mapping[str, str]] = MappingProxyType(")
    add("    {")
    for name, registry_hash in (
        ("states.yaml", states_hash),
        ("events.yaml", events_hash),
        ("errors.yaml", errors_hash),
        ("schemas.yaml", schemas_hash),
    ):
        add(f"        {_py_str(name)}: {_py_str(registry_hash)},")
    add("    }")
    add(")")
    add("")
    add("")
    # --- EventType ---
    add("class EventType(Enum):")
    add('    """§1.8 共通監査イベント。正本は events.yaml。"""')
    add("")
    for name in event_types:
        add(f"    {name} = {_py_str(name)}")
    add("")
    add("")
    # --- ErrorClassification ---
    classifications: list[str] = []
    for row in error_rows:
        cls = row["classification"]
        if cls not in classifications:
            classifications.append(cls)
    add("class ErrorClassification(Enum):")
    add('    """§1.7 共通エラー分類。正本は errors.yaml の classification 列。"""')
    add("")
    for name in classifications:
        add(f"    {name} = {_py_str(name)}")
    add("")
    add("")
    # --- ErrorCode ---
    add("class ErrorCode(Enum):")
    add('    """§1.7.1 Error Code Registry。正本は errors.yaml。"""')
    add("")
    for row in error_rows:
        add(f"    {row['error_code']} = {_py_str(row['error_code'])}")
    add("")
    add("")
    add("ERROR_CLASSIFICATION: Final[Mapping[ErrorCode, ErrorClassification]] = MappingProxyType(")
    add("    {")
    for row in error_rows:
        add(f"        ErrorCode.{row['error_code']}: ErrorClassification.{row['classification']},")
    add("    }")
    add(")")
    add("")
    add("")
    # --- StateNamespace ---
    add("class StateNamespace(Enum):")
    add('    """§19.1 State名空間。正本は states.yaml。"""')
    add("")
    for namespace in state_namespaces:
        add(f"    {namespace} = {_py_str(namespace)}")
    add("")
    add("")
    add("STATE_NAMESPACES: Final[Mapping[StateNamespace, tuple[str, ...]]] = MappingProxyType(")
    add("    {")
    for namespace, members in state_namespaces.items():
        _assert_unique(f"state in {namespace}", members)
        add(f"        StateNamespace.{namespace}: (")
        for member in members:
            add(f"            {_py_str(member)},")
        add("        ),")
    add("    }")
    add(")")
    add("")
    add("")
    # --- Core schemas ---
    # 1行 = 1 Version。同一 schema_name が複数行に現れうる（§15.2 複数Version）。
    add("CORE_SCHEMAS: Final[tuple[tuple[str, str, str], ...]] = (")
    for entry in schema_entries:
        add(
            f"    ({_py_str(entry['schema_name'])}, "
            f"{_py_str(entry['schema_version'])}, {_py_str(entry['path'])}),"
        )
    add(")")
    add("")
    add("")
    add("# 論理Schema名 → 書込み可能Version。読み取り専用の旧版はここに現れない。")
    add("CORE_SCHEMA_ACTIVE_WRITE_VERSIONS: Final[Mapping[str, str]] = MappingProxyType(")
    add("    {")
    for name in sorted(active_versions):
        add(f"        {_py_str(name)}: {_py_str(active_versions[name])},")
    add("    }")
    add(")")

    # 末尾は改行1個で終える。`ruff format` は余分な空行を落とすため、
    # ここで空行を足すと生成器と整形器が同時に満たせなくなる。
    return "\n".join(lines) + "\n"


def _assert_unique(label: str, values: list[str]) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {label}: {value}")
        seen.add(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registries", type=Path, default=DEFAULT_REGISTRIES)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="生成せず、チェックイン済みファイルとの一致だけを検査する",
    )
    args = parser.parse_args(argv)

    rendered = render(args.registries)

    if args.check:
        if not args.out.exists():
            print(f"generated file missing: {args.out}", file=sys.stderr)
            return 1
        current = args.out.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"generated file is stale: {args.out}\n"
                "run: python tools/generate_domain_registry_code.py",
                file=sys.stderr,
            )
            return 1
        print(f"{args.out}: up to date")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered, encoding="utf-8")
    print(f"generated {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
