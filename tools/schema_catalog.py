#!/usr/bin/env python3
"""Core Schema Catalog の共有正規化Module（Owner Decision D-06 / Step 3-b）。

`design-source/registries/schemas.yaml` を読む4 Tool
（`build_core_schemas` / `generate_domain_registry_code` / `lint_spec` /
`build_registry_snapshot`）が**同じ1実装**を使うためのModuleである。

Step 3-a では変更許可Pathが4 Toolに限定されていたため同じ関数を4箇所へ複製し、
`tests/spec_lint/test_schema_version_tools.py` が出力一致を機械検査していた。
複製は「4つの正本」を作る経路であり、検査は食い違いを**事後に**見つけるだけである。
Step 3-b で共有Moduleへ統合し、食い違いが**起こり得ない**形にした。

## 提供するもの

| 関数 | 役割 |
|---|---|
| `normalize_core_schemas` | 1 Version形式／複数Version形式を `(name, version)` 単位へ展開 |
| `logical_schema_count` | 論理Schema数（Version数とは別に数える） |
| `schema_catalog_entries` | Version・Path・Content Hashを持つCatalog行 |
| `compute_schema_catalog_hash` | 登録済み**全Version**を対象にしたHash |

`schema_catalog_hash` と `schema_set_hash` は**別物**である。混同しない。

| Hash | 対象 | 変わるとき |
|---|---|---|
| `schema_catalog_hash` | 登録済み全Version | Registryに版が増減したとき |
| `schema_set_hash` | Planが**実際に使用した**Version集合 | そのPlanが使う版が変わったとき |

前者の実装は本Module、後者は `src/harness/domain/schema_set.py` である。

分離しないと、`1.0.0` を読み取り可能なまま保持するだけで**全PlanのHashが動く**。
§15.2 は「Execution Planへ**使用**Schema ID／Version／HashのSchema Set Hashを含める」と
定めており、Planへ束縛するのは使用集合の側である。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

CATALOG_HASH_PREFIX = "FDE-HARNESS/schema-catalog/1/"

# `schema_catalog_hash` の入力に含めるKey。Registryの記載順・生成時刻・Filesystem
# 列挙順といった非決定値は含めない（§1.11）。
CATALOG_HASH_FIELDS = (
    "schema_name",
    "schema_version",
    "path",
    "content_hash",
    "read_only",
    "active_write",
)


def canonical(obj: Any) -> bytes:
    """§1.11 Canonical JSON。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def domain_hash(prefix: str, obj: Any) -> str:
    return "sha256:" + hashlib.sha256(prefix.encode("utf-8") + canonical(obj)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def normalize_core_schemas(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`core_schemas` を `(schema_name, schema_version)` 単位へ展開する。

    受け付ける形式は2つ。

    1 Version形式（Step 3-b 以前の `schemas.yaml`。Fixture互換のため残す）::

        - ordinal: 11
          schema_name: ContextBundle
          schema_version: 1.0.0
          path: schemas/core/ContextBundle/1.0.0.schema.json

    複数Version形式（Step 3-b 以降の `schemas.yaml`）::

        - ordinal: 11
          schema_name: ContextBundle
          active_write_version: 2.0.0
          versions:
            - version: 1.0.0
              path: schemas/core/ContextBundle/1.0.0.schema.json
              read_only: true
            - version: 2.0.0
              path: schemas/core/ContextBundle/2.0.0.schema.json
              read_only: false

    戻り値の各要素は
    `{ordinal, schema_name, schema_version, path, read_only, active_write}`。
    並びは `(ordinal, schema_version)` 昇順で固定する。

    同一 `schema_name` の異なるVersionは許可し、
    同一 `(schema_name, schema_version)` の重複は拒否する。
    """
    entries: list[dict[str, Any]] = []
    for row in rows:
        name = row["schema_name"]
        ordinal = int(row["ordinal"])
        versions = row.get("versions")
        if not versions:
            entries.append(
                {
                    "ordinal": ordinal,
                    "schema_name": name,
                    "schema_version": row["schema_version"],
                    "path": row["path"],
                    "read_only": False,
                    "active_write": True,
                }
            )
            continue

        active = row.get("active_write_version")
        if not active:
            raise ValueError(f"{name}: versions を持つ行は active_write_version が必須")
        declared = [entry["version"] for entry in versions]
        if active not in declared:
            raise ValueError(f"{name}: active_write_version {active} が versions に無い")
        writable = [entry["version"] for entry in versions if not entry.get("read_only", False)]
        if writable != [active]:
            raise ValueError(
                f"{name}: read_only=false は active_write_version だけに付ける "
                f"(active={active}, writable={writable})"
            )
        for entry in versions:
            entries.append(
                {
                    "ordinal": ordinal,
                    "schema_name": name,
                    "schema_version": entry["version"],
                    "path": entry["path"],
                    "read_only": bool(entry.get("read_only", False)),
                    "active_write": entry["version"] == active,
                }
            )

    seen: set[tuple[str, str]] = set()
    for entry in entries:
        key = (entry["schema_name"], entry["schema_version"])
        if key in seen:
            raise ValueError(f"duplicate core schema version: {key[0]}@{key[1]}")
        seen.add(key)

    entries.sort(key=lambda item: (item["ordinal"], item["schema_version"]))
    return entries


def logical_schema_count(entries: list[dict[str, Any]]) -> int:
    """論理Schema数。Version数とは別に数える（Owner Decision D-06 条件3）。"""
    return len({entry["schema_name"] for entry in entries})


def active_write_versions(entries: list[dict[str, Any]]) -> dict[str, str]:
    """Schema名 → 書込み可能Version。

    各論理Schemaはちょうど1つのactive write版を持つ。`normalize_core_schemas` が
    複数Version形式について既に検査しているが、1 Version形式と混在した場合にも
    成立することをここで確かめられるようにする。
    """
    active: dict[str, str] = {}
    for entry in entries:
        if not entry["active_write"]:
            continue
        name = entry["schema_name"]
        if name in active:
            raise ValueError(
                f"{name}: active write version が複数ある "
                f"({active[name]}, {entry['schema_version']})"
            )
        active[name] = entry["schema_version"]
    missing = sorted({entry["schema_name"] for entry in entries} - set(active))
    if missing:
        raise ValueError(f"active write version が無い core schema: {', '.join(missing)}")
    return active


def schema_catalog_entries(entries: list[dict[str, Any]], root: Path) -> list[dict[str, Any]]:
    """Catalogの1件分。Version・Path・Content Hashを持つ（Owner Decision D-06 条件4）。

    `core_schemas`（名前の配列）だけではVerifierが版を識別できない。
    どの版がどのFileで、その中身が何であるかを snapshot 側へ落とす。

    **登録Pathが存在しない場合はFail-Closedで停止する。** 以前は `content_hash` を
    `None` にして続行していたが、それは「Fileが無いRegistry行」を
    Catalog Hashへ黙って畳み込むことであり、`2.0.0` を登録し忘れたまま
    Snapshotが生成できてしまう（不変条件#9、§6「判定できないなら止める」）。
    """
    catalog: list[dict[str, Any]] = []
    missing: list[str] = []
    for entry in entries:
        target = root / entry["path"]
        if not target.is_file():
            missing.append(f"{entry['schema_name']}@{entry['schema_version']} -> {entry['path']}")
            continue
        catalog.append(
            {
                "schema_name": entry["schema_name"],
                "schema_version": entry["schema_version"],
                "path": entry["path"],
                "content_hash": sha256_file(target),
                "read_only": entry["read_only"],
                "active_write": entry["active_write"],
            }
        )
    if missing:
        raise FileNotFoundError(
            "schemas.yaml が指すSchema Fileが存在しない（Fail-Closed）:\n  " + "\n  ".join(missing)
        )
    return catalog


def compute_schema_catalog_hash(catalog: list[dict[str, Any]]) -> str:
    """`schema_catalog_hash`。**登録済み全Version**を対象にする。

    * Hash対象は `schema_name` / `schema_version` / `path` / `content_hash` /
      `read_only` / `active_write`。どの版へ書いてよいかが変われば値が変わる。
    * `generated_at` などの非決定値は入力に含めない（§1.11）。
    * 整列は `(schema_name, schema_version)` 昇順で固定し、Registryの記載順を
      持ち込まない。
    """
    projection = sorted(
        ({field: item[field] for field in CATALOG_HASH_FIELDS} for item in catalog),
        key=lambda item: (item["schema_name"], item["schema_version"]),
    )
    return domain_hash(CATALOG_HASH_PREFIX, projection)
