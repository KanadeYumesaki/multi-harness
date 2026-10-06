"""Workbench Session と CLI Invocation Journal の永続化。

## 型注釈を保存データの検査にしない

読み戻すたびに **Record Hash を再計算して照合する**。State 名も Enum へ厳格に
戻す。DB の行を直接書き換えられた場合、型が合っていても Hash が合わない。

## 同じ invocation を二度作らせない

`invocation_id` は PRIMARY KEY、`session_id` は UNIQUE。二重起動の勝者が 1 つ
しか生まれないことを、Application の判断ではなく **制約** で決める。
"""

from __future__ import annotations

import sqlite3

from harness.domain.cli_invocation import CliInvocationJournal, InvocationState
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash, hash_canonical
from harness.domain.workbench import WorkbenchPreference
from harness.infrastructure.sqlite.transaction_guard import require_transaction
from harness.ports.cli_workbench import WorkbenchRecord

__all__ = ["SqliteInvocationJournalRepository", "SqliteWorkbenchRepository"]


class SqliteWorkbenchRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get(self, session_id: str) -> WorkbenchRecord | None:
        row = self._connection.execute(
            "SELECT session_id,state,version,document_hash FROM workbench_session "
            "WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return _record(row)

    def list_recent(self, limit: int) -> tuple[WorkbenchRecord, ...]:
        bound = max(1, min(int(limit), 200))
        rows = self._connection.execute(
            "SELECT session_id,state,version,document_hash FROM workbench_session "
            "ORDER BY rowid DESC LIMIT ?",
            (bound,),
        ).fetchall()
        return tuple(_record(row) for row in rows)

    def save(self, record: WorkbenchRecord, *, expected_version: int) -> None:
        require_transaction(self._connection, "workbench session save")
        if record.version != expected_version + 1:
            raise HarnessError(
                ErrorCode.EVENT_ORDER_VIOLATION, "session update requires an ordered unit of work"
            )
        try:
            if expected_version == 0:
                self._connection.execute(
                    "INSERT INTO workbench_session(session_id,state,version,document_hash) "
                    "VALUES(?,?,?,?)",
                    (record.session_id, record.state, record.version, str(record.document_hash)),
                )
                return
            cursor = self._connection.execute(
                "UPDATE workbench_session SET state=?,version=?,document_hash=? "
                "WHERE session_id=? AND version=?",
                (
                    record.state,
                    record.version,
                    str(record.document_hash),
                    record.session_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise HarnessError(
                    ErrorCode.EVENT_ORDER_VIOLATION, "workbench session was concurrently changed"
                )
        except sqlite3.IntegrityError as error:
            raise HarnessError(
                ErrorCode.EVENT_ORDER_VIOLATION, "workbench session already exists"
            ) from error


def _record(row: sqlite3.Row) -> WorkbenchRecord:
    return WorkbenchRecord(
        str(row["session_id"]),
        str(row["state"]),
        int(row["version"]),
        ContentHash.parse(str(row["document_hash"])),
    )


class SqliteInvocationJournalRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def create_prepared(self, journal: CliInvocationJournal, *, session_id: str) -> None:
        require_transaction(self._connection, "cli invocation journal create")
        if journal.state is not InvocationState.PREPARED_DURABLE or journal.store_version != 0:
            raise HarnessError(
                ErrorCode.EVENT_ORDER_VIOLATION,
                "a new CLI invocation journal must start at PREPARED_DURABLE",
            )
        try:
            self._connection.execute(
                "INSERT INTO cli_invocation_journal(invocation_id,session_id,execution_plan_hash,"
                "request_hash,runtime_hash,state,response_hash,store_version,record_hash) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    journal.invocation_id,
                    session_id,
                    str(journal.execution_plan_hash),
                    str(journal.request_hash),
                    str(journal.runtime_hash),
                    journal.state.value,
                    None,
                    journal.store_version,
                    str(_record_hash(journal, session_id)),
                ),
            )
        except sqlite3.IntegrityError as error:
            # 同じ Session・同じ invocation の二重起動はここで落ちる。
            raise HarnessError(
                ErrorCode.APPROVAL_REPLAY, "a CLI invocation already exists for this session"
            ) from error

    def get_by_session(self, session_id: str) -> CliInvocationJournal | None:
        row = self._connection.execute(
            "SELECT invocation_id,session_id,execution_plan_hash,request_hash,runtime_hash,"
            "state,response_hash,store_version,record_hash FROM cli_invocation_journal "
            "WHERE session_id=?",
            (session_id,),
        ).fetchone()
        return None if row is None else _journal(row)

    def update(self, journal: CliInvocationJournal, *, expected_store_version: int) -> None:
        require_transaction(self._connection, "cli invocation journal update")
        if journal.store_version != expected_store_version + 1:
            raise HarnessError(
                ErrorCode.EVENT_ORDER_VIOLATION, "journal update must advance the store version"
            )
        row = self._connection.execute(
            "SELECT session_id FROM cli_invocation_journal WHERE invocation_id=?",
            (journal.invocation_id,),
        ).fetchone()
        if row is None:
            raise HarnessError(
                ErrorCode.EVENT_ORDER_VIOLATION, "CLI invocation journal does not exist"
            )
        session_id = str(row["session_id"])
        cursor = self._connection.execute(
            "UPDATE cli_invocation_journal SET state=?,response_hash=?,store_version=?,"
            "record_hash=? WHERE invocation_id=? AND store_version=?",
            (
                journal.state.value,
                None if journal.response_hash is None else str(journal.response_hash),
                journal.store_version,
                str(_record_hash(journal, session_id)),
                journal.invocation_id,
                expected_store_version,
            ),
        )
        if cursor.rowcount != 1:
            raise HarnessError(
                ErrorCode.EVENT_ORDER_VIOLATION, "CLI invocation journal was concurrently changed"
            )


def _record_hash(journal: CliInvocationJournal, session_id: str) -> ContentHash:
    return hash_canonical(
        {
            "invocation_id": journal.invocation_id,
            "session_id": session_id,
            "execution_plan_hash": str(journal.execution_plan_hash),
            "request_hash": str(journal.request_hash),
            "runtime_hash": str(journal.runtime_hash),
            "state": journal.state.value,
            "response_hash": None if journal.response_hash is None else str(journal.response_hash),
            "store_version": journal.store_version,
        },
        artifact_type="cli-invocation-journal",
        schema_major=1,
    )


def _journal(row: sqlite3.Row) -> CliInvocationJournal:
    try:
        state = InvocationState(str(row["state"]))
    except ValueError as error:
        raise HarnessError(
            ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "stored invocation state is not recognised"
        ) from error
    raw_response = row["response_hash"]
    journal = CliInvocationJournal(
        invocation_id=str(row["invocation_id"]),
        execution_plan_hash=ContentHash.parse(str(row["execution_plan_hash"])),
        request_hash=ContentHash.parse(str(row["request_hash"])),
        runtime_hash=ContentHash.parse(str(row["runtime_hash"])),
        state=state,
        response_hash=None if raw_response is None else ContentHash.parse(str(raw_response)),
        store_version=int(row["store_version"]),
    )
    if str(_record_hash(journal, str(row["session_id"]))) != str(row["record_hash"]):
        raise HarnessError(
            ErrorCode.ARTIFACT_CONTENT_CONFLICT, "CLI invocation journal row was tampered with"
        )
    return journal


class SqliteWorkbenchPreferenceRepository:
    """Same database and caller-owned transaction; optimistic version prevents tab races."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get_preference(self, scope_hash: ContentHash) -> WorkbenchPreference | None:
        row = self._connection.execute(
            "SELECT * FROM workbench_preference WHERE scope_hash=?", (str(scope_hash),)
        ).fetchone()
        if row is None:
            return None
        preference = WorkbenchPreference(
            ContentHash.parse(row["scope_hash"]),
            row["provider_id"],
            row["model_id"],
            row["reasoning_effort"],
            row["version"],
        )
        if str(preference.content_hash()) != row["content_hash"]:
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT, "stored preference hash mismatch"
            )
        return preference

    def save_preference(self, preference: WorkbenchPreference, *, expected_version: int) -> None:
        require_transaction(self._connection, "workbench preference save")
        if preference.version != expected_version + 1:
            raise HarnessError(ErrorCode.EVENT_ORDER_VIOLATION, "preference version must advance")
        values = (
            preference.provider_id,
            preference.model_id,
            preference.reasoning_effort,
            preference.version,
            str(preference.content_hash()),
            str(preference.scope_hash),
        )
        try:
            if expected_version == 0:
                self._connection.execute(
                    "INSERT INTO workbench_preference(provider_id,model_id,reasoning_effort,"
                    "version,content_hash,scope_hash) VALUES(?,?,?,?,?,?)",
                    values,
                )
            else:
                cursor = self._connection.execute(
                    "UPDATE workbench_preference SET provider_id=?,model_id=?,reasoning_effort=?,"
                    "version=?,content_hash=? WHERE scope_hash=? AND version=?",
                    (*values, expected_version),
                )
                if cursor.rowcount != 1:
                    raise HarnessError(
                        ErrorCode.EVENT_ORDER_VIOLATION, "preference changed; reload before saving"
                    )
        except sqlite3.IntegrityError as error:
            raise HarnessError(
                ErrorCode.EVENT_ORDER_VIOLATION, "preference already saved; reload before saving"
            ) from error
