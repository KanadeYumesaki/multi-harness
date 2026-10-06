"""Artifact の値オブジェクトとCAS配置規則（Domain層・純粋関数）。

§1.14「Artifact Bytesの書込みはTemp write、File fsync、Atomic Rename、
Parent Directory fsync、Manifest登録の順とする」の**判断部分**だけを持つ。
実際のI/Oは`infrastructure/artifact/`が行う。

Domain層は`os`・`pathlib`をimportしないため、Pathは**文字列セグメントの列**として
返す。組み立ては具象層の責務とする（CLAUDE.md §2）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash

__all__ = [
    "ArtifactMetadata",
    "ArtifactVerification",
    "VerificationOutcome",
    "cas_object_segments",
    "cas_temp_segments",
]

# CASのShard深さ。1ディレクトリへFileが集中して列挙が遅くなるのを避ける。
_SHARD_WIDTH: Final[int] = 2
_SHARD_DEPTH: Final[int] = 2
_OBJECTS_ROOT: Final[str] = "objects"
_INCOMING_ROOT: Final[str] = "incoming"


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    """Put時に与える分類情報。

    §1.16.3「Artifact StoreへのPut前にClassification／Secret Scanを実行する」を
    通過した結果だけがここへ来る。Scan未実施のPutを型で防ぐことはできないため、
    Application層のUnit of Workが順序を保証する。
    """

    media_type: str
    size_bytes: int
    data_classification: str
    trust_level: str

    def __post_init__(self) -> None:
        if self.size_bytes < 0:
            raise ValueError("size_bytes must not be negative")
        for name in ("media_type", "data_classification", "trust_level"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")


class VerificationOutcome(Enum):
    """§1.14 Artifact StoreのRepair方針に対応する判定。"""

    OK = "OK"
    # Manifestがあり、Bytesが無い。§1.14「BLOCKED_REPAIR_REQUIRED」
    BYTES_MISSING = "BYTES_MISSING"
    # Bytesの再Hashが記録と一致しない。改ざんまたは破損
    CONTENT_MISMATCH = "CONTENT_MISMATCH"
    # Bytesだけがあり、Manifestが無い。§1.14「孤児としてGC候補」
    ORPHAN_BYTES = "ORPHAN_BYTES"


@dataclass(frozen=True, slots=True)
class ArtifactVerification:
    outcome: VerificationOutcome
    content_hash: ContentHash
    observed_hash: ContentHash | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is VerificationOutcome.OK

    def raise_if_repair_required(self) -> None:
        """§1.14「DBにManifestがありBytesがない場合は`BLOCKED_REPAIR_REQUIRED`」。

        推測で成功にせず停止する（不変条件#9）。
        """
        if self.outcome is VerificationOutcome.BYTES_MISSING:
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT,
                f"manifest exists but bytes are missing for {self.content_hash}; "
                "BLOCKED_REPAIR_REQUIRED",
            )
        if self.outcome is VerificationOutcome.CONTENT_MISMATCH:
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT,
                f"stored bytes do not hash to {self.content_hash} (observed {self.observed_hash})",
            )


def cas_object_segments(content_hash: ContentHash) -> tuple[str, ...]:
    """Content HashからCAS上の相対Path Segmentを導く。

    `objects/ab/cd/abcd…` の形にする。Hash文字列そのものをFile名にするため、
    同一Hashは必ず同一Pathへ落ちる（Content-addressed）。
    """
    digest = content_hash.hexdigest
    shards = tuple(
        digest[index * _SHARD_WIDTH : (index + 1) * _SHARD_WIDTH] for index in range(_SHARD_DEPTH)
    )
    return (_OBJECTS_ROOT, *shards, digest)


def cas_temp_segments(content_hash: ContentHash, *, attempt_token: str) -> tuple[str, ...]:
    """Atomic Rename元となるTemp Pathの相対Segment。

    §1.14「Bytesを**同一Filesystemの**Tempへwrite」に従い、CAS Root配下へ置く。
    `/tmp`等へ書くとRenameがFilesystemを跨ぎ、Atomicでなくなる。

    `attempt_token`は同一Artifactの並行Putを衝突させないためのもので、
    Content Hashの入力ではない。
    """
    if not attempt_token or "/" in attempt_token or "\0" in attempt_token:
        raise ValueError("attempt_token must be a non-empty path-safe token")
    return (_INCOMING_ROOT, f"{content_hash.hexdigest}.{attempt_token}.tmp")
