"""Run projection; immutable versions and events reside in CAS and the ledger."""

from __future__ import annotations

import sqlite3

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash
from harness.ports.local_workflow import WorkflowRecord


class SqliteWorkflowRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get(self, run_id: str) -> WorkflowRecord | None:
        row = self._connection.execute(
            "SELECT run_id,state,version,document_hash FROM local_workflow_run WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return WorkflowRecord(
            str(row["run_id"]),
            str(row["state"]),
            int(row["version"]),
            ContentHash.parse(str(row["document_hash"])),
        )

    def save(self, record: WorkflowRecord, *, expected_version: int) -> None:
        if not self._connection.in_transaction or record.version != expected_version + 1:
            raise HarnessError(ErrorCode.EVENT_ORDER_VIOLATION, "run update requires ordered UoW")
        try:
            if expected_version == 0:
                self._connection.execute(
                    "INSERT INTO local_workflow_run(run_id,state,version,document_hash) "
                    "VALUES(?,?,?,?)",
                    (record.run_id, record.state, record.version, str(record.document_hash)),
                )
            else:
                cursor = self._connection.execute(
                    "UPDATE local_workflow_run SET state=?,version=?,document_hash=? "
                    "WHERE run_id=? AND version=?",
                    (
                        record.state,
                        record.version,
                        str(record.document_hash),
                        record.run_id,
                        expected_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise HarnessError(
                        ErrorCode.EVENT_ORDER_VIOLATION, "run was concurrently changed"
                    )
        except sqlite3.IntegrityError as exc:
            raise HarnessError(ErrorCode.EVENT_ORDER_VIOLATION, "run already exists") from exc
