from __future__ import annotations

from pathlib import Path

import pytest

from harness.domain.attempt import ActionAttempt
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_canonical
from harness.infrastructure.sqlite.action_attempt_repository import SqliteActionAttemptRepository
from harness.infrastructure.sqlite.connection_factory import ConnectionFactory, ConnectionRole
from harness.infrastructure.sqlite.migrations import migrate

pytestmark = pytest.mark.integration


def _hash(label: str):
    return hash_canonical({"label": label}, artifact_type="test-value", schema_major=1)


@pytest.fixture
def factory(tmp_path: Path) -> ConnectionFactory:
    made = ConnectionFactory(tmp_path / "harness.db")
    migrate(made, recorded_at="2026-08-15T00:00:00Z")
    return made


def test_action_attempt_is_saved_and_advanced_by_store_version_cas(
    factory: ConnectionFactory,
) -> None:
    connection = factory.connect(ConnectionRole.RUNTIME)
    try:
        repository = SqliteActionAttemptRepository(connection)
        planning = ActionAttempt(attempt_id="attempt-1", action_id="action-1", attempt_number=1)
        with factory.begin_immediate(connection):
            repository.create(planning)
        planned = planning.with_plan(_hash("content"), _hash("execution"))
        with factory.begin_immediate(connection):
            repository.update(planned, expected_store_version=1)
        loaded = repository.get("attempt-1")
        assert loaded == planned
        with pytest.raises(HarnessError) as stale, factory.begin_immediate(connection):
            repository.update(planned.ready(), expected_store_version=1)
        assert stale.value.code is ErrorCode.EVENT_ORDER_VIOLATION
    finally:
        connection.close()
