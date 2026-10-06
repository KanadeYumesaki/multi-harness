"""Approval admissions and append-only result references in the shared SQLite DB."""

from __future__ import annotations

import sqlite3

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash
from harness.infrastructure.sqlite.transaction_guard import require_transaction
from harness.ports.approval_consume import ConsumeTicket


class SqliteConsumeRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def register(self, ticket: ConsumeTicket) -> None:
        require_transaction(self.connection, "approval race admission")
        row = self.connection.execute(
            "SELECT concurrency_group FROM approval_consume_group WHERE grant_id = ?",
            (ticket.grant_id,),
        ).fetchone()
        if row is None:
            self.connection.execute(
                "INSERT INTO approval_consume_group VALUES (?, ?)",
                (ticket.grant_id, ticket.concurrency_group),
            )
        elif row[0] != ticket.concurrency_group:
            raise HarnessError(ErrorCode.APPROVAL_REPLAY, "grant already belongs to another group")
        try:
            self.connection.execute(
                "INSERT INTO approval_consume_ticket VALUES (?, ?, ?, ?, ?, ?)",
                (
                    ticket.ticket_id,
                    ticket.grant_id,
                    ticket.attempt_id,
                    ticket.concurrency_group,
                    str(ticket.request_hash),
                    ticket.state,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HarnessError(
                ErrorCode.APPROVAL_REPLAY, "attempt admission already exists"
            ) from exc

    def get(self, ticket_id: str) -> ConsumeTicket | None:
        row = self.connection.execute(
            "SELECT * FROM approval_consume_ticket WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        return None if row is None else self._ticket(row)

    def winner(self, concurrency_group: str) -> ConsumeTicket | None:
        rows = self.connection.execute(
            "SELECT * FROM approval_consume_ticket "
            "WHERE concurrency_group = ? AND state = 'CONSUMED'",
            (concurrency_group,),
        ).fetchall()
        if len(rows) > 1:
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT, "multiple approval winners detected"
            )
        return self._ticket(rows[0]) if rows else None

    def finish(self, ticket_id: str, state: str) -> None:
        require_transaction(self.connection, "approval admission finish")
        cursor = self.connection.execute(
            "UPDATE approval_consume_ticket SET state = ? "
            "WHERE ticket_id = ? AND state = 'ADMITTED'",
            (state, ticket_id),
        )
        if cursor.rowcount != 1:
            raise HarnessError(ErrorCode.APPROVAL_REPLAY, "admission already used")

    def put_result(self, result_id: str, ticket_id: str, digest: ContentHash) -> None:
        require_transaction(self.connection, "approval consume result append")
        self.connection.execute(
            "INSERT INTO approval_consume_result VALUES (?, ?, ?)",
            (result_id, ticket_id, str(digest)),
        )

    def result_hash(self, result_id: str) -> ContentHash | None:
        row = self.connection.execute(
            "SELECT content_hash FROM approval_consume_result WHERE consume_result_id = ?",
            (result_id,),
        ).fetchone()
        return None if row is None else ContentHash.parse(row[0])

    @staticmethod
    def _ticket(row: sqlite3.Row) -> ConsumeTicket:
        return ConsumeTicket(
            row["ticket_id"],
            row["grant_id"],
            row["attempt_id"],
            row["concurrency_group"],
            ContentHash.parse(row["request_hash"]),
            row["state"],
        )
