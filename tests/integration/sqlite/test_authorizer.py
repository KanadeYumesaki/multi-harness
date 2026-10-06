"""SQLite authorizer（多層防御の第3層）と、Append時のEvent順序検証。

Triggerだけでは「Triggerを落としてからUPDATE」という順序を止められない。
authorizerはSQL発行そのものを拒否するため、この経路を先に塞ぐ。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_bytes
from harness.infrastructure.sqlite.connection_factory import (
    ConnectionFactory,
    ConnectionRole,
)
from harness.infrastructure.sqlite.event_ledger_repository import (
    SqliteEventLedgerRepository,
)
from harness.infrastructure.sqlite.migrations import migrate
from harness.ports.event_ledger import NewEvent

pytestmark = pytest.mark.integration

RECORDED_AT = "2026-08-07T00:00:00Z"


@pytest.fixture
def factory(tmp_path: Path) -> ConnectionFactory:
    made = ConnectionFactory(tmp_path / "harness.db")
    migrate(made, recorded_at=RECORDED_AT)
    return made


@pytest.fixture
def runtime(factory: ConnectionFactory) -> Iterator[sqlite3.Connection]:
    conn = factory.connect(ConnectionRole.RUNTIME)
    try:
        yield conn
    finally:
        conn.close()


def _event(stream: str, event_type: str, payload: str) -> NewEvent:
    return NewEvent(
        stream_id=stream,
        event_type=event_type,
        payload_hash=hash_bytes(payload.encode("utf-8")),
        recorded_at=RECORDED_AT,
    )


def _seed(factory: ConnectionFactory, conn: sqlite3.Connection) -> None:
    repository = SqliteEventLedgerRepository(conn)
    with factory.begin_immediate(conn):
        repository.append([_event("run-1", "INTENT_CREATED", "p1")], expected_stream_sequence=0)


# --------------------------------------------------------------------------
# authorizer による拒否
# --------------------------------------------------------------------------


def test_runtime_connection_denies_ledger_update(
    factory: ConnectionFactory, runtime: sqlite3.Connection
) -> None:
    _seed(factory, runtime)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        runtime.execute("UPDATE event_ledger SET event_type = 'ACTION_COMMITTED'")


def test_runtime_connection_denies_ledger_delete(
    factory: ConnectionFactory, runtime: sqlite3.Connection
) -> None:
    _seed(factory, runtime)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        runtime.execute("DELETE FROM event_ledger")


def test_runtime_connection_denies_dropping_the_protective_trigger(
    factory: ConnectionFactory, runtime: sqlite3.Connection
) -> None:
    """Triggerを落としてからUPDATEする順序を塞ぐ。

    authorizerが無ければ、この2手でLedgerを書き換えられてしまう。
    """
    _seed(factory, runtime)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        runtime.execute("DROP TRIGGER trg_event_ledger_no_update")
    # Triggerが残っていることを確認する
    row = runtime.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name='trg_event_ledger_no_update'"
    ).fetchone()
    assert row is not None


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE event_ledger",
        "ALTER TABLE event_ledger ADD COLUMN injected TEXT",
        "CREATE TABLE shadow_ledger (x TEXT)",
        "CREATE TRIGGER t AFTER INSERT ON event_ledger BEGIN SELECT 1; END",
    ],
)
def test_runtime_connection_denies_schema_change(
    runtime: sqlite3.Connection, statement: str
) -> None:
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        runtime.execute(statement)


def test_runtime_connection_still_allows_append_and_read(
    factory: ConnectionFactory, runtime: sqlite3.Connection
) -> None:
    """拒否は書換えとSchema変更に限る。追記と読取りは通常どおり動く。"""
    repository = SqliteEventLedgerRepository(runtime)
    with factory.begin_immediate(runtime):
        repository.append([_event("run-1", "INTENT_CREATED", "p1")], expected_stream_sequence=0)
    assert repository.verify_chain("run-1").valid
    assert len(repository.load_stream("run-1")) == 1


def test_migration_connection_is_not_restricted(factory: ConnectionFactory) -> None:
    """Migration接続にはauthorizerを設定しない。Schema変更はMigrationの責務。"""
    conn = factory.connect(ConnectionRole.MIGRATION)
    try:
        with factory.begin_immediate(conn):
            conn.execute("CREATE TABLE probe (x TEXT) STRICT")
            conn.execute("DROP TABLE probe")
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Append時のEvent順序検証（§1.4.1）
# --------------------------------------------------------------------------


def test_append_rejects_event_missing_its_predecessor(
    factory: ConnectionFactory, runtime: sqlite3.Connection
) -> None:
    """`AT-EVENT-ORDER-001/ACTION_STARTED_MISSING` 相当。

    `ACTION_PREPARED` は `ACTION_STARTED` を必要とする。
    拒否時にLedgerへ1件も書かれないこと（Stateを変えない）を確認する。
    """
    repository = SqliteEventLedgerRepository(runtime)
    with factory.begin_immediate(runtime):
        repository.append(
            [
                _event("run-1", "INTENT_CREATED", "p1"),
                _event("run-1", "PLAN_RESOLVED", "p2"),
            ],
            expected_stream_sequence=0,
        )

    with pytest.raises(HarnessError) as excinfo, factory.begin_immediate(runtime):
        repository.append([_event("run-1", "ACTION_PREPARED", "p3")], expected_stream_sequence=2)
    assert excinfo.value.code is ErrorCode.EVENT_ORDER_VIOLATION
    assert "ACTION_STARTED" in str(excinfo.value)
    assert len(repository.load_stream("run-1")) == 2


def test_append_accepts_predecessor_within_same_batch(
    factory: ConnectionFactory, runtime: sqlite3.Connection
) -> None:
    """同一Append内の先行Eventも順序の根拠になる。"""
    repository = SqliteEventLedgerRepository(runtime)
    with factory.begin_immediate(runtime):
        repository.append(
            [
                _event("run-1", "INTENT_CREATED", "a"),
                _event("run-1", "PLAN_RESOLVED", "b"),
                _event("run-1", "POLICY_DECIDED", "c"),
                _event("run-1", "ACTION_CLAIMED", "d"),
                _event("run-1", "LEASE_ACQUIRED", "e"),
                _event("run-1", "RUNTIME_ATTESTED", "f"),
                _event("run-1", "ACTION_STARTED", "g"),
                _event("run-1", "ACTION_PREPARED", "h"),
            ],
            expected_stream_sequence=0,
        )
    assert repository.verify_chain("run-1").entry_count == 8


def test_order_violation_leaves_no_partial_append(
    factory: ConnectionFactory, runtime: sqlite3.Connection
) -> None:
    """Batch途中の順序違反で先行分だけが残らない。"""
    repository = SqliteEventLedgerRepository(runtime)
    with pytest.raises(HarnessError), factory.begin_immediate(runtime):
        repository.append(
            [
                _event("run-1", "INTENT_CREATED", "a"),
                _event("run-1", "EFFECT_OBSERVED", "b"),  # EXECUTION_ATTEMPTED が無い
            ],
            expected_stream_sequence=0,
        )
    assert repository.load_stream("run-1") == []
