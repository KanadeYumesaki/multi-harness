"""ArtifactManifest の SQLite Repository。

不変条件#15に従い`commit()`しない。Transaction所有者は呼出側のUnit of Work。
Bytes は DB外のCASにあり、本Repositoryは Manifest だけを扱う（ADR-001）。
"""

from __future__ import annotations

import sqlite3

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash
from harness.infrastructure.sqlite.transaction_guard import require_transaction
from harness.ports.artifact_store import ArtifactManifestRecord

__all__ = ["SqliteArtifactManifestRepository"]


class SqliteArtifactManifestRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def register(self, record: ArtifactManifestRecord) -> None:
        require_transaction(self._connection, "artifact manifest register")
        try:
            self._connection.execute(
                "INSERT INTO artifact_manifest ("
                " artifact_id, content_hash, media_type, size_bytes,"
                " data_classification, trust_level, stored_at, storage_path,"
                " payload_deleted, store_version"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.artifact_id,
                    str(record.content_hash),
                    record.media_type,
                    record.size_bytes,
                    record.data_classification,
                    record.trust_level,
                    record.stored_at,
                    record.storage_path,
                    int(record.payload_deleted),
                    1,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT,
                f"manifest registration rejected for {record.content_hash}: {exc}",
            ) from exc

    def find_by_content_hash(self, content_hash: ContentHash) -> ArtifactManifestRecord | None:
        row = self._connection.execute(
            "SELECT artifact_id, content_hash, media_type, size_bytes,"
            " data_classification, trust_level, stored_at, storage_path, payload_deleted"
            " FROM artifact_manifest WHERE content_hash = ?",
            (str(content_hash),),
        ).fetchone()
        return None if row is None else self._to_record(row)

    def mark_payload_deleted(self, content_hash: ContentHash) -> None:
        require_transaction(self._connection, "artifact manifest payload deletion")
        """GC。Bytesを消してもMetadataは残す（AT-GC-001/RAW_RETENTION）。"""
        self._connection.execute(
            "UPDATE artifact_manifest SET payload_deleted = 1 WHERE content_hash = ?",
            (str(content_hash),),
        )

    def all_content_hashes(self) -> list[ContentHash]:
        rows = self._connection.execute(
            "SELECT content_hash FROM artifact_manifest ORDER BY content_hash ASC"
        ).fetchall()
        return [ContentHash.parse(row["content_hash"]) for row in rows]

    @staticmethod
    def _to_record(row: sqlite3.Row) -> ArtifactManifestRecord:
        return ArtifactManifestRecord(
            artifact_id=row["artifact_id"],
            content_hash=ContentHash.parse(row["content_hash"]),
            media_type=row["media_type"],
            size_bytes=int(row["size_bytes"]),
            data_classification=row["data_classification"],
            trust_level=row["trust_level"],
            stored_at=row["stored_at"],
            storage_path=row["storage_path"],
            payload_deleted=bool(row["payload_deleted"]),
        )
