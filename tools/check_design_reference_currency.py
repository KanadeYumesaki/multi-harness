#!/usr/bin/env python3
"""旧設計書File名への参照を分類し、増えていないことを検査する。

## なぜ検査が要るか

v1.8 → v1.9 の改名で、`design-v1.8-runtime-go.md` は**存在しないPath**になった。
それでもRepository内には旧名への参照が残る。**残ってよいものと、いけないものがある。**

| 種類 | 例 | 扱い |
|---|---|---|
| 起きたことの記録 | 過去Review、完了済Task Brief、過去実行のEvidence Script | **残す** |
| 改名そのものの記録 | §23.4、決裁Brief、Step 4 Evidence、許可Path一覧 | **残す** |
| 運用上の指し先 | CI、`run_verification.sh`、現行Task Brief、生成Tool | **残してはならない** |

記録側を書き換えると「その時点で何を見ていたか」が消える。
`runtime-evidence/TASK-MVP0A-003/run-checks.sh` は、当時 v1.8 のFileに対して
検査を走らせた事実の証跡である。v1.9 へ書き換えると、実行していない検査を
実行したことにしてしまう（不変条件#16）。

一方、**新しく書いたScriptが旧名を指していたら、それは実行時に落ちるバグである。**
両者は同じ文字列で、意図だけが違う。意図は機械には見えないので、
**許可リストで固定する。**

## Baseline方式である

`tests/schema-contract-baseline.yaml` と同じ考え方を採る。
終了Code `0` は「旧名参照が0件」ではなく「**既知の歴史参照から増えていない**」を意味する。

| Code | 意味 |
|---|---|
| `0` | 参照集合が許可リストと一致する |
| `1` | `NEW_STALE_REFERENCE`／`BASELINE_STALE`／現行設計書が無い |
| `4` | 入出力が不正 |

`NEW_STALE_REFERENCE` は許可外Fileが旧名を参照している状態、
`BASELINE_STALE` は許可Fileが旧名を参照しなくなった状態である。

`BASELINE_STALE` も失敗にする。参照しなくなったFileを許可リストへ残すと、
リストが現実から離れ、次に見た人が何を守っているのか分からなくなる。

## 非公開の記録を指す許可は別Fileに置く

公開用の配布コピーには、過去の回答・Block Record・履歴生成Toolを収録しない。
それらを指す許可項目は `docs/private-history/design-reference-allowlist.json`
（配布コピーに入れないFile）へ分け、存在するときだけ本表へ合わせる。
配布コピーでは本表だけが効き、非公開側では両方が効く。どちらの木でも
`NEW_STALE_REFERENCE` と `BASELINE_STALE` の規則は変わらない。非公開側で
このFileが消えれば、記録側の参照が `NEW_STALE_REFERENCE` として落ちる。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Final

CURRENT_DESIGN: Final[str] = "design-v1.25-runtime-go.md"

# 検出対象。改名前の正本File名（英語名・日本語名）。
# 改訂のたびにここへ**足す**。消すと、その版への陳腐化参照を見逃す。
LEGACY_NAMES: Final[tuple[str, ...]] = (
    "design-v1.8-runtime-go.md",
    "マルチプロバイダーAI業務実行統制基盤_フェーズ別詳細設計書_v1.8_Runtime_GO判定実行版.md",
    "design-v1.9-runtime-go.md",
    "マルチプロバイダーAI業務実行統制基盤_フェーズ別詳細設計書_v1.9_Runtime_GO判定実行版.md",
    "design-v1.10-runtime-go.md",
    "マルチプロバイダーAI業務実行統制基盤_フェーズ別詳細設計書_v1.10_Runtime_GO判定実行版.md",
    "design-v1.11-runtime-go.md",
    "マルチプロバイダーAI業務実行統制基盤_フェーズ別詳細設計書_v1.11_Runtime_GO判定実行版.md",
    "design-v1.12-runtime-go.md",
    "design-v1.19-runtime-go.md",
    "マルチプロバイダーAI業務実行統制基盤_フェーズ別詳細設計書_v1.19_Runtime_GO判定実行版.md",
    "design-v1.20-runtime-go.md",
    "マルチプロバイダーAI業務実行統制基盤_フェーズ別詳細設計書_v1.20_Runtime_GO判定実行版.md",
    "design-v1.21-runtime-go.md",
    "design-v1.22-runtime-go.md",
    "design-v1.23-runtime-go.md",
    "マルチプロバイダーAI業務実行統制基盤_フェーズ別詳細設計書_v1.23_Runtime_GO判定実行版.md",
    "マルチプロバイダーAI業務実行統制基盤_フェーズ別詳細設計書_v1.22_Runtime_GO判定実行版.md",
    "マルチプロバイダーAI業務実行統制基盤_フェーズ別詳細設計書_v1.21_Runtime_GO判定実行版.md",
    "マルチプロバイダーAI業務実行統制基盤_フェーズ別詳細設計書_v1.12_Runtime_GO判定実行版.md",
)

# 旧名を参照してよいFileと、その理由。
# **理由を書けないFileはこのリストへ入れない。** 理由が書けないなら、
# それは歴史記録ではなく直し忘れである。配布コピーに入れない記録を指す項目は
# PRIVATE_HISTORY_ALLOWLIST 側へ置く（本Fileの冒頭を参照）。
ALLOWED_HISTORICAL_REFERENCES: Final[dict[str, str]] = {
    # --- 起きたことの記録 ---
    "MIGRATION-v1.6-to-v1.7.md": "v1.6→v1.7移行時の記録",
    # --- 改名そのものの記録。旧名が記述の対象である ---
    "design-v1.25-runtime-go.md": "§23.4〜§23.8 の改名記録。旧名→新名を本文に残す",
    "マルチプロバイダーAI業務実行統制基盤_フェーズ別詳細設計書_v1.25_Runtime_GO判定実行版.md": (
        "英語正本の同期ミラー。内容がBytes一致するため同じ記述を含む"
    ),
    # --- 本検査自身 ---
    "tools/check_design_reference_currency.py": "検出対象の文字列を定義するFile",
    "tests/spec_lint/test_design_reference_currency.py": "本検査の試験",
}

#: 配布コピーに入れない記録を指す許可項目の置き場。存在するときだけ読む。
PRIVATE_HISTORY_ALLOWLIST: Final[str] = "docs/private-history/design-reference-allowlist.json"


class AllowlistInvalid(ValueError):
    """補助許可Fileの形が不正。黙って空扱いにしない。"""


def allowed_references(root: Path) -> dict[str, str]:
    """本表と、あれば非公開側の補助許可Fileを合わせた許可リストを返す。"""
    merged = dict(ALLOWED_HISTORICAL_REFERENCES)
    supplement = root / PRIVATE_HISTORY_ALLOWLIST
    if not supplement.exists() and not supplement.is_symlink():
        return merged
    try:
        payload = json.loads(supplement.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AllowlistInvalid(f"{PRIVATE_HISTORY_ALLOWLIST}: unreadable") from exc
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if payload.get("version") != 1 or not isinstance(entries, dict) or not entries:
        raise AllowlistInvalid(f"{PRIVATE_HISTORY_ALLOWLIST}: expected version 1 entries")
    for relative, reason in entries.items():
        if not isinstance(relative, str) or not isinstance(reason, str) or len(reason) <= 5:
            raise AllowlistInvalid(f"{PRIVATE_HISTORY_ALLOWLIST}: every entry needs a reason")
        if relative in merged:
            raise AllowlistInvalid(f"{PRIVATE_HISTORY_ALLOWLIST}: duplicate entry {relative}")
        merged[relative] = reason
    return merged


SKIP_DIR_PARTS: Final[frozenset[str]] = frozenset(
    {".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "node_modules"}
)

TEXT_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".md", ".py", ".sh", ".yml", ".yaml", ".json", ".txt", ".toml", ".cfg", ".ini"}
)


def find_references(root: Path) -> dict[str, list[str]]:
    """旧名を参照するFileを集める。戻り値は `{相対Path: [見つかった旧名]}`。"""
    found: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if SKIP_DIR_PARTS & set(path.relative_to(root).parts):
            continue
        if path.suffix not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hits = [name for name in LEGACY_NAMES if name in text]
        if hits:
            found[path.relative_to(root).as_posix()] = hits
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)

    root = args.root.resolve()
    if not root.is_dir():
        print(f"input invalid: {root} is not a directory", file=sys.stderr)
        return 4

    errors: list[str] = []
    if not (root / CURRENT_DESIGN).is_file():
        errors.append(f"現行設計書が無い: {CURRENT_DESIGN}")

    found = find_references(root)
    try:
        allowed = set(allowed_references(root))
    except AllowlistInvalid as exc:
        print(f"input invalid: {exc}", file=sys.stderr)
        return 4

    for relative in sorted(set(found) - allowed):
        errors.append(
            f"NEW_STALE_REFERENCE {relative}: 旧設計書名を参照している。"
            f"運用上の指し先なら {CURRENT_DESIGN} へ更新し、"
            "歴史記録なら理由を添えて ALLOWED_HISTORICAL_REFERENCES へ追加すること"
        )
    for relative in sorted(allowed - set(found)):
        errors.append(
            f"BASELINE_STALE {relative}: 旧設計書名を参照しなくなった。許可リストから削除すること"
        )

    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        return 1

    print(f"design reference currency ok: current={CURRENT_DESIGN}")
    print(f"  歴史参照として許可: {len(found)} File（新規の陳腐化参照なし）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
