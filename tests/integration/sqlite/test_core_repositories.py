"""## Case Adapter はここではない

本 File が持っていた `@pytest.mark.case` は
`tests/integration/sqlite/test_approval_plan_orchestrator.py` と
`tests/integration/sqlite/test_recovery_orchestrator.py` へ移した。
Case は Event 列まで要求するが、本 File は Ledger を観測しない。
Ledger を観測しない試験を Adapter にすると「State だけ一致した Case」になる。

本 File の試験は消していない。Domain／Repository の振る舞いは引き続き検証する。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.domain.approval import ApprovalGrant, ApprovalStatus
from harness.domain.effect import OperationJournal
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash, hash_canonical
from harness.infrastructure.sqlite.approval_grant_repository import SqliteApprovalGrantRepository
from harness.infrastructure.sqlite.connection_factory import ConnectionFactory, ConnectionRole
from harness.infrastructure.sqlite.effect_journal_repository import SqliteEffectJournalRepository
from harness.infrastructure.sqlite.lease_repository import SqliteLeaseRepository
from harness.infrastructure.sqlite.migrations import migrate
from harness.ports.approval import ApprovalConsumeRequest
from harness.ports.lease import LeaseAcquireRequest

pytestmark = pytest.mark.integration

NOW = "2026-08-15T00:00:00Z"


def _hash(label: str) -> ContentHash:
    return hash_canonical({"label": label}, artifact_type="test-value", schema_major=1)


@pytest.fixture
def factory(tmp_path: Path) -> ConnectionFactory:
    made = ConnectionFactory(tmp_path / "harness.db")
    migrate(made, recorded_at="2026-08-15T00:00:00Z")
    return made


def _grant() -> ApprovalGrant:
    return ApprovalGrant(
        grant_id="grant-1",
        plan_content_hash=_hash("content"),
        execution_plan_hash=_hash("execution"),
        action_scope=("action-1",),
        approver_subject_id="operator:local",
        approver_tenant_id="tenant:local",
        authentication_context_class="urn:harness:os-login",
        mfa_performed=False,
        authentication_time=NOW,
        issued_at=NOW,
        not_before=NOW,
        expires_at="2026-08-15T00:10:00Z",
        maximum_clock_skew_seconds=30,
        nonce="0123456789abcdef",
        revocation_epoch=1,
        issuer_id="approval-service:local",
        issuer_key_id="key-1",
        signature_algorithm="ed25519",
        signature="signature-placeholder",
        status=ApprovalStatus.ISSUED,
        store_version=1,
    )


def _journal() -> OperationJournal:
    return OperationJournal(
        operation_journal_id="journal-1",
        operation_id="operation-1",
        effect_id="effect-1",
        run_id="run-1",
        action_id="action-1",
        attempt_id="attempt-1",
        operation_type="LOCAL_FILE_COMMIT",
        target_resource_identity="workspace:demo/file.txt",
        fencing_token=1,
        before_hash=_hash("before"),
        expected_after_hash=_hash("after"),
        prepared_event_id="event-prepared",
        prepared_at=NOW,
        durability_level="STORAGE_SYNC",
    )


def test_approval_consumption_is_atomic_and_single_use(factory: ConnectionFactory) -> None:
    connection = factory.connect(ConnectionRole.RUNTIME)
    try:
        repository = SqliteApprovalGrantRepository(connection)
        with factory.begin_immediate(connection):
            repository.issue(_grant())
        with factory.begin_immediate(connection):
            consumed = repository.consume(
                ApprovalConsumeRequest(
                    grant_id="grant-1",
                    expected_execution_plan_hash=_hash("execution"),
                    current_revocation_epoch=1,
                    actor_id="worker-1",
                    attempt_id="attempt-1",
                    now="2026-08-15T00:01:00Z",
                    signature_valid=True,
                    expected_store_version=1,
                )
            )
        assert consumed.status is ApprovalStatus.CONSUMED
        with pytest.raises(HarnessError) as error, factory.begin_immediate(connection):
            repository.consume(
                ApprovalConsumeRequest(
                    grant_id="grant-1",
                    expected_execution_plan_hash=_hash("execution"),
                    current_revocation_epoch=1,
                    actor_id="worker-2",
                    attempt_id="attempt-2",
                    now="2026-08-15T00:02:00Z",
                    signature_valid=True,
                    expected_store_version=2,
                )
            )
        assert error.value.code is ErrorCode.APPROVAL_REPLAY
    finally:
        connection.close()


@pytest.mark.case("AT-FENCE-001/STALE_WORKER")
def test_lease_fencing_is_monotonic_and_stale_lease_cannot_release(
    factory: ConnectionFactory,
) -> None:
    connection = factory.connect(ConnectionRole.RUNTIME)
    try:
        repository = SqliteLeaseRepository(connection)
        first_request = LeaseAcquireRequest(
            resource_key="workspace:demo",
            holder_id="worker-1",
            attempt_id="attempt-1",
            issued_at=NOW,
            expires_at="2026-08-15T00:10:00Z",
        )
        with factory.begin_immediate(connection):
            first = repository.acquire(first_request)
            repository.release(first, now="2026-08-15T00:01:00Z")
        with factory.begin_immediate(connection):
            second = repository.acquire(
                LeaseAcquireRequest(
                    resource_key="workspace:demo",
                    holder_id="worker-2",
                    attempt_id="attempt-2",
                    issued_at="2026-08-15T00:02:00Z",
                    expires_at="2026-08-15T00:10:00Z",
                )
            )
        assert second.fencing_token == first.fencing_token + 1
        with pytest.raises(HarnessError) as stale, factory.begin_immediate(connection):
            repository.release(first, now="2026-08-15T00:03:00Z")
        assert stale.value.code is ErrorCode.STALE_FENCING_TOKEN
    finally:
        connection.close()


def test_effect_journal_updates_with_history_and_rejects_stale_version(
    factory: ConnectionFactory,
) -> None:
    connection = factory.connect(ConnectionRole.RUNTIME)
    try:
        repository = SqliteEffectJournalRepository(connection)
        journal = _journal()
        with factory.begin_immediate(connection):
            repository.create_prepared(journal)
        attempted = journal.mark_execution_attempted(
            event_id="event-attempted", at="2026-08-15T00:01:00Z"
        )
        with factory.begin_immediate(connection):
            repository.update(attempted, expected_store_version=1)
        with pytest.raises(HarnessError) as stale, factory.begin_immediate(connection):
            repository.update(attempted, expected_store_version=1)
        assert stale.value.code is ErrorCode.STALE_FENCING_TOKEN
        rows = connection.execute(
            "SELECT store_version, state FROM operation_journal_history "
            "WHERE operation_journal_id = ? ORDER BY store_version",
            ("journal-1",),
        ).fetchall()
        assert [(row["store_version"], row["state"]) for row in rows] == [
            (1, "PREPARED_DURABLE"),
            (2, "EXECUTION_ATTEMPTED"),
        ]
    finally:
        connection.close()
