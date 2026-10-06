"""Native SQLite/CAS restore probe on newly created synthetic data only.

This is a component measurement, not a Release-area PASS: application Drain,
Effect observation and operational restore authorization are separate requirements.
"""

from __future__ import annotations

import time
from pathlib import Path

from emit_case_evidence import EvidenceEmissionError
from harness.domain.artifact import ArtifactMetadata
from harness.domain.hashing import hash_bytes
from harness.infrastructure.artifact.filesystem_cas import FilesystemArtifactCas
from harness.infrastructure.artifact.store import ArtifactStore
from harness.infrastructure.sqlite.artifact_manifest_repository import (
    SqliteArtifactManifestRepository,
)
from harness.infrastructure.sqlite.connection_factory import ConnectionFactory
from harness.infrastructure.sqlite.event_ledger_repository import SqliteEventLedgerRepository
from harness.infrastructure.sqlite.migrations import migrate
from harness.ports.event_ledger import NewEvent


def inspect_restored(database: Path, cas_root: Path) -> dict:
    """Recompute chain hashes and read every referenced CAS object, not just heads."""
    if not database.is_file() or not cas_root.is_dir():
        raise EvidenceEmissionError("RESTORE_INPUT_MISSING")
    connection = ConnectionFactory(database).connect()
    try:
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise EvidenceEmissionError("RESTORE_INTEGRITY_FAILED")
        ledger = SqliteEventLedgerRepository(connection)
        streams = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT stream_id FROM event_ledger ORDER BY stream_id"
            )
        ]
        heads = []
        for stream in streams:
            verified = ledger.verify_chain(stream)
            if not verified.valid:
                raise EvidenceEmissionError("RESTORE_LEDGER_CHAIN_INVALID")
            entries = ledger.load_stream(stream)
            heads.append(
                {
                    "stream_id": stream,
                    "count": verified.entry_count,
                    "head": str(entries[-1].event_hash),
                }
            )
        manifests = SqliteArtifactManifestRepository(connection)
        store = ArtifactStore(FilesystemArtifactCas(cas_root), manifests)
        artifacts = []
        for content_hash in manifests.all_content_hashes():
            record = manifests.find_by_content_hash(content_hash)
            if record is None or record.payload_deleted:
                raise EvidenceEmissionError("RESTORE_PROBE_REQUIRES_LIVE_PAYLOAD")
            payload = store.get(content_hash)
            if len(payload) != record.size_bytes:
                raise EvidenceEmissionError("RESTORE_ARTIFACT_SIZE_MISMATCH")
            artifacts.append(
                {
                    "id": record.artifact_id,
                    "hash": str(hash_bytes(payload)),
                    "size_bytes": len(payload),
                }
            )
        # Compare complete metadata as well as the reference set.
        rows = [
            list(row)
            for row in connection.execute("SELECT * FROM artifact_manifest ORDER BY artifact_id")
        ]
        return {
            "integrity_check": integrity,
            "streams": heads,
            "artifacts": artifacts,
            "manifest_rows": rows,
        }
    finally:
        connection.close()


def measure_backup_restore(root: Path, recorded_at: str) -> dict:
    """Never opens a user's database; both databases are new probe fixtures."""
    root.mkdir(exist_ok=False)
    source = root / "source.sqlite3"
    restored = root / "restored.sqlite3"
    source_cas = FilesystemArtifactCas(root / "source-cas")
    restored_cas = FilesystemArtifactCas(root / "restored-cas")
    factory = ConnectionFactory(source)
    migrate(factory, recorded_at=recorded_at)
    connection = factory.connect()
    try:
        store = ArtifactStore(source_cas, SqliteArtifactManifestRepository(connection))
        ledger = SqliteEventLedgerRepository(connection)
        payloads = (b"synthetic backup payload alpha\n", b"synthetic backup payload beta\n")
        with factory.begin_immediate(connection):
            for index, payload in enumerate(payloads):
                metadata = ArtifactMetadata(
                    media_type="text/plain",
                    size_bytes=len(payload),
                    data_classification="INTERNAL",
                    trust_level="VERIFIED_INTERNAL",
                )
                store.put(payload, metadata, artifact_id=f"probe-{index}", stored_at=recorded_at)
                ledger.append(
                    [
                        NewEvent(
                            stream_id=f"probe-{index}",
                            event_type=event_type,
                            payload_hash=hash_bytes(payload),
                            recorded_at=recorded_at,
                        )
                        for event_type in ("RUN_CREATED", "INTENT_CREATED", "PLAN_RESOLVED")
                    ],
                    expected_stream_sequence=0,
                )
        before = inspect_restored(source, source_cas.root)
        destination = ConnectionFactory(restored).connect()
        try:
            deadline = time.monotonic() + 30

            def progress(status: int, remaining: int, total: int) -> None:
                if time.monotonic() > deadline:
                    raise EvidenceEmissionError("RESTORE_BACKUP_TIMEOUT")

            connection.backup(destination, pages=128, progress=progress, sleep=0.01)
        finally:
            destination.close()
        # Copy only synthetic, verified objects using the production durable CAS writer.
        for payload in payloads:
            metadata = ArtifactMetadata(
                media_type="text/plain",
                size_bytes=len(payload),
                data_classification="INTERNAL",
                trust_level="VERIFIED_INTERNAL",
            )
            restored_cas.write_bytes(source_cas.read_bytes(hash_bytes(payload)), metadata)
        after = inspect_restored(restored, restored_cas.root)
        unchanged = inspect_restored(source, source_cas.root)
        if before != after or before != unchanged:
            raise EvidenceEmissionError("RESTORE_SNAPSHOT_MISMATCH")
        return {
            "measurement": "NATIVE_SQLITE_CAS_ROUNDTRIP",
            "matched": True,
            "source_unchanged": True,
            "observed": after,
            "release_area_status": "UNVERIFIED",
            "unmeasured": [
                "application_drain",
                "effects_during_restore",
                "operator_restore_workflow",
            ],
        }
    finally:
        connection.close()
