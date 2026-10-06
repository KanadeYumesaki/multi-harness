#!/usr/bin/env python3
"""UCD 14.0.0 割当済み符号位置Bitmapを公式UnicodeData.txtから生成する（ADR-007）。

## なぜ必要か

Python 3.11 の `unicodedata` は UCD 14.0.0、3.12 は 15.0.0 である。
`unicodedata.normalize('NFC', s)` の結果は、`s` に15.0で追加された符号位置が含まれると
処理系間で食い違い得る。`source_normalized_hash` が処理系依存になると、
ADR-006 §4.3 の委任照合まで処理系依存になる。

Unicode Normalization Stability Policy は
「あるVersionで割当済みの文字だけから成る文字列の正規化形は、以降のVersionでも変化しない」
と保証する。したがって **入力を UCD 14.0 割当済み符号位置だけに限定すれば**、
標準の `unicodedata.normalize('NFC')` の出力は 3.11／3.12 で一致する。
NFCアルゴリズムを自前実装する必要はない。

本Toolが生成するのはその「割当済み集合」だけであり、正規化テーブルではない。

## 形式（`UCD14_ASSIGNED_BITMAP_V1`）

* ヘッダなしの固定長バイナリ。0x110000 bit = 1,114,112 bit = **139,264 bytes**
* 符号位置昇順、バイト内は LSB-first
      codepoint cp が割当済み  <=>  (data[cp // 8] >> (cp % 8)) & 1 == 1
* Artifact Hash は生Bytesに対する SHA-256

## 除外する符号位置

* Surrogate（一般カテゴリ `Cs`）。Unicode scalar value ではなくUTF-8で表現できない
* UnicodeData.txt に現れない符号位置（未割当・Noncharacter）

Private Use（`Co`）は割当済みとして含める。NFCは恒等であり安定である。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import urllib.request
from pathlib import Path
from typing import Final

UNICODE_SPACE: Final[int] = 0x110000
BITMAP_BYTES: Final[int] = UNICODE_SPACE // 8  # 139,264
ARTIFACT_FORMAT: Final[str] = "UCD14_ASSIGNED_BITMAP_V1"
DEFAULT_VERSION: Final[str] = "14.0.0"
DEFAULT_URL: Final[str] = "https://www.unicode.org/Public/{version}/ucd/UnicodeData.txt"


UCD_FIELD_COUNT: Final[int] = 15


def parse_assigned(unicode_data: str) -> set[int]:
    """UnicodeData.txt から割当済み符号位置集合を作る。

    `<..., First>` / `<..., Last>` の行対はRangeを表すため展開する。
    Surrogate（カテゴリ Cs）は除外する。

    **不正行を読み飛ばさない。** UnicodeData.txtは1行15 fieldの固定形式であり、
    これに合わない行は入力破損か形式変更を意味する。黙って`continue`すると
    割当済み集合が静かに欠け、Guardが本来Rejectすべき符号位置を通してしまう。
    `masking-policy.yaml`の`ucd_source_malformed_line: REJECT`に対応する。
    """
    assigned: set[int] = set()
    range_start: int | None = None
    range_category: str | None = None

    for number, raw in enumerate(unicode_data.splitlines(), 1):
        if not raw:
            continue
        fields = raw.split(";")
        if len(fields) != UCD_FIELD_COUNT:
            raise ValueError(
                f"malformed UnicodeData.txt line {number}: "
                f"expected {UCD_FIELD_COUNT} fields, got {len(fields)}"
            )
        try:
            codepoint = int(fields[0], 16)
        except ValueError as exc:
            raise ValueError(
                f"malformed UnicodeData.txt line {number}: code point field is not hexadecimal"
            ) from exc
        name = fields[1]
        category = fields[2]

        if name.endswith(", First>"):
            range_start = codepoint
            range_category = category
            continue
        if name.endswith(", Last>"):
            if range_start is None:
                raise ValueError(f"unmatched Last marker at U+{codepoint:04X}")
            if range_category != "Cs":
                assigned.update(range(range_start, codepoint + 1))
            range_start = None
            range_category = None
            continue

        if category != "Cs":
            assigned.add(codepoint)

    if range_start is not None:
        raise ValueError("unterminated First/Last range in UnicodeData.txt")
    return assigned


def build_bitmap(assigned: set[int]) -> bytes:
    data = bytearray(BITMAP_BYTES)
    for codepoint in assigned:
        if not 0 <= codepoint < UNICODE_SPACE:
            raise ValueError(f"codepoint out of range: U+{codepoint:04X}")
        data[codepoint // 8] |= 1 << (codepoint % 8)
    return bytes(data)


def _expected_hashes(registries: Path) -> tuple[str | None, str | None]:
    """`masking-policy.yaml`へ固定した期待Hashを読む。

    Registryを信頼根とし、期待値をTool側へ持たない。Registryが読めない、
    または項目が無い場合はNoneを返し、呼出側がFail-Closedを決める。
    """
    path = registries / "masking-policy.yaml"
    if not path.is_file():
        return None, None
    try:
        import yaml
    except ImportError:  # pragma: no cover - requirements-devに含まれる
        return None, None
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    normalization = document.get("normalization") or {}
    return (
        normalization.get("ucd_source_sha256"),
        normalization.get("engine_artifact_sha256"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument(
        "--source",
        type=Path,
        help="ローカルのUnicodeData.txt。未指定なら公式URLから取得する",
    )
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--emit-provenance",
        type=Path,
        help="由来Metadata（入力URL・入力Hash・Artifact Hash）の出力先",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="生成せず、既存Artifactが再生成結果と一致することだけを検査する",
    )
    parser.add_argument(
        "--registries",
        type=Path,
        default=Path("design-source/registries"),
        help="masking-policy.yaml から期待Hashを読む。信頼根はRegistry側に固定する",
    )
    parser.add_argument(
        "--allow-unpinned",
        action="store_true",
        help="期待Hash未登録のRegistryを許容する（初回Artifact生成時のみ）",
    )
    args = parser.parse_args(argv)

    expected_source, expected_artifact = _expected_hashes(args.registries)
    if not args.allow_unpinned and (expected_source is None or expected_artifact is None):
        print(
            "masking-policy.yaml に ucd_source_sha256 / engine_artifact_sha256 が無い。\n"
            "信頼根が固定されていない状態で生成・検査しない（Fail-Closed）。\n"
            "初回生成時のみ --allow-unpinned を使う。",
            file=sys.stderr,
        )
        return 4

    url = DEFAULT_URL.format(version=args.version)
    if args.source is not None:
        source_bytes = args.source.read_bytes()
        origin = str(args.source)
    else:
        with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - 固定https
            source_bytes = response.read()
        origin = url

    source_hash = hashlib.sha256(source_bytes).hexdigest()

    # 取得元の固定。Artifactが偶然一致しても入力が違えば拒否する。
    if expected_source is not None and f"sha256:{source_hash}" != expected_source:
        print(
            "UCD source hash mismatch (Fail-Closed):\n"
            f"  origin   : {origin}\n"
            f"  expected : {expected_source}\n"
            f"  actual   : sha256:{source_hash}",
            file=sys.stderr,
        )
        return 3

    assigned = parse_assigned(source_bytes.decode("utf-8"))
    bitmap = build_bitmap(assigned)

    if len(bitmap) != BITMAP_BYTES:  # pragma: no cover - 構造上あり得ない
        raise ValueError(f"bitmap size {len(bitmap)} != {BITMAP_BYTES}")

    artifact_hash = hashlib.sha256(bitmap).hexdigest()

    # 再生成結果そのものがRegistryの期待値と一致すること。
    if expected_artifact is not None and f"sha256:{artifact_hash}" != expected_artifact:
        print(
            "regenerated artifact does not match the pinned hash (Fail-Closed):\n"
            f"  expected : {expected_artifact}\n"
            f"  actual   : sha256:{artifact_hash}",
            file=sys.stderr,
        )
        return 3

    if args.check:
        if not args.out.exists():
            print(f"artifact missing: {args.out}", file=sys.stderr)
            return 1
        current = args.out.read_bytes()
        if current != bitmap:
            print(
                f"artifact is stale or tampered: {args.out}\n"
                f"  expected sha256:{artifact_hash}\n"
                f"  actual   sha256:{hashlib.sha256(current).hexdigest()}",
                file=sys.stderr,
            )
            return 1
        print(
            f"{args.out}: up to date (sha256:{artifact_hash})"
            f"{'' if expected_artifact is None else ' / pinned in masking-policy.yaml'}"
        )
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(bitmap)

    print(f"ucd version      : {args.version}")
    print(f"source           : {origin}")
    print(f"source sha256    : sha256:{source_hash}")
    print(f"assigned         : {len(assigned):,} codepoints")
    print(f"artifact         : {args.out} ({len(bitmap):,} bytes)")
    print(f"artifact sha256  : sha256:{artifact_hash}")

    if args.emit_provenance:
        provenance = {
            "artifact_format": ARTIFACT_FORMAT,
            "artifact_bit_order": "CODEPOINT_ASCENDING_LSB0",
            "artifact_size_bytes": len(bitmap),
            "artifact_sha256": f"sha256:{artifact_hash}",
            "unicode_data_version": args.version,
            "source_url": url,
            "source_sha256": f"sha256:{source_hash}",
            "assigned_codepoint_count": len(assigned),
            "excluded_categories": ["Cs"],
            "generated_at": dt.datetime.now(dt.UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "generator": "tools/build_ucd_assigned_bitmap.py",
        }
        args.emit_provenance.parent.mkdir(parents=True, exist_ok=True)
        args.emit_provenance.write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"provenance       : {args.emit_provenance}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
