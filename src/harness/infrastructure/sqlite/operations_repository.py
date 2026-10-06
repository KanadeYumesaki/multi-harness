"""Drain観測とRestore先の具象。**実SQLiteを数える。**

## 独立Probeの 0 と、実DBの 0 を分ける

`DrainSnapshot` が返す 0 は「この接続で数えて 0 件だった」を意味する。
数えずに 0 を返す実装を作らない。内訳（`active_states` / `pending_statuses`）を
必ず添えるのは、どのstateを進行中と数えたのかを Evidence から読めるようにするためである。

## 進行中と数える state

`operation_journal.state` のうち、**終端でないもの**を進行中とする。

| state | 扱い | 理由 |
|---|---|---|
| `PREPARED_DURABLE` | 進行中 | Journal確定済みでこれから作用を起こす |
| `EXECUTION_ATTEMPTED` | 進行中 | 起こしたが結果未観測 |
| `EFFECT_UNKNOWN` | 進行中 | 作用したか判定できない。**人の復帰待ち** |
| `EFFECT_CONFLICT` | 進行中 | 競合。未解決 |
| `EFFECT_OBSERVED` | 進行中 | Receipt未確定。Recoveryでの補完が必要 |
| `RECEIPT_DURABLE` | 終端 | Receipt確定済み |

`EFFECT_UNKNOWN` を終端に含めない。判定できない作用を残したまま入れ替えると、
どのVersionの規則で動いていたのか永久に確定しない。

Workbench の送信は `operation_journal` ではなく `cli_invocation_journal` に残る。
claim 後・応答捕捉前（`PREPARED_DURABLE` / `EXECUTION_ATTEMPTED` / `EFFECT_UNKNOWN`）は
Process を失っても `resume_send` や照合を待つ進行中である。ここを数えないと、
復元開始は未決として拒否するのに Drain だけが ACCEPTED を返す食い違いになる。
内訳では `cli_invocation_journal:<state>` と表の名前を付け、どの表を数えたかを残す。

承認待ちは `approval_grant.status = 'ISSUED'`。人がまだ使っても失効させても
いない状態であり、新Versionが引き継げる保証が無い。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from harness.domain.artifact import ArtifactMetadata
from harness.domain.hashing import ContentHash, hash_bytes
from harness.infrastructure.artifact.filesystem_cas import FilesystemArtifactCas
from harness.infrastructure.sqlite.artifact_manifest_repository import (
    SqliteArtifactManifestRepository,
)
from harness.infrastructure.sqlite.connection_factory import ConnectionFactory
from harness.infrastructure.sqlite.event_ledger_repository import SqliteEventLedgerRepository
from harness.ports.operations import BackupArtifact, DrainSnapshot

__all__ = [
    "ACTIVE_CLI_INVOCATION_STATES",
    "ACTIVE_JOURNAL_STATES",
    "CLI_INVOCATION_STATE_PREFIX",
    "IntakeStillOpenError",
    "RestoreVerificationError",
    "SqliteDrainInspection",
    "SqliteRestoreTarget",
    "create_backup",
]

#: 終端でない `operation_journal.state`。ここを狭めると未完了を見落とす。
ACTIVE_JOURNAL_STATES: tuple[str, ...] = (
    "PREPARED_DURABLE",
    "EXECUTION_ATTEMPTED",
    "EFFECT_UNKNOWN",
    "EFFECT_CONFLICT",
    "EFFECT_OBSERVED",
)

#: 終端でない `cli_invocation_journal.state`。終端は応答を捕捉した `RESPONSE_CAPTURED` だけ。
ACTIVE_CLI_INVOCATION_STATES: tuple[str, ...] = (
    "PREPARED_DURABLE",
    "EXECUTION_ATTEMPTED",
    "EFFECT_UNKNOWN",
)

#: 内訳で `operation_journal` の state と区別するための接頭辞。
CLI_INVOCATION_STATE_PREFIX = "cli_invocation_journal:"

#: 承認待ちとみなす `approval_grant.status`。
PENDING_APPROVAL_STATUSES: tuple[str, ...] = ("ISSUED",)


class IntakeStillOpenError(RuntimeError):
    """新規受付を止めずにDrainを主張しようとした。"""


class RestoreVerificationError(RuntimeError):
    """復元先を読み直せない。**読めないものを一致と書かない。**"""


class SqliteDrainInspection:
    """実SQLiteを数える `DrainInspectionPort` の具象。"""

    def __init__(self, database: Path) -> None:
        self._database = database
        self._intake_stopped = False

    def stop_intake(self) -> None:
        self._intake_stopped = True

    def intake_stopped(self) -> bool:
        return self._intake_stopped

    def inspect(self) -> DrainSnapshot:
        connection = ConnectionFactory(self._database).connect()
        try:
            active = self._counts(
                connection,
                "SELECT state, COUNT(*) FROM operation_journal GROUP BY state ORDER BY state",
                ACTIVE_JOURNAL_STATES,
            ) + tuple(
                (CLI_INVOCATION_STATE_PREFIX + state, count)
                for state, count in self._counts(
                    connection,
                    "SELECT state, COUNT(*) FROM cli_invocation_journal"
                    " GROUP BY state ORDER BY state",
                    ACTIVE_CLI_INVOCATION_STATES,
                )
            )
            pending = self._counts(
                connection,
                "SELECT status, COUNT(*) FROM approval_grant GROUP BY status ORDER BY status",
                PENDING_APPROVAL_STATUSES,
            )
            observed_at = str(
                next(iter(connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')")))[0]
            )
        finally:
            connection.close()
        return DrainSnapshot(
            active_run_count=sum(count for _state, count in active),
            pending_approval_count=sum(count for _status, count in pending),
            active_states=active,
            pending_statuses=pending,
            observed_at=observed_at,
            source=str(self._database),
        )

    @staticmethod
    def _counts(
        connection: sqlite3.Connection, query: str, wanted: tuple[str, ...]
    ) -> tuple[tuple[str, int], ...]:
        """該当stateだけを拾う。**存在しない行を 0 として作らない。**"""
        rows = [(str(row[0]), int(row[1])) for row in connection.execute(query)]
        return tuple((name, count) for name, count in rows if name in wanted)


def create_backup(
    *,
    source_database: Path,
    source_cas: Path,
    destination_database: Path,
    destination_cas: Path,
    backup_id: str,
    created_at: str,
) -> BackupArtifact:
    """SQLite Backup API と CAS 複製で Backup を作る。

    **合成環境専用の入出力しか受けない前提で呼ぶこと。** ユーザーDBを開く判断は
    呼出し側（Operator入口）が負う。
    """
    source = ConnectionFactory(source_database).connect()
    try:
        heads, artifact_ids = _read_bindings(source, source_cas)
        destination = ConnectionFactory(destination_database).connect()
        try:
            source.backup(destination, pages=128)
        finally:
            destination.close()
    finally:
        source.close()

    destination_cas.mkdir(parents=True, exist_ok=True)
    origin = FilesystemArtifactCas(source_cas)
    copy = FilesystemArtifactCas(destination_cas)
    for content_hash in _cas_hashes(source_database):
        copy.write_bytes(
            origin.read_bytes(content_hash), _metadata_for(source_database, content_hash)
        )

    return BackupArtifact(
        backup_id=backup_id,
        database_path=str(destination_database),
        cas_root=str(destination_cas),
        database_sha256=str(hash_bytes(destination_database.read_bytes())),
        created_at=created_at,
        source_chain_heads=heads,
        artifact_ids=artifact_ids,
    )


def _metadata_for(database: Path, content_hash: ContentHash) -> ArtifactMetadata:
    connection = ConnectionFactory(database).connect()
    try:
        record = SqliteArtifactManifestRepository(connection).find_by_content_hash(content_hash)
        if record is None:
            raise RestoreVerificationError("BACKUP_ARTIFACT_METADATA_MISSING")
        return ArtifactMetadata(
            media_type=record.media_type,
            size_bytes=record.size_bytes,
            data_classification=record.data_classification,
            trust_level=record.trust_level,
        )
    finally:
        connection.close()


def _cas_hashes(database: Path) -> tuple[ContentHash, ...]:
    connection = ConnectionFactory(database).connect()
    try:
        return tuple(SqliteArtifactManifestRepository(connection).all_content_hashes())
    finally:
        connection.close()


def _read_bindings(
    connection: sqlite3.Connection, cas_root: Path
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    """Chain Head と Artifact 参照集合を読む。**Chainは端から検証する。**"""
    ledger = SqliteEventLedgerRepository(connection)
    heads: list[tuple[str, str]] = []
    streams = [
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT stream_id FROM event_ledger ORDER BY stream_id"
        )
    ]
    for stream in streams:
        verified = ledger.verify_chain(stream)
        if not verified.valid:
            raise RestoreVerificationError(f"LEDGER_CHAIN_INVALID: {stream}")
        entries = ledger.load_stream(stream)
        heads.append((stream, str(entries[-1].event_hash)))
    manifests = SqliteArtifactManifestRepository(connection)
    artifact_ids: list[str] = []
    for content_hash in manifests.all_content_hashes():
        record = manifests.find_by_content_hash(content_hash)
        if record is None:
            raise RestoreVerificationError("ARTIFACT_MANIFEST_ROW_MISSING")
        artifact_ids.append(record.artifact_id)
        if not (cas_root / "objects").exists():
            raise RestoreVerificationError("CAS_ROOT_MISSING")
    return tuple(heads), tuple(sorted(artifact_ids))


class SqliteRestoreTarget:
    """復元先。復元後の Chain と Manifest を**読み直す**。"""

    def __init__(self, *, database: Path, cas_root: Path) -> None:
        self._database = database
        self._cas_root = cas_root
        self._restored = False

    def restore(
        self, backup: BackupArtifact, *, checkpoint: Callable[[], None] | None = None
    ) -> None:
        """Backup DB と CAS を復元先へ写す。"""
        source_db = Path(backup.database_path)
        source_cas = Path(backup.cas_root)
        if not source_db.is_file() or not source_cas.is_dir():
            raise RestoreVerificationError("BACKUP_INPUT_MISSING")
        if str(hash_bytes(source_db.read_bytes())) != backup.database_sha256:
            # Backup 自体が改変されている。復元して一致を主張しない。
            raise RestoreVerificationError("BACKUP_BYTES_TAMPERED")

        if self._database.exists() or self._cas_root.exists():
            raise RestoreVerificationError("RESTORE_TARGET_MUST_BE_NEW")
        self._database.parent.mkdir(parents=True, exist_ok=True)
        source = ConnectionFactory(source_db).connect()
        try:
            destination = ConnectionFactory(self._database).connect()
            try:
                source.backup(destination, pages=128)
            finally:
                destination.close()
        finally:
            source.close()

        if checkpoint is not None:
            checkpoint()
        self._cas_root.mkdir(parents=True, exist_ok=True)
        origin = FilesystemArtifactCas(source_cas)
        copy = FilesystemArtifactCas(self._cas_root)
        for content_hash in _cas_hashes(source_db):
            copy.write_bytes(
                origin.read_bytes(content_hash), _metadata_for(source_db, content_hash)
            )
        self._restored = True

    def _require_restored(self) -> None:
        if not self._restored:
            raise RestoreVerificationError("RESTORE_NOT_PERFORMED")

    def source_chain_heads(self) -> tuple[tuple[str, str], ...]:
        """復元前の元DBから Chain Head を読む。Backup の比較基準に使う。"""
        connection = ConnectionFactory(self._database).connect()
        try:
            heads, _ids = _read_bindings(connection, self._cas_root)
            return heads
        finally:
            connection.close()

    def source_artifact_ids(self) -> tuple[str, ...]:
        """復元前の元DBから Artifact 参照集合を読む。"""
        connection = ConnectionFactory(self._database).connect()
        try:
            _heads, ids = _read_bindings(connection, self._cas_root)
            return ids
        finally:
            connection.close()

    def chain_heads(self) -> tuple[tuple[str, str], ...]:
        self._require_restored()
        connection = ConnectionFactory(self._database).connect()
        try:
            integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
            if integrity != ["ok"]:
                raise RestoreVerificationError("RESTORE_INTEGRITY_FAILED")
            heads, _ids = _read_bindings(connection, self._cas_root)
            return heads
        finally:
            connection.close()

    def artifact_ids(self) -> tuple[str, ...]:
        self._require_restored()
        connection = ConnectionFactory(self._database).connect()
        try:
            _heads, ids = _read_bindings(connection, self._cas_root)
            return ids
        finally:
            connection.close()
