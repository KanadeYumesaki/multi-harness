"""SQLite Event LedgerとConnectionFactoryの統合試験。

検証対象は次の3点。

* §1.14 接続ごとのWAL／`synchronous=FULL`／`foreign_keys=ON`を**読み戻して**確認する
* 不変条件#1 LedgerのUPDATE／DELETEがTriggerで拒否される
* `expected_stream_sequence`によるCAS Appendと`EVENT_ORDER_VIOLATION`
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from case_probe import observe_unit_case

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_bytes
from harness.infrastructure.sqlite.connection_factory import (
    ConnectionFactory,
    ConnectionRole,
)
from harness.infrastructure.sqlite.event_ledger_repository import (
    SqliteEventLedgerRepository,
)
from harness.infrastructure.sqlite.migrations import SCHEMA_VERSION, migrate
from harness.ports.event_ledger import NewEvent

pytestmark = pytest.mark.integration

RECORDED_AT = "2026-08-06T00:00:00Z"


@pytest.fixture
def factory(tmp_path: Path) -> ConnectionFactory:
    made = ConnectionFactory(tmp_path / "harness.db")
    migrate(made, recorded_at=RECORDED_AT)
    return made


@pytest.fixture
def connection(factory: ConnectionFactory) -> Iterator[sqlite3.Connection]:
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


# --------------------------------------------------------------------------
# ConnectionFactory（§1.14）
# --------------------------------------------------------------------------


def test_pragmas_are_verified_by_readback(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_migration_is_idempotent(factory: ConnectionFactory) -> None:
    assert migrate(factory, recorded_at=RECORDED_AT) == SCHEMA_VERSION
    assert migrate(factory, recorded_at=RECORDED_AT) == SCHEMA_VERSION


def test_downgrade_is_refused(factory: ConnectionFactory) -> None:
    conn = factory.connect(ConnectionRole.MIGRATION)
    try:
        with factory.begin_immediate(conn):
            conn.execute(
                "INSERT INTO schema_migration (version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION + 1, RECORDED_AT),
            )
    finally:
        conn.close()
    with pytest.raises(HarnessError) as excinfo:
        migrate(factory, recorded_at=RECORDED_AT)
    assert excinfo.value.code is ErrorCode.MIGRATION_FAILED


# --------------------------------------------------------------------------
# 追記専用の強制（不変条件#1）
# --------------------------------------------------------------------------


def test_update_on_ledger_is_denied_on_runtime_connection(
    factory: ConnectionFactory, connection: sqlite3.Connection
) -> None:
    """Runtime接続ではauthorizer（第3層）がSQL発行時点で拒否する。"""
    repository = SqliteEventLedgerRepository(connection)
    with factory.begin_immediate(connection):
        repository.append([_event("run-1", "RUN_CREATED", "p1")], expected_stream_sequence=0)

    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        connection.execute("UPDATE event_ledger SET event_type = 'ACTION_COMMITTED'")


def test_delete_on_ledger_is_denied_on_runtime_connection(
    factory: ConnectionFactory, connection: sqlite3.Connection
) -> None:
    repository = SqliteEventLedgerRepository(connection)
    with factory.begin_immediate(connection):
        repository.append([_event("run-1", "RUN_CREATED", "p1")], expected_stream_sequence=0)

    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        connection.execute("DELETE FROM event_ledger")

    assert connection.execute("SELECT COUNT(*) FROM event_ledger").fetchone()[0] == 1


def test_trigger_still_denies_update_without_authorizer(
    factory: ConnectionFactory, connection: sqlite3.Connection
) -> None:
    """Trigger（第1層）がauthorizerとは独立に機能することを示す。

    authorizerが無い接続（Migration接続）でもUPDATEは拒否される。
    片方だけを検証するとTriggerが外れたことに気付けない。
    """
    repository = SqliteEventLedgerRepository(connection)
    with factory.begin_immediate(connection):
        repository.append([_event("run-1", "RUN_CREATED", "p1")], expected_stream_sequence=0)

    migration = factory.connect(ConnectionRole.MIGRATION)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            migration.execute("UPDATE event_ledger SET event_type = 'ACTION_COMMITTED'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            migration.execute("DELETE FROM event_ledger")
        assert migration.execute("SELECT COUNT(*) FROM event_ledger").fetchone()[0] == 1
    finally:
        migration.close()


# --------------------------------------------------------------------------
# CAS Append
# --------------------------------------------------------------------------


def test_append_and_verify_chain(
    factory: ConnectionFactory, connection: sqlite3.Connection
) -> None:
    repository = SqliteEventLedgerRepository(connection)
    with factory.begin_immediate(connection):
        result = repository.append(
            [
                _event("run-1", "RUN_CREATED", "p1"),
                _event("run-1", "INTENT_CREATED", "p2"),
                _event("run-1", "PLAN_RESOLVED", "p3"),
            ],
            expected_stream_sequence=0,
        )
    assert result.appended_count == 3
    assert (result.first_sequence_number, result.last_sequence_number) == (1, 3)

    verification = repository.verify_chain("run-1")
    assert verification.valid, verification.reason
    assert verification.entry_count == 3


def test_append_continues_existing_chain(
    factory: ConnectionFactory, connection: sqlite3.Connection
) -> None:
    repository = SqliteEventLedgerRepository(connection)
    with factory.begin_immediate(connection):
        repository.append([_event("run-1", "RUN_CREATED", "p1")], expected_stream_sequence=0)
    with factory.begin_immediate(connection):
        repository.append([_event("run-1", "INTENT_CREATED", "p2")], expected_stream_sequence=1)
    assert repository.verify_chain("run-1").valid
    entries = repository.load_stream("run-1")
    assert entries[1].previous_event_hash == entries[0].event_hash


def test_stale_expected_sequence_is_rejected(
    factory: ConnectionFactory, connection: sqlite3.Connection
) -> None:
    """§1.4.2／AT-EVENT-ORDER-001 SEQUENCE_REGRESSION。"""
    repository = SqliteEventLedgerRepository(connection)
    with factory.begin_immediate(connection):
        repository.append([_event("run-1", "RUN_CREATED", "p1")], expected_stream_sequence=0)

    with pytest.raises(HarnessError) as excinfo, factory.begin_immediate(connection):
        repository.append([_event("run-1", "INTENT_CREATED", "p2")], expected_stream_sequence=0)
    assert excinfo.value.code is ErrorCode.EVENT_ORDER_VIOLATION
    assert repository.verify_chain("run-1").entry_count == 1


def test_future_expected_sequence_is_rejected(
    factory: ConnectionFactory, connection: sqlite3.Connection
) -> None:
    repository = SqliteEventLedgerRepository(connection)
    with pytest.raises(HarnessError) as excinfo, factory.begin_immediate(connection):
        repository.append([_event("run-1", "RUN_CREATED", "p1")], expected_stream_sequence=7)
    assert excinfo.value.code is ErrorCode.EVENT_ORDER_VIOLATION


def test_unknown_event_type_is_rejected(
    factory: ConnectionFactory, connection: sqlite3.Connection
) -> None:
    """§1.8の正本Event Typeだけを受け付ける。"""
    repository = SqliteEventLedgerRepository(connection)
    with (
        pytest.raises(HarnessError, match="unknown event type"),
        factory.begin_immediate(connection),
    ):
        repository.append([_event("run-1", "NOT_A_REAL_EVENT", "p1")], expected_stream_sequence=0)


def test_rollback_leaves_no_partial_append(
    factory: ConnectionFactory, connection: sqlite3.Connection
) -> None:
    """Transaction内の失敗で部分Appendを残さない。"""
    repository = SqliteEventLedgerRepository(connection)
    with pytest.raises(HarnessError), factory.begin_immediate(connection):
        repository.append(
            [
                _event("run-1", "RUN_CREATED", "p1"),
                _event("run-1", "NOT_A_REAL_EVENT", "p2"),
            ],
            expected_stream_sequence=0,
        )
    assert repository.load_stream("run-1") == []


def test_streams_are_independent(
    factory: ConnectionFactory, connection: sqlite3.Connection
) -> None:
    repository = SqliteEventLedgerRepository(connection)
    with factory.begin_immediate(connection):
        repository.append([_event("run-1", "RUN_CREATED", "a")], expected_stream_sequence=0)
    with factory.begin_immediate(connection):
        repository.append([_event("run-2", "RUN_CREATED", "b")], expected_stream_sequence=0)
    assert repository.verify_chain("run-1").entry_count == 1
    assert repository.verify_chain("run-2").entry_count == 1
    assert repository.load_stream("run-2")[0].previous_event_hash is None


def test_append_rejects_multiple_streams_in_one_call(
    factory: ConnectionFactory, connection: sqlite3.Connection
) -> None:
    repository = SqliteEventLedgerRepository(connection)
    with (
        pytest.raises(HarnessError, match="exactly one stream"),
        factory.begin_immediate(connection),
    ):
        repository.append(
            [_event("run-1", "RUN_CREATED", "a"), _event("run-2", "RUN_CREATED", "b")],
            expected_stream_sequence=0,
        )


def test_load_stream_after_sequence(
    factory: ConnectionFactory, connection: sqlite3.Connection
) -> None:
    repository = SqliteEventLedgerRepository(connection)
    with factory.begin_immediate(connection):
        repository.append(
            [
                _event("run-1", "RUN_CREATED", "p1"),
                _event("run-1", "INTENT_CREATED", "p2"),
                _event("run-1", "PLAN_RESOLVED", "p3"),
            ],
            expected_stream_sequence=0,
        )
    tail = repository.load_stream("run-1", after_sequence=1)
    assert [entry.sequence_number for entry in tail] == [2, 3]


def test_tampered_row_is_detected_by_verify_chain(
    factory: ConnectionFactory, tmp_path: Path, connection: sqlite3.Connection
) -> None:
    """Triggerを迂回した直接改ざんをChain検証が検出する（Gate 14）。

    Triggerを一時的に落とせるのは同一UIDの攻撃者と同じ立場である。
    改ざん「耐性」ではなく「検知」を提供することの実証にあたる（ADR-004）。
    """
    repository = SqliteEventLedgerRepository(connection)
    with factory.begin_immediate(connection):
        repository.append(
            [
                _event("run-1", "RUN_CREATED", "p1"),
                _event("run-1", "INTENT_CREATED", "p2"),
            ],
            expected_stream_sequence=0,
        )
    assert repository.verify_chain("run-1").valid

    raw = factory.connect(ConnectionRole.MIGRATION)
    try:
        raw.execute("DROP TRIGGER trg_event_ledger_no_update")
        raw.execute(
            "UPDATE event_ledger SET payload_hash = ? WHERE sequence_number = 2",
            (str(hash_bytes(b"tampered")),),
        )
    finally:
        raw.close()

    result = repository.verify_chain("run-1")
    assert not result.valid
    assert result.first_invalid_sequence == 2


def test_empty_append_is_rejected(
    factory: ConnectionFactory, connection: sqlite3.Connection
) -> None:
    """Eventを1件も渡さないAppendを黙って成功させない。

    `expected_stream_sequence` は「この値の続きへ書く」という主張である。
    書くものが無いなら主張だけが残る。呼出側が空のリストを作ってしまう
    バグを、成功として返してしまうと発見が遅れる。
    """
    repository = SqliteEventLedgerRepository(connection)
    with factory.begin_immediate(connection):
        with pytest.raises(HarnessError) as error:
            repository.append([], expected_stream_sequence=0)
    assert error.value.code is ErrorCode.EVENT_ORDER_VIOLATION
    assert "at least one" in str(error.value)


def test_concurrent_insert_at_the_same_sequence_is_rejected(
    factory: ConnectionFactory, connection: sqlite3.Connection
) -> None:
    """同じ `(stream_id, sequence_number)` への二重Insertを推測で解決しない。

    CAS（`expected_stream_sequence`）で防ぐのが第1層だが、それを擦り抜けて
    もDBの一意制約が残っている。多層防御の**下の層が本当にあるか**を
    確かめる。上の層だけを試験していると、下が外れても気付けない。

    ここでは行を直接入れて衝突を作る。Triggerを落として書けるのは
    同一UIDの攻撃者と同じ立場である（ADR-004）。
    """
    repository = SqliteEventLedgerRepository(connection)
    with factory.begin_immediate(connection):
        repository.append([_event("run-1", "RUN_CREATED", "p1")], expected_stream_sequence=0)

    raw = factory.connect(ConnectionRole.MIGRATION)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "INSERT INTO event_ledger ("
                " stream_id, sequence_number, event_type, payload_hash,"
                " recorded_at, previous_event_hash, event_hash"
                ") VALUES (?, 1, 'RUN_CREATED', ?, ?, NULL, ?)",
                (
                    "run-1",
                    str(hash_bytes(b"other")),
                    RECORDED_AT,
                    str(hash_bytes(b"other-event")),
                ),
            )
    finally:
        raw.close()


@pytest.mark.case("AT-LEDGER-TAMPER-001/ONE_BYTE")
@pytest.mark.unit_subject("LEDGER_CHAIN_VERIFICATION", durability="NOT_APPLICABLE")
def test_one_byte_modification_is_detected(
    factory: ConnectionFactory, connection: sqlite3.Connection, case_observation: Any
) -> None:
    """記録済みHashの**1文字**だけを書き換える。

    直前の試験はPayload Hashを丸ごと別の値へ差し替える。それは検出できて
    当然であり、`ledger_payload_one_byte_modified` が問うているのはもっと
    狭い話である。攻撃者は普通、目立たない最小の改変を選ぶ。
    Hashの比較が前方一致や長さ比較へ退化していれば、丸ごとの差し替えは
    捕まえられても1文字の反転は素通りする。境界側で確かめる。

    改変対象はChainの**先頭**にする。壊れた場所より後ろが全部おかしくなる
    のではなく、壊れた場所そのものが指されることまで見る。
    """
    repository = SqliteEventLedgerRepository(connection)
    with factory.begin_immediate(connection):
        repository.append(
            [
                _event("run-1", "RUN_CREATED", "p1"),
                _event("run-1", "INTENT_CREATED", "p2"),
                _event("run-1", "PLAN_RESOLVED", "p3"),
            ],
            expected_stream_sequence=0,
        )
    assert repository.verify_chain("run-1").valid

    raw = factory.connect(ConnectionRole.MIGRATION)
    try:
        original = str(
            raw.execute(
                "SELECT payload_hash FROM event_ledger WHERE sequence_number = 1"
            ).fetchone()[0]
        )
        # 16進1桁を隣の値へ回す。差分は1文字、長さも接頭辞も保つ。
        prefix, _, digest = original.partition(":")
        flipped = f"{prefix}:{'1' if digest[0] == '0' else '0'}{digest[1:]}"
        assert flipped != original
        assert len(flipped) == len(original)

        raw.execute("DROP TRIGGER trg_event_ledger_no_update")
        raw.execute(
            "UPDATE event_ledger SET payload_hash = ? WHERE sequence_number = 1",
            (flipped,),
        )
    finally:
        raw.close()

    result = repository.verify_chain("run-1")
    assert not result.valid
    assert result.first_invalid_sequence == 1

    # 主張しているのは「改変を検出した」ことだけである。Chain 検証は Ledger へ
    # 何も足さないので、Event 列を観測しない（Registry の
    # `event_observation_policy: NOT_APPLICABLE`）。
    observe_unit_case(
        case_observation,
        "AT-LEDGER-TAMPER-001/ONE_BYTE",
        state="REJECTED",
        subject_id="run-1",
        error_code="LEDGER_CHAIN_TAMPERED",
        payload={
            "entry_count": result.entry_count,
            "first_invalid_sequence": result.first_invalid_sequence,
            "modified_characters": 1,
            "chain_valid": result.valid,
            "tamper_detected": not result.valid,
        },
    )
