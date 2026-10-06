"""§1.16 Input Read Capability と Path検証（Domain層・純粋関数）。

§1.16.2「Linux Safe Read規則」の拒否対象のうち、**Filesystemを見なくても
判定できるもの**をここで扱う。Symlink・Mount越境・特殊Fileの判定は実体を見る
必要があるため`infrastructure/filesystem/`が行う。

分けている理由は、Path文字列だけで弾ける攻撃（絶対Path、`..`、NUL）は
Filesystemへ触れる前に落としたいためである。実体へ触れてから判定すると、
判定前にsymlink追跡やDevice openが起きうる。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

from harness.domain.errors import ErrorCode

__all__ = [
    "CapabilityScope",
    "DenialReason",
    "ReadDecision",
    "ReadDenial",
    "normalize_relative_segments",
    "validate_relative_path",
]


class ReadDecision(Enum):
    """`INPUT_READ_DECISION` 名前空間のState。"""

    ALLOWED = "ALLOWED"
    DENIED = "DENIED"


class DenialReason(Enum):
    """§1.16.2 の拒否理由。対応するError Codeを持つ。"""

    PATH_OUTSIDE_CAPABILITY = ErrorCode.PATH_OUTSIDE_CAPABILITY
    SYMLINK_DENIED = ErrorCode.SYMLINK_DENIED
    MOUNT_CROSSING_DENIED = ErrorCode.MOUNT_CROSSING_DENIED
    SPECIAL_FILE_DENIED = ErrorCode.SPECIAL_FILE_DENIED

    @property
    def error_code(self) -> ErrorCode:
        return self.value


@dataclass(frozen=True, slots=True)
class ReadDenial:
    """拒否の記録。

    §1.16.2「拒否時は`ContextSelectionReceipt.rejected_input_resources`へ
    Capability ID、要求Path、Canonical候補Path、File Identity、Reason Code、
    検証時刻を保存する」に対応する。**Bytesは保存しない。**
    """

    reason: DenialReason
    capability_id: str
    requested_path: str
    canonical_candidate: str | None = None
    file_identity: str | None = None
    detail: str = ""

    @property
    def decision(self) -> ReadDecision:
        return ReadDecision.DENIED

    @property
    def error_code(self) -> ErrorCode:
        return self.reason.error_code


# Path Segmentへ許さない文字。制御文字全般とNULを弾く。
_FORBIDDEN_SEGMENTS: Final[frozenset[str]] = frozenset({"", ".", ".."})


def validate_relative_path(path: str) -> tuple[str, ...] | None:
    """Workspace相対Pathとして受理できるならSegment列を返し、駄目ならNone。

    実体を見ずに判定できるものだけを扱う。

    * 空Path、絶対Path（`/`始まり）
    * `..`（Path Traversal）と`.`
    * NULおよび制御文字
    * Windows系のDrive／UNC／Device Path（MVP2-A以前は§1.16.2が拒否）
    * 連続する区切りや末尾区切り
    """
    if not path or path.startswith("/") or path.startswith("\\"):
        return None
    if "\0" in path or any(ord(character) < 0x20 for character in path):
        return None
    # Windows Drive（`C:`）／UNC（`\\host`）／Device Path（`\\?\`）
    if "\\" in path or (len(path) >= 2 and path[1] == ":"):
        return None

    segments = tuple(path.split("/"))
    if any(segment in _FORBIDDEN_SEGMENTS for segment in segments):
        return None
    return segments


def normalize_relative_segments(path: str) -> tuple[str, ...]:
    """受理できるPathのSegment列を返す。受理できなければ`ValueError`。"""
    segments = validate_relative_path(path)
    if segments is None:
        raise ValueError(f"path is not a safe workspace-relative path: {path!r}")
    return segments


@dataclass(frozen=True, slots=True)
class CapabilityScope:
    """Capabilityが許す読取り範囲。

    `allowed_prefixes` はWorkspace相対のDirectory prefix、または完全一致Path。
    Glob・正規表現・否定は持たない。ADR-006 のPredicateと同じ方針で、
    範囲が読んで分かる形だけを許す。
    """

    allowed_prefixes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.allowed_prefixes:
            raise ValueError("capability scope must not be empty")
        for prefix in self.allowed_prefixes:
            if validate_relative_path(prefix.rstrip("/")) is None:
                raise ValueError(f"unsafe scope prefix: {prefix!r}")
            for character in ("*", "?", "["):
                if character in prefix:
                    raise ValueError(f"scope prefix must not contain glob: {prefix!r}")

    def permits(self, path: str) -> bool:
        """`path`がScope内か。Segment境界で比較する。

        文字列prefixで比較すると `docs` が `docs-secret/…` を許してしまう。
        """
        segments = validate_relative_path(path)
        if segments is None:
            return False
        for prefix in self.allowed_prefixes:
            allowed = validate_relative_path(prefix.rstrip("/"))
            if allowed is None:
                continue
            if segments[: len(allowed)] == allowed:
                return True
        return False
