"""Real two-connection CAS and immutable loser evidence for DCR-1-A."""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest
from test_approval_plan_orchestrator import FrozenClock, _consume, _grant

from harness.application.approval_consume_coordinator import ApprovalConsumeCoordinator
from harness.domain.approval import ApprovalStatus
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_canonical
from harness.domain.schema_set import compute_schema_set_hash
from harness.infrastructure.artifact.filesystem_cas import FilesystemArtifactCas
from harness.infrastructure.artifact.store import ArtifactStore
from harness.infrastructure.schema.registry import CoreSchemaRegistry
from harness.infrastructure.sqlite.approval_consume_repository import SqliteConsumeRepository
from harness.infrastructure.sqlite.approval_grant_repository import SqliteApprovalGrantRepository
from harness.infrastructure.sqlite.artifact_manifest_repository import (
    SqliteArtifactManifestRepository,
)
from harness.infrastructure.sqlite.connection_factory import ConnectionFactory
from harness.infrastructure.sqlite.event_ledger_repository import SqliteEventLedgerRepository
from harness.infrastructure.sqlite.migrations import migrate
from harness.infrastructure.sqlite.unit_of_work import SqliteUnitOfWork

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests/support"))
from approval_observation import ApprovalEffectProbe, record_consume_outcome  # noqa: E402
from case_probe import observe_case  # noqa: E402 - local test support path
from real_ledger_view import RealLedgerView  # noqa: E402 - local test support path

pytestmark = pytest.mark.integration


def coordinator(factory, connection, directory):
    schemas = CoreSchemaRegistry(ROOT)
    return ApprovalConsumeCoordinator(
        clock=FrozenClock(),
        grants=SqliteApprovalGrantRepository(connection),
        store=SqliteConsumeRepository(connection),
        artifacts=ArtifactStore(
            FilesystemArtifactCas(directory), SqliteArtifactManifestRepository(connection)
        ),
        ledger=SqliteEventLedgerRepository(connection),
        uow=SqliteUnitOfWork(factory, connection),
        schema_set_hash=compute_schema_set_hash((schemas.active_ref("ApprovalConsumeResult"),)),
        validate=lambda name, record: schemas.validate_or_raise(
            name, str(record["schema_version"]), record
        ),
    )


@pytest.fixture
def environment(tmp_path):
    factory = ConnectionFactory(tmp_path / "harness.db")
    migrate(factory, recorded_at="2026-08-20T00:00:00Z")
    connection = factory.connect()
    directory = tmp_path / "artifacts"
    directory.mkdir()
    service = coordinator(factory, connection, directory)
    with service.uow.begin_immediate():
        service.grants.issue(_grant())
    try:
        yield factory, connection, directory, service
    finally:
        connection.close()


@pytest.mark.case("AT-APPROVAL-002/CONCURRENT_LOSER")
def test_two_connections_persist_exactly_one_loser_and_one_success(
    environment, case_observation, monkeypatch
):
    factory, connection, directory, reader = environment
    effects = ApprovalEffectProbe()
    effects.install(monkeypatch)
    barrier = Barrier(2, timeout=15)

    def compete(number):
        own = factory.connect()
        try:
            service = coordinator(factory, own, directory)
            request = _consume(
                now="2026-08-20T00:00:00Z", actor=f"worker-{number}", attempt=f"attempt-{number}"
            )
            ticket = service.register(request, "group-1")
            barrier.wait()  # Both admissions are durable before either tries its CAS.
            return service.consume(ticket, request)
        finally:
            own.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(compete, (1, 2)))
    assert sum(x.successful_consumes for x in outcomes) == 1
    assert sum(x.failed_consumes for x in outcomes) == len(outcomes) - 1
    assert all(x.concurrency_group == "group-1" for x in outcomes)
    loser = next(x for x in outcomes if x.failed_consumes)
    assert loser.successful_consumes == 0
    digest = reader.store.result_hash(loser.consume_result_id)
    assert digest == loser.result_hash
    record = json.loads(reader.artifacts.get(digest))
    reader.validate("ApprovalConsumeResult", record)
    assert record["content_hash"] == str(
        hash_canonical(
            {k: v for k, v in record.items() if k != "content_hash"},
            artifact_type="approval-consume-result",
            schema_major=1,
        )
    )
    assert reader.grants.get("grant-1").status is ApprovalStatus.CONSUMED
    view = RealLedgerView(reader.ledger, loser.stream_id)
    assert reader.ledger.verify_chain(loser.stream_id).valid
    assert reader.ledger.load_stream(loser.stream_id)[-1].payload_hash == digest
    record_consume_outcome(case_observation, loser, reader=reader, outcomes=outcomes)
    observe_case(
        case_observation,
        "AT-APPROVAL-002/CONCURRENT_LOSER",
        state=record["state"],
        subject_id=record["consume_result_id"],
        error_code=record["error_code"],
        ledger=view,
        effects=effects,
        head_before=loser.head_before,
        payload={
            **record,
            "producer_module": "harness.application.approval_consume_coordinator",
            "producer_symbol": "ApprovalConsumeCoordinator.consume",
        },
    )


def admitted_pair(service):
    a = _consume(now="2026-08-20T00:00:00Z", attempt="first")
    b = replace(a, attempt_id="second")
    return (service.register(a, "group-1"), a), (service.register(b, "group-1"), b)


@pytest.mark.parametrize("loser_phase", [False, True])
def test_ledger_failure_rolls_back_cas_result_and_ticket(environment, monkeypatch, loser_phase):
    _, connection, _, service = environment
    first, second = admitted_pair(service)
    if loser_phase:
        service.consume(*first)
    head = service.ledger.stream_head("approval:group-1")

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic ledger storage failure")

    monkeypatch.setattr(service.ledger, "append", fail)
    with pytest.raises(RuntimeError, match="synthetic ledger"):
        service.consume(*(second if loser_phase else first))
    grant = service.grants.get("grant-1")
    assert grant.status is (ApprovalStatus.CONSUMED if loser_phase else ApprovalStatus.ISSUED)
    assert service.store.get((second if loser_phase else first)[0].ticket_id).state == "ADMITTED"
    assert connection.execute("SELECT COUNT(*) FROM approval_consume_result").fetchone()[0] == 0
    assert service.ledger.stream_head("approval:group-1") == head


def test_replay_is_not_counted_as_another_concurrent_loser(environment):
    _, connection, _, service = environment
    first, second = admitted_pair(service)
    service.consume(*first)
    loser = service.consume(*second)
    with pytest.raises(HarnessError):
        service.consume(*second)
    with pytest.raises(HarnessError):
        service.register(replace(second[1], attempt_id="later"), "group-1")
    assert connection.execute("SELECT COUNT(*) FROM approval_consume_result").fetchone()[0] == 1
    assert service.store.result_hash(loser.consume_result_id) == loser.result_hash


def test_changed_group_and_request_cannot_reuse_admission(environment):
    _, _, _, service = environment
    first, _ = admitted_pair(service)
    with pytest.raises(HarnessError):
        service.consume(replace(first[0], concurrency_group="other"), first[1])
    with pytest.raises(HarnessError):
        service.consume(first[0], replace(first[1], actor_id="other"))
    assert service.grants.get("grant-1").status is ApprovalStatus.ISSUED


def test_consumption_checks_current_clock_after_admission(environment):
    _, _, _, service = environment
    first, _ = admitted_pair(service)
    service.clock.stamp = "2026-08-22T00:00:00Z"
    with pytest.raises(HarnessError) as error:
        service.consume(*first)
    assert error.value.code is ErrorCode.CLOCK_SKEW_EXCEEDED
    assert service.grants.get("grant-1").status is ApprovalStatus.ISSUED


@pytest.mark.parametrize(
    "field,value",
    [
        ("successful_consumes", 1),
        ("failed_consumes", 0),
        ("state", "CONSUMED"),
        ("error_code", "APPROVAL_REQUIRED"),
        ("schema_set_hash", "invalid"),
    ],
)
def test_loser_schema_rejects_wrong_counter_state_and_binding(environment, field, value):
    _, _, _, service = environment
    first, second = admitted_pair(service)
    service.consume(*first)
    loser = service.consume(*second)
    record = json.loads(service.artifacts.get(loser.result_hash))
    record[field] = value
    with pytest.raises(HarnessError):
        service.validate("ApprovalConsumeResult", record)
