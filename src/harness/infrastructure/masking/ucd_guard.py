"""UCD 14.0 割当済み符号位置Guard と NFC正規化（ADR-007）。

Python 3.11 の `unicodedata` は UCD 14.0.0、3.12 は 15.0.0 である。
15.0で追加された符号位置を含む文字列は NFC 結果が処理系間で食い違い得る。
`source_normalized_hash` が処理系依存になると、ADR-006 §4.3 経由で
委任の照合結果まで処理系依存になる。

Unicode Normalization Stability Policy は「あるVersionで割当済みの文字だけから
成る文字列の正規化形は以降のVersionでも変化しない」と保証する。したがって
**入力を UCD 14.0 割当済み符号位置へ限定したうえで標準NFCを使う**。
NFCアルゴリズムを自前実装しない。

## Guardは正規化の「前」に置く

未割当符号位置は Python 3.11 では素通り、3.12 では分解され得る。
正規化の**後**に検査したのでは、処理系差異を吸収できない。
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from harness.domain.errors import ErrorCode, HarnessError

__all__ = ["NormalizationResult", "Ucd14Guard"]

_UNICODE_SPACE: Final[int] = 0x110000
_BITMAP_BYTES: Final[int] = _UNICODE_SPACE // 8  # 139,264


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    normalized: str
    source_normalized_hash: str
    normalization_profile: str
    normalization_profile_artifact_hash: str


class Ucd14Guard:
    """割当済み集合Bitmapを持ち、Guard→NFCを行う。"""

    def __init__(
        self,
        artifact_path: Path,
        *,
        expected_artifact_sha256: str,
        profile_id: str,
        minimum_unicodedata_version: str,
    ) -> None:
        self._profile_id = profile_id
        self._artifact_hash = expected_artifact_sha256
        self._bitmap = self._load(artifact_path, expected_artifact_sha256)
        self._require_runtime_version(minimum_unicodedata_version)

    # ------------------------------------------------------------------

    @staticmethod
    def _load(path: Path, expected_hash: str) -> bytes:
        if not path.is_file():
            raise HarnessError(
                ErrorCode.MASKING_NORMALIZATION_ARTIFACT_MISSING,
                f"UCD assigned-codepoint bitmap not found: {path}",
            )
        data = path.read_bytes()
        if len(data) != _BITMAP_BYTES:
            raise HarnessError(
                ErrorCode.MASKING_NORMALIZATION_ARTIFACT_MISSING,
                f"bitmap size {len(data)} != {_BITMAP_BYTES}",
            )
        actual = "sha256:" + hashlib.sha256(data).hexdigest()
        if actual != expected_hash:
            raise HarnessError(
                ErrorCode.MASKING_NORMALIZATION_ARTIFACT_MISSING,
                f"bitmap hash {actual} does not match pinned {expected_hash}",
            )
        return data

    @staticmethod
    def _require_runtime_version(minimum: str) -> None:
        """`unicodedata` が UCD 14.0 の文字集合を知っていること。

        14.0未満だと、14.0で追加された文字の正規化を知らないため
        Guardを通した入力でも結果が食い違う。
        """
        actual = unicodedata.unidata_version
        if tuple(int(part) for part in actual.split(".")) < tuple(
            int(part) for part in minimum.split(".")
        ):
            raise HarnessError(
                ErrorCode.MASKING_NORMALIZATION_PROFILE_MISMATCH,
                f"runtime unicodedata {actual} is older than required {minimum}",
            )

    # ------------------------------------------------------------------

    def is_assigned(self, codepoint: int) -> bool:
        """`UCD14_ASSIGNED_BITMAP_V1`: 符号位置昇順・LSB-first。"""
        if not 0 <= codepoint < _UNICODE_SPACE:
            return False
        return bool((self._bitmap[codepoint // 8] >> (codepoint % 8)) & 1)

    def normalize(self, text: str) -> NormalizationResult:
        """Guardを通してからNFC正規化し、`source_normalized_hash`を返す。

        未割当符号位置が1つでもあれば正規化前に停止する。
        """
        for index, character in enumerate(text):
            if not self.is_assigned(ord(character)):
                raise HarnessError(
                    ErrorCode.MASKING_UNSUPPORTED_CODEPOINT,
                    f"U+{ord(character):04X} at index {index} is not assigned in "
                    f"UCD 14.0; normalization would be interpreter-dependent",
                )

        normalized = unicodedata.normalize("NFC", text)

        # ProfileとArtifact HashをHash入力へ含める。Profileが変われば
        # 同じ本文でも別のHashになり、下流の束縛が自動的に無効化される。
        payload = (
            b"FDE-HARNESS/normalized-source/3/"
            + self._profile_id.encode("utf-8")
            + self._artifact_hash.encode("utf-8")
            + normalized.encode("utf-8")
        )
        return NormalizationResult(
            normalized=normalized,
            source_normalized_hash="sha256:" + hashlib.sha256(payload).hexdigest(),
            normalization_profile=self._profile_id,
            normalization_profile_artifact_hash=self._artifact_hash,
        )
