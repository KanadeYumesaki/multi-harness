"""ArtifactStorePort の実装。Bytes(CAS) と Manifest(SQLite) を束ねる。

§1.14 の順序どおり **Bytes書込みが完全に耐久化されてから** Manifestを登録する。
逆順にすると、Manifestは存在するのにBytesが無い状態が正常系で発生し、
§1.14 の`BLOCKED_REPAIR_REQUIRED`が「異常検出」ではなく日常になる。

Manifest登録は呼出側のTransactionの内側で行う。本Classは`commit()`しない
（不変条件#15）。したがって「Bytesは書けたがTransactionがRollbackした」場合は
**孤児Bytes**が残る。これは§1.14が「Bytesだけがある場合は孤児としてGC候補」と
定めた状態であり、逆（Manifestだけが残る）より安全である。
"""

from __future__ import annotations

from harness.domain.artifact import ArtifactMetadata, ArtifactVerification
from harness.domain.hashing import ContentHash
from harness.infrastructure.artifact.filesystem_cas import FilesystemArtifactCas
from harness.infrastructure.sqlite.artifact_manifest_repository import (
    SqliteArtifactManifestRepository,
)
from harness.ports.artifact_store import ArtifactManifestRecord

__all__ = ["ArtifactStore"]


class ArtifactStore:
    def __init__(
        self,
        cas: FilesystemArtifactCas,
        manifests: SqliteArtifactManifestRepository,
    ) -> None:
        self._cas = cas
        self._manifests = manifests

    def put(
        self,
        data: bytes,
        metadata: ArtifactMetadata,
        *,
        artifact_id: str,
        stored_at: str,
    ) -> ArtifactManifestRecord:
        # 1〜4: Temp write → File fsync → Atomic Rename → Directory fsync
        content_hash = self._cas.write_bytes(data, metadata)

        existing = self._manifests.find_by_content_hash(content_hash)
        if existing is not None:
            # 同一内容の再Putは冪等。Bytesの一致はwrite_bytesが照合済み。
            return existing

        # 5: Manifest登録
        record = ArtifactManifestRecord(
            artifact_id=artifact_id,
            content_hash=content_hash,
            media_type=metadata.media_type,
            size_bytes=metadata.size_bytes,
            data_classification=metadata.data_classification,
            trust_level=metadata.trust_level,
            stored_at=stored_at,
            storage_path=self._cas.relative_object_path(content_hash),
        )
        self._manifests.register(record)
        return record

    def get(self, content_hash: ContentHash) -> bytes:
        return self._cas.read_bytes(content_hash)

    def verify(self, content_hash: ContentHash) -> ArtifactVerification:
        manifest = self._manifests.find_by_content_hash(content_hash)
        return self._cas.verify(content_hash, manifest_exists=manifest is not None)
