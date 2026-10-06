#!/usr/bin/env python3
"""`Extended_Pictographic` の符号位置集合Artifactを生成する（ADR-007 §3）。

## なぜ範囲近似をやめるのか

初版は `U+2190–U+2BFF` と `U+1F000–U+1FAFF` の範囲で近似していた。
「絵文字はこのあたりに固まっている」という当て推量である。

実測すると、この範囲は矢印・星・囲み数字を丸ごと含んでいた。

    ①山田太郎②佐藤花子  span=(1,5) -> MASKING_GRAPHEME_SPLIT
    担当山田太郎→承認    span=(2,6) -> MASKING_GRAPHEME_SPLIT

箇条書きの丸数字は日本語の業務文書で頻出する。「氏名の直後に記号が来る」
だけで入力全体が拒否される。**Grapheme Clusterを割らないための保守判定が、
Cluster と無関係な文字まで巻き込んでいた。**

近似の幅を手で調整しても、根拠が「たぶんこの辺」のままでは同じことが起きる。
Unicodeの実データを引いて、`Extended_Pictographic` の実際の集合を固定する。

## UCD Assigned Bitmap と同じ扱いにする

Version・Source Hash・Artifact Hashを `masking-policy.yaml` へ固定し、
生成物と入力の双方を照合する。期待値を持たない状態を「検査済み」と
扱わない。判定が変われば `MASKING_GRAPHEME_SPLIT` の発生条件が変わるため、
Artifactの素性は `source_normalized_hash` と同格に管理する必要がある。

## Artifact形式

`UCD14_EXTENDED_PICTOGRAPHIC_BITMAP_V1`。
0x110000 bit = 139,264 bytes。符号位置昇順のLSB-first。
Assigned Bitmap と同じ形にして、読み出しコードを共有できるようにする。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import urllib.request
from pathlib import Path
from typing import Final

import yaml

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
DEFAULT_VERSION: Final[str] = "14.0.0"
DEFAULT_URL: Final[str] = "https://www.unicode.org/Public/{version}/ucd/emoji/emoji-data.txt"

UNICODE_SPACE: Final[int] = 0x110000
BITMAP_BYTES: Final[int] = UNICODE_SPACE // 8

# 例: "231A..231B    ; Emoji  # E0.6  [2] (⌚..⌛)"
_LINE = re.compile(
    r"^\s*(?P<start>[0-9A-Fa-f]{4,6})(?:\.\.(?P<end>[0-9A-Fa-f]{4,6}))?\s*;\s*(?P<prop>[A-Za-z_]+)"
)


def parse_extended_pictographic(text: str) -> set[int]:
    """`Extended_Pictographic` の符号位置を集める。

    形式に合わない行を黙って飛ばさない。飛ばすと、取得物が壊れていても
    「該当なし」として通ってしまい、判定が静かに緩む。
    """
    found: set[int] = set()
    saw_property = False
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = _LINE.match(line)
        if match is None:
            raise ValueError(f"emoji-data.txt line {number} does not match the expected form")
        if match.group("prop") != "Extended_Pictographic":
            saw_property = True
            continue
        saw_property = True
        start = int(match.group("start"), 16)
        end = int(match.group("end") or match.group("start"), 16)
        if end < start or end >= UNICODE_SPACE:
            raise ValueError(f"emoji-data.txt line {number} has an invalid range")
        found.update(range(start, end + 1))
    if not saw_property:
        raise ValueError("emoji-data.txt contained no property assignments")
    if not found:
        raise ValueError("no Extended_Pictographic codepoints were found")
    return found


def build_bitmap(codepoints: set[int]) -> bytes:
    bitmap = bytearray(BITMAP_BYTES)
    for codepoint in codepoints:
        bitmap[codepoint // 8] |= 1 << (codepoint % 8)
    return bytes(bitmap)


def _expected(registries: Path) -> tuple[str | None, str | None]:
    document = yaml.safe_load((registries / "masking-policy.yaml").read_text(encoding="utf-8"))
    section = document.get("grapheme", {}) or {}
    return section.get("pictographic_artifact_sha256"), section.get("pictographic_source_sha256")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--source", type=Path, help="取得済みemoji-data.txt。省略時はDownload")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--registries", type=Path, default=REPO_ROOT / "design-source" / "registries"
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    # 相対Pathで呼ばれても表示・比較が壊れないようにする。
    args.out = args.out.resolve()

    if args.source is not None:
        raw = args.source.read_bytes()
    else:
        url = DEFAULT_URL.format(version=args.version)
        with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - 固定https
            raw = response.read()

    source_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
    bitmap = build_bitmap(parse_extended_pictographic(raw.decode("utf-8")))
    artifact_hash = "sha256:" + hashlib.sha256(bitmap).hexdigest()

    expected_artifact, expected_source = _expected(args.registries)

    if expected_source is None or expected_artifact is None:
        print(
            "masking-policy.yaml does not pin the pictographic hashes; "
            "refusing to treat an unpinned artifact as verified",
            file=sys.stderr,
        )
        if args.check:
            return 4
    else:
        if source_hash != expected_source:
            print(f"source hash {source_hash} != pinned {expected_source}", file=sys.stderr)
            return 3
        if artifact_hash != expected_artifact:
            print(f"artifact hash {artifact_hash} != pinned {expected_artifact}", file=sys.stderr)
            return 3

    if args.check:
        if not args.out.is_file() or args.out.read_bytes() != bitmap:
            print(f"{args.out} differs from the regenerated bitmap", file=sys.stderr)
            return 3
        print(f"{args.out.relative_to(REPO_ROOT)}: up to date ({artifact_hash})")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(bitmap)
    print(f"wrote {args.out.relative_to(REPO_ROOT)}")
    print(f"  source   : {source_hash}")
    print(f"  artifact : {artifact_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
