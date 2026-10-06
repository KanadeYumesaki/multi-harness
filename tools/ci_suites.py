#!/usr/bin/env python3
"""Tier から pytest へ渡す Path を出す。**Workflow に一覧を持たせない。**

正本は `ci/test-tiers.yaml`。Workflow は

    python tools/ci_suites.py --tier "$TIER" > /tmp/suites.txt
    readarray -t suites < /tmp/suites.txt
    pytest "${suites[@]}" ...

の形で読む。一覧が 2 つの Workflow へ写ると、片方だけ直したときに静かに
食い違う。実際に `tests/` 直下の 4 File はどちらの一覧にも無く、29 件が
CI のどこでも走っていなかった。

## 存在しない Path を渡さない

`optional: true` の Suite は MVP0-A で未作成である。渡すと pytest が
「引数が無い」で落ち、CI が恒常的に赤くなって警報として機能しなくなる。
**存在するものだけを出す。** Suite を新設した時点で自動的に対象へ入る。

## 必須の Path が消えたら止める

`optional: false` の Path が消えていたら、黙って出力から外さない。Test File の
削除や改名を「対象が減っただけ」として通さない。

## 標準ライブラリだけで動かない

`ci/test-tiers.yaml` を読むので PyYAML が要る。CI では quality Job が依存を
入れたあとに呼ぶ。**判定（`ci_scope.py`）と違い、これは依存を入れたあとの
工程である。**
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "ci/test-tiers.yaml"

__all__ = [
    "DEEP_GROUP",
    "covered_paths_for",
    "load_registry",
    "paths_for",
    "scripts_for",
    "tiers",
]

#: 主 Step が取らない group。nightly が狙い撃ちで走らせる。
DEEP_GROUP = "deep"


def load_registry(path: Path | None = None) -> dict[str, Any]:
    """正本を読む。形が崩れていれば止める。"""
    source = path or REGISTRY
    if not source.is_file():
        raise SystemExit(f"Tier の正本がない: {source}")
    document: dict[str, Any] = yaml.safe_load(source.read_text(encoding="utf-8"))
    for key in ("version", "tier_order", "suites", "scripts"):
        if key not in document:
            raise SystemExit(f"{source.name}: {key} が無い")
    known = set(document["tier_order"])
    for entry in [*document["suites"], *document["scripts"]]:
        unknown = sorted(set(entry["tiers"]) - known)
        if unknown:
            raise SystemExit(f"{entry['path']}: 知らない Tier がある: {unknown}")
        if not entry["tiers"]:
            raise SystemExit(f"{entry['path']}: どの Tier にも入っていない")
    return document


def tiers(document: dict[str, Any]) -> list[str]:
    order: list[str] = list(document["tier_order"])
    return order


def _missing_required(document: dict[str, Any], repo_root: Path) -> list[str]:
    """必須なのに存在しない Path。**消えたことを出力から隠さない。**"""
    gone: list[str] = []
    for entry in document["suites"]:
        if not entry.get("optional", False) and not (repo_root / entry["path"]).exists():
            gone.append(entry["path"])
    for entry in document["scripts"]:
        if not (repo_root / entry["path"]).exists():
            gone.append(entry["path"])
    return sorted(gone)


def paths_for(
    document: dict[str, Any],
    tier: str,
    *,
    group: str | None = None,
    repo_root: Path | None = None,
    existing_only: bool = True,
) -> list[str]:
    """その Tier で pytest へ渡す Path。宣言順を保つ。

    `group` を省くと **`deep` 以外**を返す。deep は nightly が狙い撃ちで走らせる
    ので、主 Step が拾うと同じ Test を二重に実行することになる。
    """
    root = repo_root or ROOT
    found: list[str] = []
    for entry in document["suites"]:
        if tier not in entry["tiers"]:
            continue
        entry_group = entry.get("group")
        if group is None:
            if entry_group == DEEP_GROUP:
                continue
        elif entry_group != group:
            continue
        path = entry["path"]
        if existing_only and not (root / path).exists():
            continue
        found.append(path)
    return found


def covered_paths_for(
    document: dict[str, Any], tier: str, *, repo_root: Path | None = None
) -> list[str]:
    """その Tier で **どこかの Job が**走らせる Path。deep も含む。

    網羅の計算に使う。主 Step が渡す Path（`paths_for`）とは別である。
    """
    root = repo_root or ROOT
    return [
        entry["path"]
        for entry in document["suites"]
        if tier in entry["tiers"] and (root / entry["path"]).exists()
    ]


def scripts_for(document: dict[str, Any], tier: str) -> list[dict[str, Any]]:
    return [entry for entry in document["scripts"] if tier in entry["tiers"]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tier から pytest の Path を出す")
    parser.add_argument("--tier", help="draft / ready / full / deep")
    parser.add_argument("--group", help="coverage / integration。省略で全部")
    parser.add_argument("--registry", type=Path, default=REGISTRY)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true", help="解決結果を JSON で出す")
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="必須 Path が消えていても止めない（診断用）",
    )
    args = parser.parse_args(argv)

    document = load_registry(args.registry)
    gone = _missing_required(document, args.repo_root)
    if gone and not args.allow_missing:
        raise SystemExit(f"MISSING_REQUIRED_PATH: 正本にあるのに存在しない: {gone}")

    if args.json:
        resolved = {
            tier: {
                "pytest_paths": paths_for(document, tier, repo_root=args.repo_root),
                "scripts": [entry["path"] for entry in scripts_for(document, tier)],
            }
            for tier in tiers(document)
        }
        print(
            json.dumps(
                {"version": document["version"], "missing_required": gone, "tiers": resolved},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not args.tier:
        parser.error("--tier か --json のどちらかが要る")
    if args.tier not in tiers(document):
        parser.error(f"知らない Tier: {args.tier}")
    for path in paths_for(document, args.tier, group=args.group, repo_root=args.repo_root):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
