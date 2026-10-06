"""SQLite Operation Journal Repository。"""

from __future__ import annotations

import sqlite3

from harness.domain.effect import EffectState, OperationJournal
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash
from harness.domain.timestamps import canonical_timestamp
from harness.infrastructure.sqlite.transaction_guard import require_transaction

__all__ = ["SqliteEffectJournalRepository"]


class SqliteEffectJournalRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def transaction_identity(self) -> object:
        return self._connection

    def create_prepared(self, journal: OperationJournal) -> None:
        require_transaction(self._connection, "operation journal prepare")
        if journal.state is not EffectState.PREPARED_DURABLE:
            raise HarnessError(
                ErrorCode.EFFECT_UNKNOWN, "journal must be PREPARED_DURABLE at creation"
            )
        try:
            self._connection.execute(
                """
                INSERT INTO operation_journal (
                    operation_journal_id, operation_id, effect_id, run_id, action_id, attempt_id,
                    operation_type, target_resource_identity, fencing_token, before_hash,
                    expected_after_hash, prepared_event_id, prepared_at, durability_level, state,
                    execution_attempted_event_id, execution_attempted_at, observed_hash,
                    observation_method, observed_at, receipt_id, error_classification, store_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _values(journal),
            )
            self._append_history(journal, journal.prepared_at)
        except sqlite3.IntegrityError as exc:
            raise HarnessError(
                ErrorCode.UNRECONCILED_EFFECT_PRESENT, "operation or effect already exists"
            ) from exc

    def get_by_effect_id(self, effect_id: str) -> OperationJournal | None:
        row = self._connection.execute(
            "SELECT * FROM operation_journal WHERE effect_id = ?", (effect_id,)
        ).fetchone()
        return None if row is None else _journal(row)

    def update(self, journal: OperationJournal, *, expected_store_version: int) -> None:
        require_transaction(self._connection, "operation journal update")
        if journal.store_version != expected_store_version + 1:
            raise HarnessError(
                ErrorCode.STALE_FENCING_TOKEN, "journal store version did not advance by one"
            )
        cursor = self._connection.execute(
            """
            UPDATE operation_journal SET
                state = ?, execution_attempted_event_id = ?, execution_attempted_at = ?,
                observed_hash = ?, observation_method = ?, observed_at = ?, receipt_id = ?,
                error_classification = ?, store_version = ?
            WHERE operation_journal_id = ? AND store_version = ? AND fencing_token = ?
            """,
            (
                journal.state.value,
                journal.execution_attempted_event_id,
                _nullable_time(journal.execution_attempted_at),
                _nullable_hash(journal.observed_hash),
                journal.observation_method,
                _nullable_time(journal.observed_at),
                journal.receipt_id,
                journal.error_classification,
                journal.store_version,
                journal.operation_journal_id,
                expected_store_version,
                journal.fencing_token,
            ),
        )
        if cursor.rowcount != 1:
            raise HarnessError(
                ErrorCode.STALE_FENCING_TOKEN, "operation journal CAS was not acquired"
            )
        self._append_history(journal, _recorded_at(journal))

    def _append_history(self, journal: OperationJournal, recorded_at: str) -> None:
        self._connection.execute(
            """
            INSERT INTO operation_journal_history (
                operation_journal_id, store_version, state, journal_hash, recorded_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                journal.operation_journal_id,
                journal.store_version,
                journal.state.value,
                str(journal.journal_hash),
                _time(recorded_at),
            ),
        )


def _values(journal: OperationJournal) -> tuple[object, ...]:
    return (
        journal.operation_journal_id,
        journal.operation_id,
        journal.effect_id,
        journal.run_id,
        journal.action_id,
        journal.attempt_id,
        journal.operation_type,
        journal.target_resource_identity,
        journal.fencing_token,
        str(journal.before_hash),
        str(journal.expected_after_hash),
        journal.prepared_event_id,
        _time(journal.prepared_at),
        journal.durability_level,
        journal.state.value,
        journal.execution_attempted_event_id,
        _nullable_time(journal.execution_attempted_at),
        _nullable_hash(journal.observed_hash),
        journal.observation_method,
        _nullable_time(journal.observed_at),
        journal.receipt_id,
        journal.error_classification,
        journal.store_version,
    )


def _journal(row: sqlite3.Row) -> OperationJournal:
    return OperationJournal(
        operation_journal_id=str(row["operation_journal_id"]),
        operation_id=str(row["operation_id"]),
        effect_id=str(row["effect_id"]),
        run_id=str(row["run_id"]),
        action_id=str(row["action_id"]),
        attempt_id=str(row["attempt_id"]),
        operation_type=str(row["operation_type"]),
        target_resource_identity=str(row["target_resource_identity"]),
        fencing_token=int(row["fencing_token"]),
        before_hash=ContentHash.parse(str(row["before_hash"])),
        expected_after_hash=ContentHash.parse(str(row["expected_after_hash"])),
        prepared_event_id=str(row["prepared_event_id"]),
        prepared_at=_parse_time(str(row["prepared_at"])),
        durability_level=str(row["durability_level"]),
        state=EffectState(str(row["state"])),
        execution_attempted_event_id=_nullable_string(row["execution_attempted_event_id"]),
        execution_attempted_at=_nullable_time_value(row["execution_attempted_at"]),
        observed_hash=_nullable_content_hash(row["observed_hash"]),
        observation_method=_nullable_string(row["observation_method"]),
        observed_at=_nullable_time_value(row["observed_at"]),
        receipt_id=_nullable_string(row["receipt_id"]),
        error_classification=_nullable_string(row["error_classification"]),
        store_version=int(row["store_version"]),
    )


def _recorded_at(journal: OperationJournal) -> str:
    if journal.observed_at is not None:
        return journal.observed_at
    if journal.execution_attempted_at is not None:
        return journal.execution_attempted_at
    return journal.prepared_at


def _time(value: str) -> str:
    return canonical_timestamp(value)


def _nullable_time(value: str | None) -> str | None:
    return None if value is None else _time(value)


def _nullable_hash(value: ContentHash | None) -> str | None:
    return None if value is None else str(value)


def _parse_time(value: str) -> str:
    return canonical_timestamp(value)


def _nullable_time_value(value: object) -> str | None:
    return None if value is None else _parse_time(str(value))


def _nullable_content_hash(value: object) -> ContentHash | None:
    return None if value is None else ContentHash.parse(str(value))


def _nullable_string(value: object) -> str | None:
    return None if value is None else str(value)
