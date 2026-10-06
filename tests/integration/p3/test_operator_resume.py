"""Real DB, signatures, CAS, CLI and transaction failures for operator resume."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from harness.domain.errors import HarnessError
from harness.domain.hashing import ContentHash
from harness.infrastructure.runtime_facade import HarnessRuntimeService
from harness.infrastructure.sqlite.connection_factory import ConnectionFactory
from harness.presentation.cli import main

REPO = Path(__file__).resolve().parents[3]
STAMP = "2026-09-23T00:00:00Z"


def setup_runtime(tmp_path: Path) -> tuple[HarnessRuntimeService, Path, Path]:
    database, cas = tmp_path / "state.db", tmp_path / "cas"
    runtime = HarnessRuntimeService(database)
    runtime.migrate(recorded_at=STAMP)
    runtime.intake_gate().stop()
    return runtime, database, cas


def inspect(runtime: HarnessRuntimeService, cas: Path) -> dict[str, Any]:
    with runtime.operator_resume(artifact_root=cas, repo_root=REPO) as service:
        return service.inspect()


def resume(runtime: HarnessRuntimeService, cas: Path, review: dict[str, Any]) -> dict[str, Any]:
    with runtime.operator_resume(artifact_root=cas, repo_root=REPO) as service:
        return service.resume(
            review_hash=review["review_hash"],
            auth_session=f"local-uid:{os.getuid()}",
            reason="maintenance-complete",
        )


def rows(database: Path, query: str) -> list[tuple[Any, ...]]:
    connection = ConnectionFactory(database).connect()
    try:
        return [tuple(row) for row in connection.execute(query).fetchall()]
    finally:
        connection.close()


def test_signed_resume_commits_receipt_and_opens_real_admission(tmp_path: Path) -> None:
    runtime, database, cas = setup_runtime(tmp_path)
    review = inspect(runtime, cas)
    assert review["eligible"] and not review["resumed"]
    assert not cas.exists()  # Inspect has no CAS, approval or ledger write.
    assert rows(database, "SELECT COUNT(*) FROM approval_grant") == [(0,)]
    result = resume(runtime, cas, review)
    assert result["resumed"] is True
    assert result["review"] == review
    assert rows(database, "SELECT status FROM approval_grant") == [("CONSUMED",)]
    assert runtime.verify_ledger(result["operation_id"])
    assert runtime.verify_ledger("approval:" + result["operation_id"])
    with runtime.operator_resume(artifact_root=cas, repo_root=REPO) as service:
        entries = service.ledger.load_stream(result["operation_id"])
        assert [entry.event_type for entry in entries] == ["RECOVERY_STARTED", "RECOVERY_DECIDED"]
        assert all(str(entry.payload_hash) == result["receipt_hash"] for entry in entries)
        payload = service.artifacts.get(ContentHash.parse(result["receipt_hash"]))
        assert json.loads(payload)["resumed"] is True
        grant = service.grants.get(result["grant_id"])
        assert grant is not None and service.authority.verify(grant, result["public_key"])
    with runtime.intake_gate().admission("real-new-request"):
        assert runtime.intake_gate().active_count() == 1
    with pytest.raises(HarnessError, match="state changed"):
        resume(runtime, cas, review)
    assert rows(database, "SELECT COUNT(*) FROM approval_grant") == [(1,)]


@pytest.mark.parametrize("block", ["RESTORING", "reservation", "approval", "cli-journal"])
def test_unsafe_state_is_preserved_and_no_approval_issued(tmp_path: Path, block: str) -> None:
    runtime, database, cas = setup_runtime(tmp_path)
    factory = ConnectionFactory(database)
    connection = factory.connect()
    try:
        with factory.begin_immediate(connection):
            if block == "RESTORING":
                connection.execute("UPDATE operation_control SET mode='RESTORING'")
            elif block == "reservation":
                connection.execute("INSERT INTO operation_admission VALUES ('orphan','effect')")
            elif block == "cli-journal":
                h = "sha256:" + "1" * 64
                connection.execute(
                    "INSERT INTO cli_invocation_journal VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        "i",
                        "s",
                        h,
                        h,
                        h,
                        "EFFECT_UNKNOWN",
                        None,
                        1,
                        h,
                    ),
                )
    finally:
        connection.close()
    if block == "approval":
        with runtime.operator_resume(artifact_root=cas, repo_root=REPO) as service:
            h = ContentHash.parse("sha256:" + "1" * 64)
            grant, _ = service.authority.issue(
                run_id="pending",
                plan_content_hash=h,
                execution_plan_hash=h,
                scope=("x",),
                subject=f"local-uid:{os.getuid()}",
                now=STAMP,
                expires_at="2999-01-01T00:00:00Z",
            )
            with service.uow.begin_immediate():
                service.grants.issue(grant)
    before = inspect(runtime, cas)
    assert not before["eligible"]
    with pytest.raises(HarnessError, match="resume blocked"):
        resume(runtime, cas, before)
    assert inspect(runtime, cas) == before
    assert not cas.exists()


def test_review_invalidated_by_admission_even_after_it_finishes(tmp_path: Path) -> None:
    runtime, _, cas = setup_runtime(tmp_path)
    review = inspect(runtime, cas)
    with runtime.intake_gate().admission("finishing-existing-action", kind="effect"):
        pass
    with pytest.raises(HarnessError, match="state changed"):
        resume(runtime, cas, review)
    assert inspect(runtime, cas)["snapshot"]["mode"] == "DRAINING"


def test_subject_and_target_binding(tmp_path: Path) -> None:
    runtime, _, cas = setup_runtime(tmp_path)
    review = inspect(runtime, cas)
    with runtime.operator_resume(artifact_root=cas, repo_root=REPO) as service:
        with pytest.raises(HarnessError, match="auth session"):
            service.resume(
                review_hash=review["review_hash"],
                auth_session="local-uid:-1",
                reason="maintenance-complete",
            )
    with pytest.raises(HarnessError, match="state changed"):
        resume(runtime, tmp_path / "other-cas", review)
    assert inspect(runtime, cas) == review


@pytest.mark.parametrize("failure", ["receipt", "ledger"])
def test_audit_failure_rolls_back_open_and_consumed_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    runtime, database, cas = setup_runtime(tmp_path)
    before = inspect(runtime, cas)
    with runtime.operator_resume(artifact_root=cas, repo_root=REPO) as service:

        def fail(*args: Any, **kwargs: Any) -> Any:
            raise OSError("synthetic final audit failure")

        if failure == "ledger":
            original = service.ledger.append

            def append(events: Any, **kwargs: Any) -> Any:
                if any(event.event_type == "RECOVERY_DECIDED" for event in events):
                    return fail()
                return original(events, **kwargs)

            monkeypatch.setattr(service.ledger, "append", append)
        else:
            original_put = service.artifacts.put

            def put(data: bytes, *args: Any, **kwargs: Any) -> Any:
                if json.loads(data).get("contract") == "operator-resume-result/1":
                    return fail()
                return original_put(data, *args, **kwargs)

            monkeypatch.setattr(service.artifacts, "put", put)
        with pytest.raises(OSError):
            service.resume(
                review_hash=before["review_hash"],
                auth_session=f"local-uid:{os.getuid()}",
                reason="maintenance-complete",
            )
    assert inspect(runtime, cas) == before
    assert rows(database, "SELECT COUNT(*) FROM approval_grant") == [(0,)]
    assert rows(database, "SELECT COUNT(*) FROM event_ledger") == [(0,)]


def test_two_operators_can_only_resume_once(tmp_path: Path) -> None:
    runtime, database, cas = setup_runtime(tmp_path)
    review = inspect(runtime, cas)

    def attempt() -> bool:
        try:
            return bool(resume(HarnessRuntimeService(database), cas, review)["resumed"])
        except HarnessError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(results) == [False, True]
    assert rows(database, "SELECT COUNT(*) FROM approval_grant") == [(1,)]


def test_cli_inspect_resume_and_replay(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runtime, database, cas = setup_runtime(tmp_path)
    args = ["--database", str(database), "--artifact-root", str(cas), "--repo-root", str(REPO)]
    assert main(["deploy", "inspect", *args]) == 0
    review = json.loads(capsys.readouterr().out)
    request = [
        "deploy",
        "resume",
        *args,
        "--review-hash",
        review["review_hash"],
        "--auth-session",
        f"local-uid:{os.getuid()}",
        "--reason",
        "maintenance-complete",
    ]
    assert main(request) == 0
    assert json.loads(capsys.readouterr().out)["resumed"] is True
    assert runtime.intake_gate().admit("after-cli").admitted
    assert main(request) == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "APPROVAL_INVALIDATED"


def test_uninitialized_database_is_not_created(tmp_path: Path) -> None:
    database = tmp_path / "missing.db"
    with pytest.raises(HarnessError, match="not initialized"):
        inspect(HarnessRuntimeService(database), tmp_path / "cas")
    assert not database.exists()


@pytest.mark.parametrize("field,value", [("review_hash", "bad"), ("reason", "force")])
def test_invalid_approval_inputs_leave_no_changes(tmp_path: Path, field: str, value: str) -> None:
    runtime, _, cas = setup_runtime(tmp_path)
    before = inspect(runtime, cas)
    request = {
        "review_hash": before["review_hash"],
        "auth_session": f"local-uid:{os.getuid()}",
        "reason": "maintenance-complete",
    }
    request[field] = value
    with runtime.operator_resume(artifact_root=cas, repo_root=REPO) as service:
        with pytest.raises(HarnessError):
            service.resume(**request)
    assert inspect(runtime, cas) == before


def test_invalid_signature_rolls_back_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, database, cas = setup_runtime(tmp_path)
    before = inspect(runtime, cas)
    with runtime.operator_resume(artifact_root=cas, repo_root=REPO) as service:
        monkeypatch.setattr(service.authority, "verify", lambda *args: False)
        with pytest.raises(HarnessError):
            service.resume(
                review_hash=before["review_hash"],
                auth_session=f"local-uid:{os.getuid()}",
                reason="maintenance-complete",
            )
    assert rows(database, "SELECT COUNT(*) FROM approval_grant") == [(0,)]
    assert inspect(runtime, cas) == before


def test_missing_audit_bytes_prevent_another_resume(tmp_path: Path) -> None:
    runtime, _, cas = setup_runtime(tmp_path)
    completed = resume(runtime, cas, inspect(runtime, cas))
    runtime.intake_gate().stop()
    with runtime.operator_resume(artifact_root=cas, repo_root=REPO) as service:
        assert service.artifacts.verify(ContentHash.parse(completed["receipt_hash"])).ok
    wrong_root = tmp_path / "empty-cas"
    review = inspect(runtime, wrong_root)
    assert not review["eligible"]
    assert "AUDIT_ARTIFACTS_UNVERIFIED" in review["blockers"]
    with pytest.raises(HarnessError, match="resume blocked"):
        resume(runtime, wrong_root, review)
    assert not runtime.intake_gate().evaluate("still-stopped").admitted
