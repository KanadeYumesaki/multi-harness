"""SQLite Action Attempt Repository。"""

from __future__ import annotations

import json
import sqlite3

from harness.domain.attempt import ActionAttempt
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash
from harness.domain.timestamps import canonical_timestamp
from harness.infrastructure.sqlite.transaction_guard import require_transaction

__all__ = ["SqliteActionAttemptRepository"]


class SqliteActionAttemptRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def transaction_identity(self) -> object:
        return self._connection

    def create(self, attempt: ActionAttempt) -> None:
        require_transaction(self._connection, "action attempt create")
        try:
            self._connection.execute(
                """
                INSERT INTO action_attempt (
                    attempt_id, action_id, attempt_number, state, plan_content_hash,
                    execution_plan_hash, worker_id, claim_id, lease_id, fencing_token,
                    runtime_attestation_hash, operation_journal_id, started_at, ended_at,
                    receipt_ids, error_classification, store_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _values(attempt),
            )
        except sqlite3.IntegrityError as exc:
            raise HarnessError(
                ErrorCode.EVENT_ORDER_VIOLATION, "attempt identity already exists"
            ) from exc

    def get(self, attempt_id: str) -> ActionAttempt | None:
        row = self._connection.execute(
            "SELECT * FROM action_attempt WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return None if row is None else _attempt(row)

    def update(self, attempt: ActionAttempt, *, expected_store_version: int) -> None:
        require_transaction(self._connection, "action attempt update")
        if attempt.store_version != expected_store_version + 1:
            raise HarnessError(
                ErrorCode.EVENT_ORDER_VIOLATION, "attempt store version did not advance by one"
            )
        cursor = self._connection.execute(
            """
            UPDATE action_attempt SET
                state = ?, plan_content_hash = ?, execution_plan_hash = ?, worker_id = ?,
                claim_id = ?, lease_id = ?, fencing_token = ?, runtime_attestation_hash = ?,
                operation_journal_id = ?, started_at = ?, ended_at = ?, receipt_ids = ?,
                error_classification = ?, store_version = ?
            WHERE attempt_id = ? AND store_version = ?
            """,
            (
                attempt.state,
                _nullable_hash(attempt.plan_content_hash),
                _nullable_hash(attempt.execution_plan_hash),
                attempt.worker_id,
                attempt.claim_id,
                attempt.lease_id,
                attempt.fencing_token,
                _nullable_hash(attempt.runtime_attestation_hash),
                attempt.operation_journal_id,
                _nullable_time(attempt.started_at),
                _nullable_time(attempt.ended_at),
                _dump(list(attempt.receipt_ids)),
                attempt.error_classification,
                attempt.store_version,
                attempt.attempt_id,
                expected_store_version,
            ),
        )
        if cursor.rowcount != 1:
            raise HarnessError(
                ErrorCode.EVENT_ORDER_VIOLATION, "action attempt CAS was not acquired"
            )


def _values(attempt: ActionAttempt) -> tuple[object, ...]:
    return (
        attempt.attempt_id,
        attempt.action_id,
        attempt.attempt_number,
        attempt.state,
        _nullable_hash(attempt.plan_content_hash),
        _nullable_hash(attempt.execution_plan_hash),
        attempt.worker_id,
        attempt.claim_id,
        attempt.lease_id,
        attempt.fencing_token,
        _nullable_hash(attempt.runtime_attestation_hash),
        attempt.operation_journal_id,
        _nullable_time(attempt.started_at),
        _nullable_time(attempt.ended_at),
        _dump(list(attempt.receipt_ids)),
        attempt.error_classification,
        attempt.store_version,
    )


def _attempt(row: sqlite3.Row) -> ActionAttempt:
    receipts = json.loads(str(row["receipt_ids"]))
    if not isinstance(receipts, list) or not all(isinstance(item, str) for item in receipts):
        raise HarnessError(ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "stored receipt IDs are invalid")
    return ActionAttempt(
        attempt_id=str(row["attempt_id"]),
        action_id=str(row["action_id"]),
        attempt_number=int(row["attempt_number"]),
        state=str(row["state"]),
        plan_content_hash=_nullable_content_hash(row["plan_content_hash"]),
        execution_plan_hash=_nullable_content_hash(row["execution_plan_hash"]),
        worker_id=_nullable_string(row["worker_id"]),
        claim_id=_nullable_string(row["claim_id"]),
        lease_id=_nullable_string(row["lease_id"]),
        fencing_token=None if row["fencing_token"] is None else int(row["fencing_token"]),
        runtime_attestation_hash=_nullable_content_hash(row["runtime_attestation_hash"]),
        operation_journal_id=_nullable_string(row["operation_journal_id"]),
        started_at=_nullable_datetime(row["started_at"]),
        ended_at=_nullable_datetime(row["ended_at"]),
        receipt_ids=tuple(receipts),
        error_classification=_nullable_string(row["error_classification"]),
        store_version=int(row["store_version"]),
    )


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _nullable_hash(value: ContentHash | None) -> str | None:
    return None if value is None else str(value)


def _nullable_time(value: str | None) -> str | None:
    return None if value is None else canonical_timestamp(value)


def _nullable_content_hash(value: object) -> ContentHash | None:
    return None if value is None else ContentHash.parse(str(value))


def _nullable_datetime(value: object) -> str | None:
    return None if value is None else canonical_timestamp(str(value))


def _nullable_string(value: object) -> str | None:
    return None if value is None else str(value)
