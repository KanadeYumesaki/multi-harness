"""§1.18.1 GC Executor の統合試験。

`tmp_path` 内の隔離 Artifact Store と隔離 SQLite だけを使う。実環境の File を
触らない。削除は**実際に**行い、Filesystem・Manifest・正本 Ledger の3つを
読み戻して観測する。

対象 Case（F-1-C）: `AT-GC-001/RAW_RETENTION`

Planner の `ACCEPTED` を削除成功として扱わない。Planner は候補を挙げるだけで、
State は Executor が自分で観測した結果から決める。
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from harness.application.gc_executor import GcExecuteRequest, GcExecutor
from harness.domain.artifact import ArtifactMetadata
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.operations_safety import ArtifactRecord, plan_garbage_collection
from harness.infrastructure.artifact.filesystem_cas import FilesystemArtifactCas
from harness.infrastructure.artifact.store import ArtifactStore
from harness.infrastructure.sqlite.artifact_manifest_repository import (
    SqliteArtifactManifestRepository,
)
from harness.infrastructure.sqlite.connection_factory import ConnectionFactory, ConnectionRole
from harness.infrastructure.sqlite.event_ledger_repository import SqliteEventLedgerRepository
from harness.infrastructure.sqlite.migrations import migrate
from harness.infrastructure.sqlite.unit_of_work import SqliteUnitOfWork

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "support"))

from case_probe import observe_case
from ledger_probe import SideEffectProbe
from real_ledger_view import RealLedgerView

pytestmark = pytest.mark.integration

STREAM_ID = "ARTIFACT_GC_STREAM"
GC_RESULT_ID = "gc-1"
PAYLOAD = b"raw provider output payload\n"
ARTIFACT_ID = "artifact-1"

# Registry の `fault_point` 列は Case のシナリオ位置を表すラベルであり、
# `harness.domain.faults.FaultPoint` の列挙値ではない。両者を混同しないため
# 名前を分けて記録する。**新しい Domain Enum を追加しない。**
DECLARED_FAULT_POINT = "GC_EXECUTE_APPROVED"
ACTUAL_INJECTION_POINT = "none"


@dataclass
class FrozenClock:
    stamp: str = "2026-08-21T00:00:00Z"

    def now(self) -> str:
        return self.stamp


@dataclass
class _Bundle:
    executor: GcExecutor
    cas: FilesystemArtifactCas
    manifests: SqliteArtifactManifestRepository
    ledger: SqliteEventLedgerRepository
    factory: ConnectionFactory
    connection: sqlite3.Connection
    content_hash: Any
    root: Path


@pytest.fixture
def bundle(tmp_path: Path) -> Iterator[_Bundle]:
    """tmp_path の中だけで完結する Artifact Store と Ledger を組む。"""
    store_root = tmp_path / "artifacts"
    store_root.mkdir()
    factory = ConnectionFactory(tmp_path / "harness.db")
    migrate(factory, recorded_at="2026-08-21T00:00:00Z")
    made = factory.connect(ConnectionRole.RUNTIME)
    try:
        cas = FilesystemArtifactCas(store_root)
        manifests = SqliteArtifactManifestRepository(made)
        store = ArtifactStore(cas, manifests)
        with factory.begin_immediate(made):
            record = store.put(
                PAYLOAD,
                ArtifactMetadata(
                    media_type="application/octet-stream",
                    size_bytes=len(PAYLOAD),
                    data_classification="INTERNAL",
                    trust_level="UNTRUSTED",
                ),
                artifact_id=ARTIFACT_ID,
                stored_at="2026-08-21T00:00:00Z",
            )
        yield _Bundle(
            executor=GcExecutor(
                cas=cas,
                manifests=manifests,
                ledger=SqliteEventLedgerRepository(made),
                unit_of_work=SqliteUnitOfWork(factory, made),
                clock=FrozenClock(),
            ),
            cas=cas,
            manifests=manifests,
            ledger=SqliteEventLedgerRepository(made),
            factory=factory,
            connection=made,
            content_hash=record.content_hash,
            root=store_root,
        )
    finally:
        made.close()


def _plan(*, payload_present: bool = True, age: int = 10_000_000) -> Any:
    """Planner を実際に走らせて候補を得る。候補を手で作らない。"""
    return plan_garbage_collection(
        [
            ArtifactRecord(
                artifact_id=ARTIFACT_ID,
                reference_count=0,
                age_seconds=age,
                payload_present=payload_present,
            )
        ],
        gc_result_id=GC_RESULT_ID,
        minimum_age_seconds=1,
        dry_run=False,
    )


def _request(bundle: _Bundle, plan: Any) -> GcExecuteRequest:
    return GcExecuteRequest(
        stream_id=STREAM_ID,
        gc_result_id=GC_RESULT_ID,
        plan=plan,
        content_hashes={ARTIFACT_ID: bundle.content_hash},
    )


# --------------------------------------------------------------------------
# AT-GC-001/RAW_RETENTION
# --------------------------------------------------------------------------


@pytest.mark.case("AT-GC-001/RAW_RETENTION")
def test_raw_payload_is_deleted_while_the_manifest_is_retained(
    bundle: _Bundle, case_observation: Any
) -> None:
    """Payloadだけを消し、Manifestは残す。削除Eventを正本Ledgerへ残す。

    6項目すべてを実測する。Plannerの`ACCEPTED`をコピーしない。
    """
    view = RealLedgerView(bundle.ledger, STREAM_ID)
    head_before = view.head

    plan = _plan()
    assert plan.deleted_ids == (ARTIFACT_ID,), "Plannerが候補を挙げていない"
    # Planner は「候補を挙げた」だけ。ここではまだ何も消えていない。
    assert bundle.cas.object_path(bundle.content_hash).is_file()

    outcome = bundle.executor.execute(_request(bundle, plan))

    # --- 6項目を Filesystem・Manifest・Ledger から読み戻す -----------------
    payload_path = bundle.cas.object_path(bundle.content_hash)
    record = bundle.manifests.find_by_content_hash(bundle.content_hash)
    appended = view.appended

    assert outcome.payload_deleted is True
    assert not payload_path.is_file(), "Bytesが残っている"
    assert outcome.manifest_retained is True
    assert record is not None, "Manifestが消えている"
    assert record.payload_deleted is True
    assert outcome.deletion_event_count == 1
    assert appended == ["ARTIFACT_PAYLOAD_DELETED"]
    assert outcome.ledger_head_before == head_before
    assert outcome.ledger_head_after == view.head
    assert outcome.reconciliation_required is False

    effects = SideEffectProbe()
    observe_case(
        case_observation,
        "AT-GC-001/RAW_RETENTION",
        state=outcome.state,
        subject_id=outcome.gc_result_id,
        error_code=None,
        ledger=view,
        effects=effects,
        head_before=head_before,
        payload={
            "payload_deleted": outcome.payload_deleted,
            "manifest_retained": outcome.manifest_retained,
            "deletion_event_count": outcome.deletion_event_count,
            "deleted_content_hashes": list(outcome.deleted_content_hashes),
            "declared_fault_point": DECLARED_FAULT_POINT,
            "actual_injection_point": ACTUAL_INJECTION_POINT,
            "producer_module": "harness.application.gc_executor",
            "producer_symbol": "GcExecutor.execute",
        },
    )


# --------------------------------------------------------------------------
# Planner／Executor 境界
# --------------------------------------------------------------------------


def test_planner_alone_deletes_nothing(bundle: _Bundle) -> None:
    """Plannerを走らせただけではBytesもEventも動かない。

    PlannerのStateが`ACCEPTED`でも、それは候補を挙げた証拠でしかない。
    """
    plan = _plan()
    assert plan.state == "ACCEPTED"
    assert bundle.cas.object_path(bundle.content_hash).is_file()
    assert RealLedgerView(bundle.ledger, STREAM_ID).appended == []


def test_executor_refuses_a_referenced_artifact(bundle: _Bundle) -> None:
    """参照中のArtifactを消さない。矛盾した計画は入力契約違反である。"""
    plan = _plan()
    contradictory = type(plan)(
        gc_result_id=plan.gc_result_id,
        state=plan.state,
        orphan_candidate_ids=plan.orphan_candidate_ids,
        deleted_ids=(ARTIFACT_ID,),
        referenced_kept_ids=(ARTIFACT_ID,),
    )
    with pytest.raises(ValueError, match="referenced"):
        bundle.executor.execute(_request(bundle, contradictory))
    assert bundle.cas.object_path(bundle.content_hash).is_file()


def test_referenced_artifact_is_never_a_candidate(bundle: _Bundle) -> None:
    """参照数が1以上ならPlannerが候補にしない。"""
    plan = plan_garbage_collection(
        [
            ArtifactRecord(
                artifact_id=ARTIFACT_ID,
                reference_count=1,
                age_seconds=10_000_000,
                payload_present=True,
            )
        ],
        gc_result_id=GC_RESULT_ID,
        minimum_age_seconds=1,
        dry_run=False,
    )
    assert plan.deleted_ids == ()
    outcome = bundle.executor.execute(_request(bundle, plan))
    assert outcome.deletion_event_count == 0
    assert bundle.cas.object_path(bundle.content_hash).is_file()


# --------------------------------------------------------------------------
# 安全性と失敗境界
# --------------------------------------------------------------------------


def test_double_deletion_is_refused(bundle: _Bundle) -> None:
    """同じPayloadを2度消さない。削除Eventを2件残さない。"""
    plan = _plan()
    bundle.executor.execute(_request(bundle, plan))
    before = RealLedgerView(bundle.ledger, STREAM_ID).appended
    assert before == ["ARTIFACT_PAYLOAD_DELETED"]

    with pytest.raises(HarnessError) as error:
        bundle.executor.execute(_request(bundle, plan))
    assert error.value.code is ErrorCode.EFFECT_UNKNOWN
    assert RealLedgerView(bundle.ledger, STREAM_ID).appended == before


def test_missing_manifest_stops_before_deleting(bundle: _Bundle) -> None:
    """Manifestが無いものを消さない。判定できないので止める。"""
    plan = _plan()
    with bundle.factory.begin_immediate(bundle.connection):
        bundle.connection.execute("DELETE FROM artifact_manifest")

    with pytest.raises(HarnessError) as error:
        bundle.executor.execute(_request(bundle, plan))
    assert error.value.code is ErrorCode.EFFECT_UNKNOWN
    assert bundle.cas.object_path(bundle.content_hash).is_file(), "止めたのにBytesが消えている"
    assert RealLedgerView(bundle.ledger, STREAM_ID).appended == []


def test_corrupted_payload_stops_before_deleting(bundle: _Bundle) -> None:
    """CAS検証に失敗したら消さない。改ざんか破損かを判定できない。"""
    plan = _plan()
    bundle.cas.object_path(bundle.content_hash).write_bytes(b"tampered\n")

    with pytest.raises(HarnessError) as error:
        bundle.executor.execute(_request(bundle, plan))
    assert error.value.code is ErrorCode.ARTIFACT_CONTENT_CONFLICT
    assert bundle.cas.object_path(bundle.content_hash).is_file()
    assert RealLedgerView(bundle.ledger, STREAM_ID).appended == []


def test_already_absent_payload_becomes_a_reconciliation_target(bundle: _Bundle) -> None:
    """Bytesが先に消えていた場合、成功Evidenceを作らずReconciliationへ回す。

    「削除後・Commit前に落ちた」残骸と同じ形である。Manifestは
    `payload_deleted=0` のままでBytesが無い。
    """
    plan = _plan()
    bundle.cas.object_path(bundle.content_hash).unlink()

    outcome = bundle.executor.execute(_request(bundle, plan))

    assert outcome.state == "REJECTED"
    assert outcome.error_code is ErrorCode.EFFECT_UNKNOWN
    assert outcome.reconciliation_required is True
    assert outcome.deletion_event_count == 0
    assert outcome.events == ()
    assert RealLedgerView(bundle.ledger, STREAM_ID).appended == []
    record = bundle.manifests.find_by_content_hash(bundle.content_hash)
    assert record is not None and record.payload_deleted is False


def test_ledger_commit_failure_after_deletion_is_reconciliation(
    bundle: _Bundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """削除後にLedger Commitが失敗したら成功にしない。

    Bytesは消えているがEventが無い。**この残骸は検出できる形にする。**
    """
    plan = _plan()

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("ledger commit failed")

    monkeypatch.setattr(bundle.executor, "_append", explode)
    outcome = bundle.executor.execute(_request(bundle, plan))

    assert outcome.state == "REJECTED"
    assert outcome.reconciliation_required is True
    assert outcome.deletion_event_count == 0
    # Bytesは消えた。Manifestは payload_deleted=0 のまま。この不一致が残骸である。
    assert not bundle.cas.object_path(bundle.content_hash).is_file()
    record = bundle.manifests.find_by_content_hash(bundle.content_hash)
    assert record is not None and record.payload_deleted is False
    assert RealLedgerView(bundle.ledger, STREAM_ID).appended == []


def test_event_metadata_carries_no_payload_body(bundle: _Bundle) -> None:
    """削除EventへPayload本文・Secret・PIIを入れない。Content Hashだけを持つ。"""
    plan = _plan()
    bundle.executor.execute(_request(bundle, plan))
    entries = bundle.ledger.load_stream(STREAM_ID)
    assert len(entries) == 1
    serialized = repr(entries[0])
    assert PAYLOAD.decode() not in serialized
    assert "raw provider output" not in serialized
