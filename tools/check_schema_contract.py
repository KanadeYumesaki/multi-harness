#!/usr/bin/env python3
"""Schema本文とJSON Schemaの契約乖離を検出する（Owner Decision D-04）。

## なぜ必要か

Core Schemaには**正本が2つ**ある。

  * `spec/20-schemas/<Name>.md` … 設計書から生成される「必須Field」の宣言
  * `schemas/core/<Name>/<ver>.schema.json` … `tools/build_core_schemas.py` の
    手書きTableから生成される機械可読Schema

**この2つを突き合わせる検査が存在しなかった。** そのため全22 Schemaのうち
20 Schemaで、設計書が必須と定めるFieldがJSON Schemaに無い状態が続き、
Spec Lintは一度も落ちなかった。乖離が何件あっても緑のままだった。

`design-source/registries/errors.yaml` と設計書§1.7.1の関係も同じ構造である。
「正本が2つあるのに照合しない」を塞ぐのが本Toolの役目である。

## 「報告」ではなく「検査」

差分を印字して常に0を返すのは報告であって検査ではない。増加を検出できない。
そこでBaseline方式にする。

| 状態 | 終了Code |
|---|---:|
| Baselineと一致（既知の乖離のみ） | 0 `REPORT_ONLY / known divergence` |
| 新しい乖離が増えた | **1** `NEW_DIVERGENCE` |
| `resolved`宣言済みSchemaに乖離が残っている | **1** `RESOLVED_SCHEMA_REGRESSED` |
| Baselineの乖離が解消した（是正済み） | **1** `BASELINE_STALE` |

**終了Code 0 は「乖離が無い」ではなく「既知の乖離から増えていない」を意味する。**
`tests/case-coverage.yaml` の `not_implemented` と同じ考え方であり、
件数の据え置きではなく**内訳を全Field名で列挙して固定する**。

Baselineを `design-source/registries/` へ置かないのは `case-coverage.yaml` と同じ理由である。
あそこは設計書由来の正本であり、中身が `spec-manifest` のHashに入る。
「今どこまで是正したか」という進捗を混ぜると、是正のたびに正本側のHashが動く。

## 検査範囲（Owner Decision D-Q7）

現在は **Required Field関連の2種類だけ**を見る。

  * spec必須FieldがJSON Schemaの `properties` に無い（`absent`）
  * spec必須FieldがJSON Schemaの `required` に無い（`optional_only`）

型・Enum・Pattern・`additionalProperties`・条件付き必須は**未検査**である。
D-Q7は「Required Fieldを先行し、残りはBaseline方式で段階拡張」と決まった。
したがって現在の件数は**下限**であって全体像ではない。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE = REPO_ROOT / "tests" / "schema-contract-baseline.yaml"
REGISTRIES = REPO_ROOT / "design-source" / "registries"

# `spec/20-schemas/<Name>.md` の "必須Field：" 直後のバッククォート囲みCSV。
_REQUIRED_BLOCK = re.compile(r"必須Field：\s*\n\s*\n`([^`]+)`")


def registry_schemas() -> list[tuple[str, str, str]]:
    """`(schema_name, schema_version, path)` を返す。正本は schemas.yaml（不変条件#18）。

    複数Version形式（`versions:` を持つ行）にも対応する。Step 3 で
    schemas.yaml が複数Version形式へ移行しても本Toolを書き換えずに済む。
    """
    document = yaml.safe_load((REGISTRIES / "schemas.yaml").read_text(encoding="utf-8"))
    result: list[tuple[str, str, str]] = []
    for row in document["core_schemas"]:
        name = row["schema_name"]
        versions = row.get("versions")
        if versions:
            for entry in versions:
                result.append((name, entry["version"], entry["path"]))
        else:
            result.append((name, row["schema_version"], row["path"]))
    return result


def spec_required_fields(name: str) -> list[str]:
    path = REPO_ROOT / "spec" / "20-schemas" / f"{name}.md"
    if not path.is_file():
        return []
    match = _REQUIRED_BLOCK.search(path.read_text(encoding="utf-8"))
    if match is None:
        return []
    return [field.strip() for field in match.group(1).split(",") if field.strip()]


def measure() -> dict[str, dict[str, list[str]]]:
    """`Schema名@Version` -> {absent, optional_only}。整列はCode Point昇順で固定する。

    Keyへ Version を含めるのは、`1.0.0` が既知乖離のままでも `2.0.0` は
    乖離0を要求できるようにするためである（Owner Decision D-1a）。
    """
    result: dict[str, dict[str, list[str]]] = {}
    for name, version, relative in sorted(registry_schemas()):
        declared = spec_required_fields(name)
        document = json.loads((REPO_ROOT / relative).read_text(encoding="utf-8"))
        properties = set(document["properties"])
        required = set(document["required"])
        absent = sorted(field for field in declared if field not in properties)
        optional = sorted(
            field for field in declared if field in properties and field not in required
        )
        if absent or optional:
            result[f"{name}@{version}"] = {"absent": absent, "optional_only": optional}
    return result


def load_baseline() -> dict[str, Any]:
    if not BASELINE.is_file():
        return {"resolved": [], "known_divergence": {}}
    loaded: dict[str, Any] = yaml.safe_load(BASELINE.read_text(encoding="utf-8")) or {}
    loaded.setdefault("resolved", [])
    loaded.setdefault("known_divergence", {})
    return loaded


def write_baseline(measured: dict[str, dict[str, list[str]]], resolved: list[str]) -> None:
    header = (
        "# Schema契約のBaseline（tools/check_schema_contract.py）。\n"
        "#\n"
        "# spec/20-schemas/<Name>.md の必須Field列と\n"
        "# schemas/core/<Name>/<ver>.schema.json の properties / required の乖離を固定する。\n"
        "#\n"
        "# resolved         : 乖離0を要求するSchema@Version。是正したらここへ移す\n"
        "# known_divergence : 既知の乖離。**増えたら落ちる**。件数ではなく全Field名を列挙する\n"
        "#\n"
        "# 終了Code 0 は「乖離が無い」ではなく「既知の乖離から増えていない」を意味する。\n"
        "# 検査範囲はRequired Field関連の2種類だけであり、型・Enum・Pattern・\n"
        "# additionalProperties・条件付き必須は未検査（Owner Decision D-Q7で段階拡張）。\n"
    )
    payload = {"contract_version": 1, "resolved": sorted(resolved), "known_divergence": measured}
    BASELINE.write_text(
        header + yaml.safe_dump(payload, allow_unicode=True, sort_keys=True, width=100),
        encoding="utf-8",
    )


def evaluate(measured: dict[str, dict[str, list[str]]], baseline: dict[str, Any]) -> list[str]:
    """Baselineとの差分を違反リストとして返す。空なら合格。"""
    resolved: list[str] = list(baseline["resolved"])
    known: dict[str, dict[str, list[str]]] = baseline["known_divergence"]
    failures: list[str] = []

    for key in sorted(resolved):
        if key in measured:
            failures.append(
                f"RESOLVED_SCHEMA_REGRESSED: {key} は是正済み宣言だが乖離がある: {measured[key]}"
            )

    for key in sorted(measured):
        if key in resolved:
            continue
        recorded = known.get(key)
        if recorded is None:
            failures.append(f"NEW_DIVERGENCE: {key} はBaselineに無い: {measured[key]}")
            continue
        for kind in ("absent", "optional_only"):
            added = sorted(set(measured[key][kind]) - set(recorded.get(kind) or []))
            if added:
                failures.append(f"NEW_DIVERGENCE: {key}.{kind} に追加: {added}")

    for key in sorted(known):
        if key not in measured and key not in resolved:
            failures.append(
                f"BASELINE_STALE: {key} の乖離が解消している。"
                "--update-baseline で更新し resolved へ加えること"
            )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="現状をBaselineとして書き直す。是正後に明示的に実行する",
    )
    parser.add_argument("--report", action="store_true", help="内訳を表示する")
    args = parser.parse_args(argv)

    measured = measure()
    absent_total = sum(len(item["absent"]) for item in measured.values())
    optional_total = sum(len(item["optional_only"]) for item in measured.values())

    if args.update_baseline:
        write_baseline(measured, load_baseline()["resolved"])
        print(f"baseline updated: {len(measured)} entries / absent={absent_total}")
        return 0

    baseline = load_baseline()
    failures = evaluate(measured, baseline)

    print(f"schema contract: {len(registry_schemas())} schema versions")
    print(f"  divergent={len(measured)} resolved={len(baseline['resolved'])}")
    print(f"  absent={absent_total} optional_only={optional_total}")
    if args.report:
        for key in sorted(measured):
            item = measured[key]
            status = "UNRESOLVED_KNOWN_GAP" if key in baseline["known_divergence"] else "NEW"
            absent_count = len(item["absent"])
            optional_count = len(item["optional_only"])
            print(f"  [{status}] {key}: absent={absent_count} optional={optional_count}")

    if failures:
        print()
        for line in failures:
            print(f"FAIL {line}", file=sys.stderr)
        print("RESULT: schema contract regression", file=sys.stderr)
        return 1

    print("RESULT: REPORT_ONLY / known divergence — Baselineから増えていない")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
