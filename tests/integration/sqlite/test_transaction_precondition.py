"""Repositoryの書込みがTransaction内であることの強制（不変条件#15）。

## 何が起きていたか

`isolation_level=None` のため、SQLiteは明示的な`BEGIN`が無いと
**文ごとに自動Commit**する。Repositoryは「Unit of Workが開いた
Transactionの内側で呼ばれる」前提で書かれていたが、その前提を
**検査していなかった**。

レビューで再現された形は次である。

    正常Event と 不正Event を1回のappendで渡す（Transaction外）
    -> 1件目だけが自動Commitされ、2件目で例外
    -> 部分적に書かれた状態が残る

Append単位の原子性が前提から崩れる。Hash Chainは連続している必要が
あるため、途中まで書かれた状態は後続のAppendを全て狂わせる。

## なぜ「呼ぶ側が気を付ける」で済ませないか

済ませていたから起きた。Transaction境界はApplication層が持つという
規約はあったが、Repositoryは規約が守られたかを見ていなかった。
規約は破られる前提で、破れたら止まる形にする。

読取りは検査しない。Transactionの有無で結果の正しさが変わらないためである。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_bytes
from harness.infrastructure.sqlite.artifact_manifest_repository import (
    SqliteArtifactManifestRepository,
)
from harness.infrastructure.sqlite.connection_factory import ConnectionFactory, ConnectionRole
from harness.infrastructure.sqlite.event_ledger_repository import SqliteEventLedgerRepository
from harness.infrastructure.sqlite.masking_receipt_repository import (
    SqliteMaskingReceiptRepository,
)
from harness.infrastructure.sqlite.migrations import migrate
from harness.ports.artifact_store import ArtifactManifestRecord
from harness.ports.event_ledger import NewEvent
from harness.ports.masking import MaskingReceiptRecord

pytestmark = pytest.mark.integration

RECORDED_AT = "2026-08-14T00:00:00Z"
STREAM_ID = "stream-tx"


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


def event(event_type: str, payload: bytes = b"x") -> NewEvent:
    return NewEvent(
        stream_id=STREAM_ID,
        event_type=event_type,
        payload_hash=hash_bytes(payload),
        recorded_at=RECORDED_AT,
    )


def receipt(receipt_id: str) -> MaskingReceiptRecord:
    return MaskingReceiptRecord(
        masking_receipt_id=receipt_id,
        record_id=f"masking-receipt:{receipt_id}",
        run_id="run-1",
        stream_id=STREAM_ID,
        attempt_id=None,
        lease_id=None,
        created_at=RECORDED_AT,
        producer="test",
        content_hash="sha256:" + "a" * 64,
        source_content_hash="sha256:" + "b" * 64,
        source_normalized_hash="sha256:" + "e" * 64,
        masked_content_hash=None,
        normalization_profile="NFC_CODEPOINT_V3",
        normalization_profile_artifact_hash="sha256:" + "c" * 64,
        masking_policy_version=4,
        policy_snapshot_hash="sha256:" + "d" * 64,
        masking_result="REJECTED",
        scan1_decision="REJECT",
        scan1_findings=(),
        span_validation_result=None,
        scan2_result=None,
        spans=(),
        rejected_categories=("PASSWORD",),
        error_code=None,
        masker_provider=None,
        masker_model=None,
        masker_model_digest=None,
        masker_instruction_hash=None,
        masker_invocation_count=0,
        rewriter_version="DETERMINISTIC_SPAN_REWRITER_V1",
    )


def manifest(content: bytes) -> ArtifactManifestRecord:
    return ArtifactManifestRecord(
        artifact_id="artifact-1",
        content_hash=hash_bytes(content),
        media_type="text/plain",
        size_bytes=len(content),
        data_classification="INTERNAL",
        trust_level="TRUSTED",
        stored_at=RECORDED_AT,
        storage_path="ab/cd/artifact",
    )


# ---------------------------------------------------------------------------
# レビューで再現された形
# ---------------------------------------------------------------------------


def test_partial_append_cannot_be_auto_committed_outside_a_transaction(
    connection: sqlite3.Connection,
) -> None:
    """正常Event＋不正Event を Transaction外で渡しても、1件も残らない。

    以前は1件目が自動Commitされ、2件目の例外後も残っていた。
    Hash Chainは連続している必要があるため、途中まで書かれた状態は
    後続のAppendを全て狂わせる。
    """
    ledger = SqliteEventLedgerRepository(connection)
    events = [event("RUN_CREATED"), event("NOT_A_REGISTERED_EVENT_TYPE")]

    with pytest.raises(HarnessError) as error:
        ledger.append(events, expected_stream_sequence=0)
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH

    rows = connection.execute("SELECT COUNT(*) FROM event_ledger").fetchone()[0]
    assert rows == 0, "Transaction外のAppendで行が残っている"


# ---------------------------------------------------------------------------
# 各Repositoryの書込み
# ---------------------------------------------------------------------------


def test_event_append_outside_a_transaction_is_rejected(
    connection: sqlite3.Connection,
) -> None:
    ledger = SqliteEventLedgerRepository(connection)
    with pytest.raises(HarnessError) as error:
        ledger.append([event("RUN_CREATED")], expected_stream_sequence=0)
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
    assert "transaction" in str(error.value).lower()


def test_receipt_save_outside_a_transaction_is_rejected(
    connection: sqlite3.Connection,
) -> None:
    receipts = SqliteMaskingReceiptRepository(connection)
    with pytest.raises(HarnessError) as error:
        receipts.save(receipt("mr-tx"))
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH


def test_manifest_register_outside_a_transaction_is_rejected(
    connection: sqlite3.Connection,
) -> None:
    repository = SqliteArtifactManifestRepository(connection)
    with pytest.raises(HarnessError) as error:
        repository.register(manifest(b"payload"))
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH


def test_mark_payload_deleted_outside_a_transaction_is_rejected(
    factory: ConnectionFactory, connection: sqlite3.Connection
) -> None:
    repository = SqliteArtifactManifestRepository(connection)
    record = manifest(b"payload")
    with factory.begin_immediate(connection):
        repository.register(record)

    with pytest.raises(HarnessError) as error:
        repository.mark_payload_deleted(record.content_hash)
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH


# ---------------------------------------------------------------------------
# 対照: Transaction内なら通る
# ---------------------------------------------------------------------------


def test_writes_succeed_inside_a_transaction(
    factory: ConnectionFactory, connection: sqlite3.Connection
) -> None:
    """検査が広すぎて正常経路を止めていないこと。"""
    ledger = SqliteEventLedgerRepository(connection)
    receipts = SqliteMaskingReceiptRepository(connection)
    repository = SqliteArtifactManifestRepository(connection)

    with factory.begin_immediate(connection):
        ledger.append([event("RUN_CREATED")], expected_stream_sequence=0)
        receipts.save(receipt("mr-ok"))
        repository.register(manifest(b"payload"))

    assert connection.execute("SELECT COUNT(*) FROM event_ledger").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM masking_receipt").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM artifact_manifest").fetchone()[0] == 1


def test_reads_do_not_require_a_transaction(
    factory: ConnectionFactory, connection: sqlite3.Connection
) -> None:
    """読取りは検査しない。Transactionの有無で結果の正しさが変わらない。

    ここを厳しくすると、Recovery用の照会や検証にまでTransactionが要る。
    """
    ledger = SqliteEventLedgerRepository(connection)
    receipts = SqliteMaskingReceiptRepository(connection)
    with factory.begin_immediate(connection):
        ledger.append([event("RUN_CREATED")], expected_stream_sequence=0)
        receipts.save(receipt("mr-read"))

    assert not connection.in_transaction
    assert ledger.stream_head(STREAM_ID) == 1
    assert len(ledger.load_stream(STREAM_ID)) == 1
    assert ledger.verify_chain(STREAM_ID).valid
    assert receipts.exists("mr-read")


def test_rollback_leaves_nothing(
    factory: ConnectionFactory, connection: sqlite3.Connection
) -> None:
    """Transaction内で失敗すれば全部消える。これが本来の姿である。"""
    ledger = SqliteEventLedgerRepository(connection)
    with pytest.raises(HarnessError):
        with factory.begin_immediate(connection):
            ledger.append([event("RUN_CREATED")], expected_stream_sequence=0)
            ledger.append([event("NOT_A_REGISTERED_EVENT_TYPE")], expected_stream_sequence=1)

    assert connection.execute("SELECT COUNT(*) FROM event_ledger").fetchone()[0] == 0
