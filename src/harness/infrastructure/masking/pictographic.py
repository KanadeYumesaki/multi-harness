"""`Extended_Pictographic` 判定表の読込み（ADR-007 §3）。

## 範囲近似をやめた理由

初版は `U+2190–U+2BFF` と `U+1F000–U+1FAFF` の範囲で近似していた。
「絵文字はこのあたりに固まっている」という当て推量である。

実測すると、この範囲は矢印・星・囲み数字を丸ごと含んでいた。

    ①山田太郎②佐藤花子  span=(1,5) -> MASKING_GRAPHEME_SPLIT
    担当山田太郎→承認    span=(2,6) -> MASKING_GRAPHEME_SPLIT

箇条書きの丸数字は日本語の業務文書で頻出する。**Grapheme Clusterを割らない
ための保守判定が、Clusterと無関係な文字まで巻き込んでいた。**

近似の幅を手で調整しても、根拠が「たぶんこの辺」のままでは同じことが起きる。
Unicodeの実データ（`emoji-data.txt`）から生成した表を引く。

## UCD Assigned Bitmap と同じ扱い

Version・Source Hash・Artifact HashをRegistryへ固定し、双方を照合する。
判定が変われば `MASKING_GRAPHEME_SPLIT` の発生条件が変わるため、
Artifactの素性は `source_normalized_hash` と同格に管理する。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

from harness.domain.errors import ErrorCode, HarnessError

__all__ = ["ExtendedPictographicTable"]

_UNICODE_SPACE: Final[int] = 0x110000
_BITMAP_BYTES: Final[int] = _UNICODE_SPACE // 8


class ExtendedPictographicTable:
    """`UCD14_EXTENDED_PICTOGRAPHIC_BITMAP_V1` を保持する。

    形式は Assigned Bitmap と同じ（符号位置昇順・LSB-first、139,264 bytes）。
    読み出しの実装を揃えておくと、片方だけ壊れる形の誤りが起きにくい。
    """

    def __init__(self, artifact_path: Path, *, expected_sha256: str) -> None:
        self._bitmap = self._load(artifact_path, expected_sha256)

    @staticmethod
    def _load(path: Path, expected: str) -> bytes:
        if not path.is_file():
            raise HarnessError(
                ErrorCode.MASKING_NORMALIZATION_ARTIFACT_MISSING,
                f"Extended_Pictographic bitmap not found: {path}",
            )
        data = path.read_bytes()
        if len(data) != _BITMAP_BYTES:
            raise HarnessError(
                ErrorCode.MASKING_NORMALIZATION_ARTIFACT_MISSING,
                f"pictographic bitmap size {len(data)} != {_BITMAP_BYTES}",
            )
        actual = "sha256:" + hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise HarnessError(
                ErrorCode.MASKING_NORMALIZATION_ARTIFACT_MISSING,
                f"pictographic bitmap hash {actual} does not match pinned {expected}",
            )
        return data

    def __call__(self, codepoint: int) -> bool:
        """`PictographicLookup` として使えるようにする。"""
        if not 0 <= codepoint < _UNICODE_SPACE:
            return False
        return bool((self._bitmap[codepoint // 8] >> (codepoint % 8)) & 1)
