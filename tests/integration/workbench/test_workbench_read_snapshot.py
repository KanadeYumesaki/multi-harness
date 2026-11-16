"""Session and Ledger must form one snapshot during an actual competing update."""

from __future__ import annotations

from dataclasses import replace

import pytest

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_bytes

from .conftest import WorkbenchEnv

pytestmark = pytest.mark.integration


def created(env: WorkbenchEnv):
    return env.gateway.create_session(
        provider_id="codex",
        model_id="fake-model",
        relative_path="hello.py",
        instruction="説明コメントを追加してください。",
    )


def test_worker_commit_between_record_and_artifact_read_is_not_a_false_tamper(
    workbench: WorkbenchEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = created(workbench)
    request = workbench.gateway._service
    worker = workbench.gateway._worker
    record = request.sessions.get(original["session_id"])
    assert record is not None
    get = request.artifacts.get
    seen = []

    def interleaved(reference):
        raw = get(reference)
        if reference == record.document_hash and not seen:
            seen.append(request.uow.in_transaction())
            worker.approve_send(
                original["session_id"],
                execution_plan_hash=original["execution_plan_hash"],
                auth_session=workbench.gateway._auth_session,
            )
        return raw

    monkeypatch.setattr(request.artifacts, "get", interleaved)
    observed = request.inspect(original["session_id"])
    assert observed["version"] == original["version"]
    assert observed["state"] == original["state"]
    assert seen == [False], "CAS and planner work must remain outside the short owned snapshot"
    current = request.inspect(original["session_id"])
    assert current["state"] == "SEND_APPROVED"
    assert current["version"] == original["version"] + 1
    assert not request.uow.in_transaction()
    assert workbench.runner.calls == []


def test_inspect_preserves_an_existing_caller_owned_transaction(workbench: WorkbenchEnv) -> None:
    original = created(workbench)
    service = workbench.gateway._service
    with service.uow.begin_immediate():
        assert service.inspect(original["session_id"])["state"] == original["state"]
        assert service.uow.in_transaction()
    assert not service.uow.in_transaction()


def test_consistent_snapshot_still_rejects_a_mismatched_ledger_head(
    workbench: WorkbenchEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = created(workbench)
    service = workbench.gateway._service
    load = service.ledger.load_stream
    observed = []

    def changed(stream_id, **kwargs):
        events = load(stream_id, **kwargs)
        observed.append(service.uow.in_transaction())
        return [*events[:-1], replace(events[-1], payload_hash=hash_bytes(b"changed proof"))]

    monkeypatch.setattr(service.ledger, "load_stream", changed)
    with pytest.raises(HarnessError) as failure:
        service.inspect(original["session_id"])
    assert failure.value.code is ErrorCode.EVENT_ORDER_VIOLATION
    assert observed == [True]
    assert not service.uow.in_transaction()
    assert workbench.runner.calls == []
