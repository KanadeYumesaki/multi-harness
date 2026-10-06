"""§1.6 ArtifactStorePort。

| Operation | 入力 | 出力 |
|---|---|---|
| `put` | Bytes、Artifact Metadata | Artifact Manifest |
| `get` | Content Hash | Bytes |
| `verify` | Content Hash | Verification Result |

Artifact BytesだけがDB外にある（ADR-001）。ManifestはSQLite側であり、
Bytes書込みとManifest登録の順序はEffect Protocolに従う（§1.14）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from harness.domain.artifact import ArtifactMetadata, ArtifactVerification
from harness.domain.hashing import ContentHash

__all__ = ["ArtifactManifestRecord", "ArtifactStorePort"]


@dataclass(frozen=True, slots=True)
class ArtifactManifestRecord:
    """Put結果。`storage_path`はCAS Rootからの相対Pathとする。

    絶対Pathを持たせるとWorkspace移動でManifestが壊れ、
    Backup／Restoreの移送も難しくなる。
    """

    artifact_id: str
    content_hash: ContentHash
    media_type: str
    size_bytes: int
    data_classification: str
    trust_level: str
    stored_at: str
    storage_path: str
    payload_deleted: bool = False


class ArtifactStorePort(Protocol):
    def put(
        self, data: bytes, metadata: ArtifactMetadata, *, artifact_id: str, stored_at: str
    ) -> ArtifactManifestRecord:
        """Bytesを保存してManifestを返す。

        同一Content Hashが既に存在する場合は内容を再Hashして照合し、
        不一致なら`ARTIFACT_CONTENT_CONFLICT`で停止する（§1.14）。
        一致する場合は冪等に既存Manifestを返す。
        """
        ...

    def get(self, content_hash: ContentHash) -> bytes: ...

    def verify(self, content_hash: ContentHash) -> ArtifactVerification: ...
