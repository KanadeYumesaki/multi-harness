"""BLOCKER 3 の攻撃試験。**実装より先に書いた。**

## なぜ先に書くか

BLOCKER 5 は4ラウンドかかった。原因は、最初の実装が「Pathではなく実体へ
束縛する」という浅い層で満足し、その周辺（FD番号の再利用、ID の再利用、
並行発行、途中失効）を後から指摘されて継ぎ足したことにある。

今回は永続化とTransaction境界を扱う。壊れ方は「どの順序で失敗したときに
何が残るか」であり、正常系を書いてから異常系を足す順序では取りこぼす。
先に**壊し方**を列挙し、それを通す実装を書く。

## ここで固定する不変条件

1. `INPUT_MASKING_STARTED` のCommitに失敗したらMaskerを呼ばない
2. Masker呼出し中にSQLite Transactionを開いていない
3. Receipt保存とEvent追記は同一の `BEGIN IMMEDIATE` Transaction
4. どちらが失敗しても両方Rollbackする（片方だけ残らない）
5. Event順序競合時にReceiptを残さない
6. 2つのRepositoryが別Connectionなら即時拒否
7. 同一 `masking_receipt_id` の重複実行を拒否する
8. REJECT／Timeoutでも原文・`detail`・Secret CanaryをDBへ書かない
9. STARTED後に停止した実行をRecoveryが検出できる
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from harness.application.masking_service import (
    MaskingExecutionRequest,
    MaskingService,
)
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_bytes
from harness.domain.recovery import RecoveryClassification
from harness.infrastructure.masking.mock_masker import (
    FullyIsolatedReport,
    RawSpanMasker,
    StaticPhraseMasker,
    UnavailableMasker,
)
from harness.infrastructure.masking.pipeline import MaskingPipeline
from harness.infrastructure.masking.policy import MaskingPolicy
from harness.infrastructure.sqlite.connection_factory import ConnectionFactory, ConnectionRole
from harness.infrastructure.sqlite.event_ledger_repository import SqliteEventLedgerRepository
from harness.infrastructure.sqlite.lease_repository import SqliteLeaseRepository
from harness.infrastructure.sqlite.masking_receipt_repository import (
    SqliteMaskingReceiptRepository,
)
from harness.infrastructure.sqlite.masking_recovery_repository import (
    SqliteMaskingRecoveryRepository,
)
from harness.infrastructure.sqlite.migrations import migrate
from harness.infrastructure.sqlite.unit_of_work import SqliteUnitOfWork
from harness.ports.lease import LeaseAcquireRequest
from harness.ports.masker import MaskerDescriptor, MaskerIsolationReport, MaskerRequest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "support"))

from case_probe import observe_case
from ledger_probe import SideEffectProbe
from masking_probe import masking_evidence_payload
from real_ledger_view import RealLedgerView

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
CANARY = "FDE-HARNESS-CANARY-QX7T2M9P"
RUN_ID = "run-1"
STREAM_ID = "masking-stream-1"


@pytest.fixture(scope="module")
def policy() -> MaskingPolicy:
    return MaskingPolicy.load(REPO_ROOT)


_FACTORIES: dict[int, ConnectionFactory] = {}


def _factory_for(connection: sqlite3.Connection) -> ConnectionFactory:
    """試験用。Connectionから対応するFactoryを引く。"""
    return _FACTORIES[id(connection)]


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    factory = ConnectionFactory(tmp_path / "harness.db")
    migrate(factory, recorded_at="2026-08-14T00:00:00Z")
    conn = factory.connect(ConnectionRole.RUNTIME)
    _FACTORIES[id(conn)] = factory
    try:
        yield conn
    finally:
        _FACTORIES.pop(id(conn), None)
        conn.close()


def build_service(
    connection: sqlite3.Connection,
    policy: MaskingPolicy,
    masker: object,
    *,
    receipt_connection: sqlite3.Connection | None = None,
    factory: ConnectionFactory | None = None,
) -> MaskingService:
    pipeline = MaskingPipeline(policy, masker, repo_root=REPO_ROOT)  # type: ignore[arg-type]
    return MaskingService(
        pipeline=pipeline,
        ledger=SqliteEventLedgerRepository(connection),
        receipts=SqliteMaskingReceiptRepository(receipt_connection or connection),
        recovery=SqliteMaskingRecoveryRepository(connection),
        unit_of_work=SqliteUnitOfWork(factory or _factory_for(connection), connection),
        policy_snapshot_hash=policy.snapshot_hash,
    )


def request_for(
    text: str,
    *,
    receipt_id: str = "mr-1",
    attempt_id: str | None = None,
    lease_id: str | None = None,
) -> MaskingExecutionRequest:
    return MaskingExecutionRequest(
        masking_receipt_id=receipt_id,
        run_id=RUN_ID,
        stream_id=STREAM_ID,
        text=text,
        # §1.16.2 の ReadEvidence から引き継ぐ。Serviceは原文Bytesを持たない。
        source_content_hash=str(hash_bytes(text.encode("utf-8"))),
        recorded_at="2026-08-14T00:00:00Z",
        producer="test-harness",
        attempt_id=attempt_id,
        lease_id=lease_id,
    )


def dump_database(connection: sqlite3.Connection) -> str:
    return "\n".join(connection.iterdump())


def event_types(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT event_type FROM event_ledger WHERE stream_id = ? ORDER BY sequence_number",
        (STREAM_ID,),
    ).fetchall()
    return [row[0] for row in rows]


def acquire_masking_lease(
    connection: sqlite3.Connection, *, attempt_id: str, expires_at: str
) -> tuple[SqliteLeaseRepository, str]:
    leases = SqliteLeaseRepository(connection)
    unit_of_work = SqliteUnitOfWork(_factory_for(connection), connection)
    with unit_of_work.begin_immediate():
        lease = leases.acquire(
            LeaseAcquireRequest(
                resource_key=STREAM_ID,
                holder_id="masking-worker",
                attempt_id=attempt_id,
                issued_at="2026-08-14T00:00:00Z",
                expires_at=expires_at,
            )
        )
    return leases, lease.lease_id


# ---------------------------------------------------------------------------
# 1. STARTED の Commit 失敗
# ---------------------------------------------------------------------------


def test_masker_is_not_called_when_started_commit_fails(
    connection: sqlite3.Connection, policy: MaskingPolicy
) -> None:
    """STARTEDが永続化できなければMaskerを呼ばない。

    呼んでしまうと、Raw PIIがMaskerへ渡ったのに記録が残らない。
    「渡っていない」と「記録が無い」を後から区別できなくなる。
    """
    masker = StaticPhraseMasker(phrases={})
    service = build_service(connection, policy, masker)

    # Event追記を必ず失敗させる。
    connection.execute("DROP TRIGGER IF EXISTS event_ledger_no_update")
    original_append = service._ledger.append

    def failing_append(*args: object, **kwargs: object) -> object:
        raise HarnessError(ErrorCode.STORAGE_WRITE_FAILED, "injected append failure")

    service._ledger.append = failing_append  # type: ignore[method-assign]
    try:
        with pytest.raises(HarnessError):
            service.execute(request_for("担当は山田太郎さんです"))
    finally:
        service._ledger.append = original_append  # type: ignore[method-assign]

    assert masker.invocations == [], "STARTED未確定なのにMaskerが呼ばれた"
    assert event_types(connection) == []


# ---------------------------------------------------------------------------
# 2. Transaction を開いたままMaskerを呼ばない
# ---------------------------------------------------------------------------


class TransactionSpyMasker:
    """呼出し時にTransactionが開いていないことを検査するSpy。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.invocations: list[MaskerRequest] = []
        self.saw_open_transaction = False

    def descriptor(self) -> MaskerDescriptor:
        return MaskerDescriptor(
            provider="spy",
            model="spy-1",
            model_digest="sha256:" + "0" * 64,
            instruction_hash="sha256:" + "1" * 64,
        )

    def verify_isolation(self) -> MaskerIsolationReport:
        return FullyIsolatedReport

    def propose_spans(self, request: MaskerRequest) -> str:
        self.invocations.append(request)
        if self._connection.in_transaction:
            self.saw_open_transaction = True
        import json

        return json.dumps(
            {
                "source_normalized_hash": request.source_normalized_hash,
                "normalization_profile": request.normalization_profile,
                "normalization_profile_artifact_hash": (
                    request.normalization_profile_artifact_hash
                ),
                "spans": [],
            }
        )


def test_no_transaction_is_open_while_the_masker_runs(
    connection: sqlite3.Connection, policy: MaskingPolicy
) -> None:
    """Masker呼出しは外部Processとの往復であり、時間の上限が読めない。

    Transactionを開いたまま待つと、その間ずっとDBを掴む。
    `BEGIN IMMEDIATE` なら書込みロックを保持したまま止まる。
    """
    masker = TransactionSpyMasker(connection)
    service = build_service(connection, policy, masker)
    service.execute(request_for("担当は山田太郎さんです"))

    assert masker.invocations, "Maskerが呼ばれていない"
    assert not masker.saw_open_transaction, (
        "Transactionを開いたままMaskerを呼んでいる。外部呼出しの間DBを掴み続ける。"
    )


# ---------------------------------------------------------------------------
# 3. 同一Transaction / 部分Rollback
# ---------------------------------------------------------------------------


def test_receipt_and_terminal_event_are_atomic_on_event_failure(
    connection: sqlite3.Connection, policy: MaskingPolicy
) -> None:
    """終端Event追記が失敗したらReceiptも残さない。"""
    masker = StaticPhraseMasker(phrases={})
    service = build_service(connection, policy, masker)

    calls = {"n": 0}
    original_append = service._ledger.append

    def fail_second_append(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] >= 2:  # STARTED は通し、終端で落とす
            raise HarnessError(ErrorCode.STORAGE_WRITE_FAILED, "injected terminal failure")
        return original_append(*args, **kwargs)  # type: ignore[operator]

    service._ledger.append = fail_second_append  # type: ignore[method-assign]
    with pytest.raises(HarnessError):
        service.execute(request_for("担当は山田太郎さんです"))
    service._ledger.append = original_append  # type: ignore[method-assign]

    receipts = connection.execute("SELECT COUNT(*) FROM masking_receipt").fetchone()[0]
    assert receipts == 0, "終端Eventが失敗したのにReceiptが残っている"
    assert event_types(connection) == ["INPUT_MASKING_STARTED"]


def test_receipt_and_terminal_event_are_atomic_on_receipt_failure(
    connection: sqlite3.Connection, policy: MaskingPolicy
) -> None:
    """Receipt保存が失敗したら終端Eventも残さない。"""
    masker = StaticPhraseMasker(phrases={})
    service = build_service(connection, policy, masker)

    def failing_save(*args: object, **kwargs: object) -> object:
        raise HarnessError(ErrorCode.STORAGE_WRITE_FAILED, "injected receipt failure")

    service._receipts.save = failing_save  # type: ignore[method-assign]
    with pytest.raises(HarnessError):
        service.execute(request_for("担当は山田太郎さんです"))

    assert event_types(connection) == ["INPUT_MASKING_STARTED"], (
        "Receipt保存が失敗したのに終端Eventが残っている"
    )


def test_event_sequence_conflict_leaves_no_receipt(
    connection: sqlite3.Connection, policy: MaskingPolicy
) -> None:
    """Event順序競合（CAS不一致）でもReceiptを残さない。"""
    masker = StaticPhraseMasker(phrases={})
    service = build_service(connection, policy, masker)

    original_append = service._ledger.append
    calls = {"n": 0}

    def conflict_on_terminal(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise HarnessError(ErrorCode.EVENT_ORDER_VIOLATION, "injected CAS conflict")
        return original_append(*args, **kwargs)  # type: ignore[operator]

    service._ledger.append = conflict_on_terminal  # type: ignore[method-assign]
    with pytest.raises(HarnessError) as error:
        service.execute(request_for("担当は山田太郎さんです"))
    assert error.value.code is ErrorCode.EVENT_ORDER_VIOLATION

    receipts = connection.execute("SELECT COUNT(*) FROM masking_receipt").fetchone()[0]
    assert receipts == 0


# ---------------------------------------------------------------------------
# 4. Connection の同一性
# ---------------------------------------------------------------------------


def test_different_connections_are_rejected_at_construction(
    tmp_path: Path, connection: sqlite3.Connection, policy: MaskingPolicy
) -> None:
    """2つのRepositoryが別Connectionなら、そもそも組み立てさせない。

    別Connectionだと同一Transactionに入らない。片方だけがCommitされる形は
    実行してから気付いても遅い。
    """
    factory = ConnectionFactory(tmp_path / "harness.db")
    other = factory.connect(ConnectionRole.RUNTIME)
    try:
        with pytest.raises(HarnessError) as error:
            build_service(
                connection,
                policy,
                StaticPhraseMasker(phrases={}),
                receipt_connection=other,
            )
        assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
    finally:
        other.close()


def test_ledger_without_a_transaction_identity_is_rejected(
    connection: sqlite3.Connection, policy: MaskingPolicy
) -> None:
    """識別子を出せないRepositoryを組み込ませない。

    同一Transaction判定は `transaction_identity()` の一致で行う。
    片方がそれを持たないと**比較そのものが成立しない**。
    「比較できなかったので通す」を許すと、判定を持たない実装を
    差し込むだけで検査を外せることになる。
    """

    class LedgerWithoutIdentity:
        """`transaction_identity` を持たないLedger。"""

        def __init__(self, inner: SqliteEventLedgerRepository) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> object:
            if name == "transaction_identity":
                raise AttributeError(name)
            return getattr(self._inner, name)

    pipeline = MaskingPipeline(policy, StaticPhraseMasker(phrases={}), repo_root=REPO_ROOT)
    with pytest.raises(HarnessError) as error:
        MaskingService(
            pipeline=pipeline,
            ledger=LedgerWithoutIdentity(SqliteEventLedgerRepository(connection)),  # type: ignore[arg-type]
            receipts=SqliteMaskingReceiptRepository(connection),
            recovery=SqliteMaskingRecoveryRepository(connection),
            unit_of_work=SqliteUnitOfWork(_factory_for(connection), connection),
            policy_snapshot_hash=policy.snapshot_hash,
        )
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
    assert "transaction identity" in str(error.value)


def test_open_transaction_before_the_masker_call_is_refused(
    connection: sqlite3.Connection, policy: MaskingPolicy
) -> None:
    """Transactionを掴んだままMaskerを呼ばない。

    `BEGIN IMMEDIATE` は書込みロックを取る。Maskerの応答を待つ間ずっと
    DBを占有すると、他のStreamの書込みが Masker の応答時間だけ止まる。
    Timeout 30秒なら30秒止まる。

    既存試験（`test_no_transaction_is_open_while_the_masker_runs`）は
    「開いていないこと」を観測する。こちらは**開いていたら止まること**を
    確かめる。観測と強制は別で、観測だけでは強制が消えても気付けない。
    """

    class StuckUnitOfWork:
        """Transaction 1 のあともTransactionが開いていると報告する。"""

        def __init__(self, inner: SqliteUnitOfWork) -> None:
            self._inner = inner
            self._begun = False

        def begin_immediate(self) -> object:
            self._begun = True
            return self._inner.begin_immediate()

        def in_transaction(self) -> bool:
            return self._begun or self._inner.in_transaction()

        def transaction_identity(self) -> object:
            return self._inner.transaction_identity()

    service = MaskingService(
        pipeline=MaskingPipeline(policy, StaticPhraseMasker(phrases={}), repo_root=REPO_ROOT),
        ledger=SqliteEventLedgerRepository(connection),
        receipts=SqliteMaskingReceiptRepository(connection),
        recovery=SqliteMaskingRecoveryRepository(connection),
        unit_of_work=StuckUnitOfWork(  # type: ignore[arg-type]
            SqliteUnitOfWork(_factory_for(connection), connection)
        ),
        policy_snapshot_hash=policy.snapshot_hash,
    )

    with pytest.raises(HarnessError) as error:
        service.execute(request_for("担当は山田太郎です"))
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
    assert "write lock" in str(error.value)

    # STARTED は残ってよい。Maskerへ渡っていないことが要点である。
    assert event_types(connection) == ["INPUT_MASKING_STARTED"]


# ---------------------------------------------------------------------------
# 5. 重複実行
# ---------------------------------------------------------------------------


def test_duplicate_masking_receipt_id_is_rejected(
    connection: sqlite3.Connection, policy: MaskingPolicy
) -> None:
    """同一 `masking_receipt_id` の再実行を拒否する。

    通してしまうと、同じIDに異なるHashのReceiptが並ぶ。
    どちらが本物かを後から決められない。
    """
    service = build_service(connection, policy, StaticPhraseMasker(phrases={}))
    service.execute(request_for("担当は山田太郎さんです", receipt_id="mr-dup"))

    with pytest.raises(HarnessError) as error:
        service.execute(request_for("別の本文です", receipt_id="mr-dup"))
    assert error.value.code is ErrorCode.STORAGE_WRITE_FAILED

    rows = connection.execute(
        "SELECT COUNT(*) FROM masking_receipt WHERE masking_receipt_id = ?", ("mr-dup",)
    ).fetchone()[0]
    assert rows == 1


# ---------------------------------------------------------------------------
# 6. 原文・detail・Canary を保存しない
# ---------------------------------------------------------------------------


def test_rejected_input_stores_no_raw_text_or_canary(
    connection: sqlite3.Connection, policy: MaskingPolicy
) -> None:
    """REJECT時もDBへ原文・Canaryを書かない。"""
    masker = StaticPhraseMasker(phrases={})
    service = build_service(connection, policy, masker)
    text = f"marker {CANARY} end"
    outcome = service.execute(request_for(text, receipt_id="mr-reject"))

    assert outcome.report.result == "REJECTED"
    assert masker.invocations == [], "REJECTカテゴリなのにMaskerへ渡っている"

    dump = dump_database(connection)
    assert CANARY not in dump, "Secret CanaryがDBへ保存されている"
    assert "marker" not in dump
    assert event_types(connection) == [
        "INPUT_MASKING_STARTED",
        "INPUT_MASKING_REJECTED",
    ]


def test_report_detail_is_never_persisted(
    connection: sqlite3.Connection, policy: MaskingPolicy
) -> None:
    """`MaskingReport.detail` はDBへ保存しない。

    detailは診断用の自由文であり、将来の変更で値が混ざり得る。
    構造化された項目だけを保存する。
    """
    service = build_service(connection, policy, StaticPhraseMasker(phrases={}))
    outcome = service.execute(request_for("password: hunter2xyz", receipt_id="mr-detail"))
    assert outcome.report.detail, "前提: detailが空でないCaseを使う"
    assert outcome.report.detail not in dump_database(connection)


def test_masker_unavailable_stores_no_text(
    connection: sqlite3.Connection, policy: MaskingPolicy
) -> None:
    """Timeout／利用不能でも原文を残さない。"""
    service = build_service(connection, policy, UnavailableMasker())
    text = "担当は山田太郎さんです"
    outcome = service.execute(request_for(text, receipt_id="mr-timeout"))

    assert outcome.report.error_code is ErrorCode.MASKER_UNAVAILABLE
    dump = dump_database(connection)
    assert "山田太郎" not in dump
    assert event_types(connection)[-1] == "INPUT_MASKING_REJECTED"


# ---------------------------------------------------------------------------
# 7. 正常系とRecovery
# ---------------------------------------------------------------------------


def test_successful_run_persists_receipt_and_events(
    connection: sqlite3.Connection, policy: MaskingPolicy
) -> None:
    service = build_service(
        connection, policy, StaticPhraseMasker(phrases={"山田太郎": "PERSON_NAME"})
    )
    outcome = service.execute(request_for("担当は山田太郎さんです", receipt_id="mr-ok"))

    assert outcome.report.result == "MASKED"
    assert event_types(connection) == [
        "INPUT_MASKING_STARTED",
        "INPUT_MASKING_SCAN1_CANDIDATES_READY",
        "INPUT_MASKING_SPANS_PROPOSED",
        "INPUT_MASKING_COMPLETED",
    ]

    row = connection.execute(
        "SELECT source_content_hash, normalization_profile, masking_policy_version,"
        " scan1_decision, scan2_result FROM masking_receipt WHERE masking_receipt_id = ?",
        ("mr-ok",),
    ).fetchone()
    assert row is not None
    assert row[0] == str(hash_bytes("担当は山田太郎さんです".encode()))
    assert row[1] == policy.normalization.profile_id
    assert row[2] == policy.policy_version
    assert row[3] in ("CLEAN", "MASKABLE", "REJECT")
    assert row[4] == "ACCEPTED"
    # 原文はどこにも無い
    assert "山田太郎" not in dump_database(connection)


def _crash_during_masker(service: MaskingService, request: MaskingExecutionRequest) -> None:
    """STARTEDを確定した後、Masker呼出し中に停止した状態を作る。"""

    def die(*args: object, **kwargs: object) -> object:
        raise RuntimeError("process died during masker call")

    service._pipeline.run = die  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        service.execute(request)


def test_active_masking_stream_is_not_an_automatic_recovery_candidate(
    connection: sqlite3.Connection, policy: MaskingPolicy
) -> None:
    """実SQLite上のACTIVE Leaseを持つSTARTED Streamを打ち切らない。"""
    attempt_id = "attempt-active"
    leases, lease_id = acquire_masking_lease(
        connection, attempt_id=attempt_id, expires_at="2026-08-14T00:10:00Z"
    )
    service = build_service(connection, policy, UnavailableMasker())
    _crash_during_masker(
        service,
        request_for(
            "担当は山田太郎さんです",
            receipt_id="mr-active",
            attempt_id=attempt_id,
            lease_id=lease_id,
        ),
    )

    decisions = service.classify_incomplete_streams(leases=leases, now="2026-08-14T00:05:00Z")

    assert [(item.stream_id, item.classification) for item in decisions] == [
        (STREAM_ID, RecoveryClassification.ACTIVE)
    ]
    assert service.expired_recovery_candidates(leases=leases, now="2026-08-14T00:05:00Z") == ()
    assert event_types(connection) == ["INPUT_MASKING_STARTED"]


def test_expired_masking_stream_is_discoverable_as_recovery_candidate(
    connection: sqlite3.Connection, policy: MaskingPolicy
) -> None:
    """実SQLiteの期限切れLeaseを持つSTARTED Streamだけが候補化される。"""
    attempt_id = "attempt-expired"
    leases, lease_id = acquire_masking_lease(
        connection, attempt_id=attempt_id, expires_at="2026-08-14T00:00:01Z"
    )
    service = build_service(connection, policy, UnavailableMasker())
    _crash_during_masker(
        service,
        request_for(
            "担当は山田太郎さんです",
            receipt_id="mr-expired",
            attempt_id=attempt_id,
            lease_id=lease_id,
        ),
    )

    candidates = service.expired_recovery_candidates(leases=leases, now="2026-08-14T00:05:00Z")

    assert [(item.stream_id, item.classification) for item in candidates] == [
        (STREAM_ID, RecoveryClassification.EXPIRED)
    ]
    bound = connection.execute(
        "SELECT run_id, attempt_id, lease_id FROM masking_recovery_stream WHERE stream_id = ?",
        (STREAM_ID,),
    ).fetchone()
    assert bound is not None
    assert tuple(bound) == (RUN_ID, attempt_id, lease_id)
    assert event_types(connection) == ["INPUT_MASKING_STARTED"]


# ---------------------------------------------------------------------------
# SECOND_SCAN_IS_THE_GATE（Orchestration層）
# ---------------------------------------------------------------------------

# 14桁の数字列はNATIONAL_ID規則（ちょうど12桁）に当たらない。中央の1桁を
# マスクすると右側がちょうど12桁になり、**マスクした結果として**規則に当たる。
# Scan#1では検出しようがなく、Scan#2だけが捕まえられる形である。
_MANUFACTURED_NATIONAL_ID = "code 12345678901234 end"
_SPLIT_ONE_DIGIT = (6, 7)


@pytest.mark.case("AT-MASKING-001/SECOND_SCAN_IS_THE_GATE")
def test_second_scan_rejection_is_recorded_in_the_ledger(
    connection: sqlite3.Connection,
    policy: MaskingPolicy,
    case_observation: Any,
) -> None:
    """Scan#2の拒否を**正本Ledgerから読み戻して**観測する。

    Pipelineは Ledger Port を持たないので、この Case の Event 列は
    Orchestration 層でしか観測できない（§1.16.2.3）。

    期待列は4件である。`INPUT_MASKING_SCAN1_CANDIDATES_READY` を含む。
    Maskerを呼ぶにはScan#1を通過している必要があり、通過していれば候補は
    揃っている。3件の列は構成上到達しない（§1.16.2.3 / MASK-EVT-1）。

    **Event列は試験側で作らない。** Ledgerへ実際にAppendされた列を読む。
    """
    masker = RawSpanMasker(spans=[(*_SPLIT_ONE_DIGIT, "PERSON_NAME")])
    service = build_service(connection, policy, masker)
    view = RealLedgerView(SqliteEventLedgerRepository(connection), STREAM_ID)

    request = request_for(_MANUFACTURED_NATIONAL_ID)
    head_before = view.head
    report = service.execute(request).report

    assert report.result == "REJECTED"
    assert report.error_code is ErrorCode.MASKING_VERIFICATION_FAILED
    assert report.masked_text is None
    # 拒否した以上、Ledgerは動いている。動いていなければ観測点が違う。
    assert view.head > head_before

    observe_case(
        case_observation,
        "AT-MASKING-001/SECOND_SCAN_IS_THE_GATE",
        state=str(report.result),
        subject_id=f"masking-{request.masking_receipt_id}",
        error_code=report.error_code.value,
        ledger=view,
        effects=SideEffectProbe(),
        head_before=head_before,
        payload=masking_evidence_payload(report, input_label="MANUFACTURED_NATIONAL_ID"),
    )
