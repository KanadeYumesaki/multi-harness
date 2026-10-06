#!/usr/bin/env python3
"""`source_normalized_hash` の期待値Fixtureを生成・検査する（Gate 44）。

## なぜFixtureが要るのか

Gate 44 は「Python 3.11 と 3.12 で同じ `source_normalized_hash` になる」ことを
要求する。CIは両Versionで走っているが、**各Jobは片方のVersionしか見ない**。
Job同士で結果を突き合わせる仕組みが無いため、両方が緑でも
「互いに違う値を出して、それぞれ自分の値と一致していた」可能性を排除できない。

そこで期待値をRepositoryへ固定する。3.11のJobも3.12のJobも**同じ定数**と
照合するため、両者が食い違えばどちらかが必ず落ちる。これが実質的な
Cross-Version検査になる。

Skipで回避しない。不変条件#16「実行していないTestをPASSと書かない」により、
「もう片方のPythonが無いので飛ばす」という逃げ道は使えない。

## 入力の選び方

NFCで結果が変わる文字、UCD 14.0と15.0で扱いが違う文字を意図的に含める。
普通のASCIIだけでは、Guardが機能しているかどうかが分からない。
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from harness.infrastructure.masking.policy import MaskingPolicy  # noqa: E402
from harness.infrastructure.masking.ucd_guard import Ucd14Guard  # noqa: E402

FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "normalization" / "source_normalized_hash.json"

# NFCで結果が変わる／Versionで扱いが割れる入力を意図的に集める。
CASES: tuple[tuple[str, str], ...] = (
    ("ascii", "plain ascii text"),
    ("japanese", "四半期の売上を部門別に集計してください。"),
    # 合成済みと分解形。NFCで同じ結果になるべき組。
    ("composed_e_acute", "émile"),
    ("decomposed_e_acute", "émile"),
    # ハングル。NFCでSyllableへ合成される。
    ("hangul_jamo", "각"),
    ("hangul_syllable", "각"),
    # 合成順序が規範化される組（Canonical Ordering）。
    ("combining_order_a", "q̣̇"),
    ("combining_order_b", "q̣̇"),
    # 互換漢字。NFCでは変わらない（NFKCなら変わる）ことの確認を兼ねる。
    ("cjk_compatibility", "六葉"),
    # 全角・半角。NFCでは変わらない。
    ("fullwidth", "ＡＢＣ"),
    # 結合文字を伴う絵文字列。
    ("emoji_zwj", "x\U0001f468‍\U0001f469x"),
    # 異体字セレクタ。
    ("variation_selector", "葛︀"),
    # サロゲート範囲外の追加面。
    ("supplementary_plane", "\U00020bb7\U0002a6b2"),
    ("mixed", "émile@example.com へ連絡 각"),
)


def build() -> dict[str, object]:
    policy = MaskingPolicy.load(REPO_ROOT)
    guard = Ucd14Guard(
        REPO_ROOT / policy.normalization.artifact_path,
        expected_artifact_sha256=policy.normalization.artifact_sha256,
        profile_id=policy.normalization.profile_id,
        minimum_unicodedata_version=policy.normalization.runtime_minimum_version,
    )
    entries = []
    for name, text in CASES:
        result = guard.normalize(text)
        entries.append(
            {
                "name": name,
                # 生の文字を書かない。Fileが正規化されると期待値の意味が変わる。
                "text_codepoints": [f"U+{ord(ch):04X}" for ch in text],
                "source_normalized_hash": result.source_normalized_hash,
            }
        )
    # **実行時のUCD版数をFixtureへ入れてはならない。**
    #
    # 最初は provenance のつもりで `unicodedata.unidata_version` を入れていたが、
    # そのせいで 3.11(UCD 14.0) と 3.12(UCD 15.0) では必ずFileが食い違い、
    # Cross-Version検査が「常に不一致」になって用をなさなかった。
    # 実際に走らせて発覚した（Hash自体は14件すべて一致していた）。
    #
    # ここへ載せるのは **Versionをまたいで同じであるべき値だけ** である。
    # Artifactの素性は `profile_artifact_sha256` が担う。
    return {
        "profile_id": policy.normalization.profile_id,
        "profile_artifact_sha256": policy.normalization.artifact_sha256,
        "cases": entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="生成せず一致だけを確認する")
    args = parser.parse_args()

    document = build()
    rendered = json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True) + "\n"

    if args.check:
        if not FIXTURE_PATH.is_file():
            print(f"fixture missing: {FIXTURE_PATH}", file=sys.stderr)
            return 3
        current = FIXTURE_PATH.read_text(encoding="utf-8")
        if current != rendered:
            print(
                "normalization fixture is stale or this interpreter produces different hashes",
                file=sys.stderr,
            )
            return 3
        print(
            f"{FIXTURE_PATH.relative_to(REPO_ROOT)}: up to date "
            f"({len(document['cases'])} cases, UCD {unicodedata.unidata_version})"
        )  # type: ignore[arg-type]
        return 0

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(rendered, encoding="utf-8")
    print(f"wrote {FIXTURE_PATH.relative_to(REPO_ROOT)} ({len(document['cases'])} cases)")  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
