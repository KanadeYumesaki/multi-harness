#!/usr/bin/env python3
"""英語名の正本と日本語名の同期ミラーが一致していることを検査する。

## なぜ検査が要るか

Owner Decision E-1 第3項は「英語名が正本、日本語名は内容一致を機械検査する派生」と
定めた。命名規則を決めるだけでは足りない。同内容2Fileは、**片方だけが更新された
瞬間**に「正本を名乗るものが2つあり突き合わせが無い」状態へ落ちる。

これは仮定の話ではない。§1.7.1 の `error-codes-v1.json` は、設計書が正本と
宣言していたのにRepositoryに実在しなかった。宣言と実体の突き合わせが無い箇所は
静かにずれる。だから宣言ではなく検査を置く。

## 何を一致とみなすか

**Bytes完全一致**である。行末や正規化の違いを許容しない。Design Hashは
`sha256(File Bytes)` で計算され、`registry-snapshot.json`の`design_sha256`、
`spec/spec-manifest.json`の`source_hash`、README、BLOCKED Recordの`design_sha256`が
その値へ束縛される。Bytesが1つでも違えば別の設計書であり、どちらの束縛が
正しいのか判定できない（不変条件#9）。

## 終了Code

| Code | 意味 |
|---|---|
| `0` | 正本とミラーがBytes一致 |
| `1` | 不一致、またはどちらかが存在しない |
| `4` | 引数・入出力が不正 |
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

DEFAULT_CANONICAL = "design-v1.25-runtime-go.md"
DEFAULT_MIRROR = (
    "マルチプロバイダーAI業務実行統制基盤_フェーズ別詳細設計書_v1.25_Runtime_GO判定実行版.md"
)


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical", type=Path, default=Path(DEFAULT_CANONICAL))
    parser.add_argument("--mirror", type=Path, default=Path(DEFAULT_MIRROR))
    args = parser.parse_args(argv)

    missing = [path for path in (args.canonical, args.mirror) if not path.is_file()]
    if missing:
        for path in missing:
            print(f"missing: {path}", file=sys.stderr)
        return 1

    try:
        canonical_hash = digest(args.canonical)
        mirror_hash = digest(args.mirror)
    except OSError as exc:
        print(f"input invalid: {exc}", file=sys.stderr)
        return 4

    if canonical_hash != mirror_hash:
        print("design mirror is out of sync", file=sys.stderr)
        print(f"  canonical {args.canonical}: {canonical_hash}", file=sys.stderr)
        print(f"  mirror    {args.mirror}: {mirror_hash}", file=sys.stderr)
        print(
            "正本は英語名File。ミラーを正本からCopyし直すこと "
            f"(cp '{args.canonical}' '{args.mirror}')",
            file=sys.stderr,
        )
        return 1

    print(f"design mirror in sync: {canonical_hash}")
    print(f"  canonical {args.canonical}")
    print(f"  mirror    {args.mirror}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
