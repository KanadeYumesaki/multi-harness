"""運用状態のStore。独立Marker、別DB、期限による自動復帰を作らない。"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_canonical
from harness.infrastructure.operation_owner import LinuxOperationOwner
from harness.infrastructure.sqlite.connection_factory import ConnectionFactory
from harness.infrastructure.sqlite.event_ledger_repository import SqliteEventLedgerRepository
from harness.infrastructure.sqlite.transaction_guard import require_transaction
from harness.ports.operation_control import OperationControlTransaction, OperationOwnerPort


class SqliteOperationControlTransaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def artifact_references(self) -> tuple[str, ...]:
        return tuple(
            str(row[0])
            for row in self.connection.execute(
                "SELECT content_hash FROM artifact_manifest WHERE payload_deleted=0 "
                "ORDER BY content_hash"
            )
        )

    def snapshot(self) -> dict[str, Any]:
        """Count and bind the same transaction snapshot; never expose raw journal payloads."""
        require_transaction(self.connection, "operation snapshot")
        mode = self.mode()
        row = self.connection.execute(
            "SELECT version, refusals FROM operation_control WHERE singleton=1"
        ).fetchone()
        # These fixed queries bind identities as well as counts. Equal-sized replacements
        # must invalidate a review. Terminal rows stay in the fingerprint for ABA detection.
        records = {}
        queries = {
            "operation_admission": "SELECT * FROM operation_admission ORDER BY token",
            "operation_journal": "SELECT * FROM operation_journal ORDER BY 1, 2",
            "cli_invocation_journal": "SELECT * FROM cli_invocation_journal ORDER BY invocation_id",
            "approval_grant": "SELECT * FROM approval_grant ORDER BY grant_id",
            "event_ledger": "SELECT * FROM event_ledger ORDER BY stream_id, sequence_number",
            "artifact_manifest": "SELECT * FROM artifact_manifest ORDER BY artifact_id",
        }
        for table, query in queries.items():
            records[table] = [list(item) for item in self.connection.execute(query).fetchall()]
        source_records = {
            key: value for key, value in records.items() if key != "operation_admission"
        }
        records["operation_admission_owner"] = [
            list(row)
            for row in self.connection.execute(
                "SELECT * FROM operation_admission_owner ORDER BY token"
            )
        ]
        records["operation_restore"] = [
            list(row)
            for row in self.connection.execute("SELECT * FROM operation_restore ORDER BY token")
        ]
        return {
            "source_hash": str(
                hash_canonical(source_records, artifact_type="operation-source", schema_major=1)
            ),
            "ledger_valid": all(
                SqliteEventLedgerRepository(self.connection).verify_chain(str(row[0])).valid
                for row in self.connection.execute("SELECT DISTINCT stream_id FROM event_ledger")
            ),
            "mode": mode,
            "version": int(row[0]),
            "refusals": int(row[1]),
            "active_reservations": self.active_count(),
            "unsettled_records": self.unsettled_count(),
            "records_hash": str(
                hash_canonical(records, artifact_type="operation-control-records", schema_major=1)
            ),
        }

    def mode(self) -> str:
        row = self.connection.execute(
            "SELECT mode FROM operation_control WHERE singleton=1"
        ).fetchone()
        if row is None or row[0] not in ("OPEN", "DRAINING", "RESTORING"):
            raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "operation control state missing")
        return str(row[0])

    def set_mode(self, mode: str) -> None:
        self.connection.execute(
            "UPDATE operation_control SET mode=?, version=version+1 WHERE singleton=1", (mode,)
        )

    def active_count(self) -> int:
        return int(
            self.connection.execute("SELECT COUNT(*) FROM operation_admission").fetchone()[0]
        )

    def unsettled_count(self) -> int:
        # Process消失後もJournal/承認待ちを無視して復元しない。
        count = 0
        for query in (
            "SELECT COUNT(*) FROM operation_journal WHERE state != 'RECEIPT_DURABLE'",
            "SELECT COUNT(*) FROM cli_invocation_journal WHERE state != 'RESPONSE_CAPTURED'",
            "SELECT COUNT(*) FROM approval_grant WHERE status='ISSUED'",
        ):
            count += int(self.connection.execute(query).fetchone()[0])
        return count

    def reserve(
        self, token: str, kind: str, *, request_id: str = "", owner_scope: str = ""
    ) -> None:
        self.connection.execute(
            "INSERT INTO operation_admission(token,kind) VALUES (?,?)", (token, kind)
        )

        if owner_scope:
            self.connection.execute(
                "INSERT INTO operation_admission_owner VALUES (?,?,?)",
                (token, request_id, owner_scope),
            )
        self._advance_version()

    def owners(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            dict(row)
            for row in self.connection.execute(
                "SELECT a.token, a.kind, o.request_id, o.owner_scope FROM operation_admission a "
                "LEFT JOIN operation_admission_owner o ON o.token=a.token ORDER BY a.token"
            )
        )

    def start_restore(self, token: str, owner_scope: str, purpose: str) -> None:
        source_hash = self.snapshot()["source_hash"]
        self.connection.execute(
            "INSERT INTO operation_restore VALUES (?,?,?,?, 'ACTIVE')",
            (token, owner_scope, purpose, source_hash),
        )

    def finish_restore(self, token: str, status: str) -> None:
        updated = self.connection.execute(
            "UPDATE operation_restore SET status=? WHERE token=? AND status IN ('ACTIVE','FAILED')",
            (status, token),
        )
        if updated.rowcount != 1:
            raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "restore ownership record changed")

    def restore_record(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM operation_restore WHERE status IN ('ACTIVE','FAILED')"
        ).fetchone()
        return None if row is None else dict(row)

    def release(self, token: str) -> None:
        result = self.connection.execute("DELETE FROM operation_admission WHERE token=?", (token,))
        if result.rowcount != 1:
            raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "admission reservation lost")

        self._advance_version()

    def _advance_version(self) -> None:
        self.connection.execute("UPDATE operation_control SET version=version+1 WHERE singleton=1")

    def record_refusal(self) -> None:
        self.connection.execute(
            "UPDATE operation_control SET refusals=refusals+1 WHERE singleton=1"
        )

    def refusals(self) -> int:
        return int(
            self.connection.execute(
                "SELECT refusals FROM operation_control WHERE singleton=1"
            ).fetchone()[0]
        )


class SqliteOperationControl:
    def __init__(self, database: Path) -> None:
        self._database = database

    def identity(self) -> str:
        return str(self._database) + "#operation_control"

    def ownership(self) -> OperationOwnerPort:
        return LinuxOperationOwner(self._database)

    def new_token(self) -> str:
        return str(uuid.uuid4())

    @contextmanager
    def transaction(self) -> Iterator[OperationControlTransaction]:
        if not self._database.is_file():
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH, "operation control DB is not initialized"
            )
        connection = ConnectionFactory(self._database).connect()
        try:
            with ConnectionFactory(self._database).begin_immediate(connection):
                state = SqliteOperationControlTransaction(connection)
                state.mode()  # Migration欠落をFail-Closedで検出する。
                yield state
        except sqlite3.Error as exc:
            raise HarnessError(
                ErrorCode.STORAGE_WRITE_FAILED, "operation control storage unavailable"
            ) from exc
        finally:
            connection.close()
