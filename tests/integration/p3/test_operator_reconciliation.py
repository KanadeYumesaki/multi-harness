"""Native ownership and approved recovery tests; no HTTP entry or external provider."""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_operator_resume import REPO, inspect, resume, rows, setup_runtime

from harness.domain.errors import HarnessError
from harness.infrastructure.operation_owner import LinuxOperationOwner
from harness.infrastructure.runtime_facade import HarnessRuntimeService
from harness.infrastructure.sqlite.connection_factory import ConnectionFactory
from harness.infrastructure.sqlite.migrations import migrate
from harness.ports.operations import BackupArtifact


def review_recovery(runtime: HarnessRuntimeService, cas: Path) -> dict[str, Any]:
    with runtime.operator_resume(artifact_root=cas, repo_root=REPO) as service:
        return service.inspect("reconcile")


def reconcile(runtime: HarnessRuntimeService, cas: Path, review: dict[str, Any]) -> dict[str, Any]:
    with runtime.operator_resume(artifact_root=cas, repo_root=REPO) as service:
        return service.reconcile(
            review_hash=review["review_hash"],
            auth_session=f"local-uid:{os.getuid()}",
            reason="reconcile-quiescent-source",
        )


def test_kernel_lock_is_exclusive_and_identity_mismatch_is_unknown(tmp_path: Path) -> None:
    database = tmp_path / "owner.db"
    database.write_bytes(b"synthetic unchanged file")
    original = database.read_bytes()
    owner = LinuxOperationOwner(database)
    token = "test-exclusive-" + str(os.getpid())
    with owner.hold(token) as first:
        assert first.status == "ACQUIRED"
        with owner.hold(token, first.scope) as second:
            assert second.status == "ACTIVE"
        with owner.hold(token, "different-boot-or-net-namespace") as unknown:
            assert unknown.status == "UNKNOWN"
    with owner.hold(token, first.scope) as released:
        assert released.status == "ACQUIRED"
    with pytest.raises(OSError, match="operation failure"), owner.hold(token):
        raise OSError("operation failure")
    assert database.read_bytes() == original


def orphan(database: Path) -> None:
    script = """import os,sys
from pathlib import Path
from harness.infrastructure.runtime_facade import HarnessRuntimeService
gate = HarnessRuntimeService(Path(sys.argv[1])).intake_gate()
with gate.admission('synthetic-abrupt-intake', kind='effect'):
    os._exit(17)
"""
    # Fixed Python fixture and pytest-owned DB; no caller-supplied executable or code.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script, str(database)],
        check=False,
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 17, result.stderr


def test_abrupt_process_reservation_reconciles_but_does_not_resume(tmp_path: Path) -> None:
    runtime, database, cas = setup_runtime(tmp_path)
    orphan(database)
    review = review_recovery(runtime, cas)
    assert review["eligible"]
    owners = review["snapshot"]["reservations"]
    assert len(owners) == 1 and owners[0]["ownership"] == "UNHELD"
    assert owners[0]["request_id"] == "synthetic-abrupt-intake"
    result = reconcile(runtime, cas, review)
    assert result["reconciled"] and not result["resumed"]
    assert result["mode"] == "DRAINING"
    assert rows(database, "SELECT COUNT(*) FROM operation_admission") == [(0,)]
    assert rows(database, "SELECT COUNT(*) FROM operation_admission_owner") == [(0,)]
    assert runtime.verify_ledger(result["operation_id"])
    assert not runtime.intake_gate().evaluate("still-stopped").admitted
    with pytest.raises(HarnessError, match="state changed"):
        reconcile(runtime, cas, review)
    assert resume(runtime, cas, inspect(runtime, cas))["resumed"]


def test_live_owner_cannot_be_reconciled(tmp_path: Path) -> None:
    runtime, _, cas = setup_runtime(tmp_path)
    with runtime.intake_gate().admission("active-target", kind="effect"):
        review = review_recovery(runtime, cas)
        assert not review["eligible"]
        assert review["snapshot"]["reservations"][0]["ownership"] == "ACTIVE"
        with pytest.raises(HarnessError):
            reconcile(runtime, cas, review)
        assert runtime.intake_gate().active_count() == 1


@pytest.mark.parametrize("legacy", ["reservation", "restore"])
def test_old_unowned_state_remains_blocked(tmp_path: Path, legacy: str) -> None:
    runtime, database, cas = setup_runtime(tmp_path)
    factory = ConnectionFactory(database)
    conn = factory.connect()
    try:
        with factory.begin_immediate(conn):
            if legacy == "reservation":
                conn.execute("INSERT INTO operation_admission VALUES ('old','effect')")
            else:
                conn.execute("UPDATE operation_control SET mode='RESTORING'")
    finally:
        conn.close()
    before = review_recovery(runtime, cas)
    assert not before["eligible"]
    with pytest.raises(HarnessError):
        reconcile(runtime, cas, before)
    assert review_recovery(runtime, cas) == before


def test_failed_real_copy_can_be_abandoned_only_after_verification(tmp_path: Path) -> None:
    runtime, database, cas = setup_runtime(tmp_path)
    service = runtime.backup_restore_service(
        restore_database=tmp_path / "target.db", restore_cas=tmp_path / "target-cas"
    )
    backup = BackupArtifact(
        "missing",
        str(tmp_path / "absent.db"),
        str(tmp_path / "absent-cas"),
        "sha256:" + "0" * 64,
        "2026-09-24T00:00:00Z",
        (),
        (),
    )
    with pytest.raises(Exception, match="BACKUP_INPUT_MISSING"):
        service.restore_and_verify(backup=backup, backup_restore_id="failed-copy")
    review = review_recovery(runtime, cas)
    assert review["snapshot"]["mode"] == "RESTORING"
    assert review["snapshot"]["restore"]["status"] == "FAILED"
    assert review["eligible"]
    outcome = reconcile(runtime, cas, review)
    assert outcome["restore_verified"] is False and not outcome["resumed"]
    assert rows(database, "SELECT status FROM operation_restore") == [("ABANDONED",)]
    assert not (tmp_path / "target.db").exists()
    assert resume(runtime, cas, inspect(runtime, cas))["resumed"]


def test_live_restore_is_not_abandoned(tmp_path: Path) -> None:
    runtime, _, cas = setup_runtime(tmp_path)
    with runtime.intake_gate().restoration(purpose="VERIFY_COPY"):
        review = review_recovery(runtime, cas)
        assert not review["eligible"]
        with pytest.raises(HarnessError):
            reconcile(runtime, cas, review)


def test_unsettled_journal_prevents_orphan_cleanup(tmp_path: Path) -> None:
    runtime, database, cas = setup_runtime(tmp_path)
    orphan(database)
    factory = ConnectionFactory(database)
    conn = factory.connect()
    h = "sha256:" + "1" * 64
    try:
        with factory.begin_immediate(conn):
            conn.execute(
                "INSERT INTO cli_invocation_journal VALUES (?,?,?,?,?,?,?,?,?)",
                ("i", "s", h, h, h, "EFFECT_UNKNOWN", None, 1, h),
            )
    finally:
        conn.close()
    review = review_recovery(runtime, cas)
    assert "UNSETTLED_JOURNALS_OR_APPROVALS" in review["blockers"]
    with pytest.raises(HarnessError):
        reconcile(runtime, cas, review)
    assert runtime.intake_gate().active_count() == 1


def test_source_drift_cannot_be_approved_away(tmp_path: Path) -> None:
    runtime, database, cas = setup_runtime(tmp_path)
    with pytest.raises(OSError), runtime.intake_gate().restoration(purpose="VERIFY_COPY"):
        raise OSError("interruption")
    # Add a real, durable terminal journal; quiescent does not mean unchanged.
    factory = ConnectionFactory(database)
    conn = factory.connect()
    h = "sha256:" + "1" * 64
    try:
        with factory.begin_immediate(conn):
            conn.execute(
                "INSERT INTO cli_invocation_journal VALUES (?,?,?,?,?,?,?,?,?)",
                ("i", "s", h, h, h, "RESPONSE_CAPTURED", h, 1, h),
            )
    finally:
        conn.close()
    review = review_recovery(runtime, cas)
    assert "RESTORE_SOURCE_CHANGED" in review["blockers"]
    assert review["snapshot"]["unsettled_records"] == 0
    with pytest.raises(HarnessError):
        reconcile(runtime, cas, review)
    assert inspect(runtime, cas)["snapshot"]["mode"] == "RESTORING"


def test_reconciliation_audit_failure_retains_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, database, cas = setup_runtime(tmp_path)
    orphan(database)
    before = review_recovery(runtime, cas)
    with runtime.operator_resume(artifact_root=cas, repo_root=REPO) as service:
        original = service.ledger.append

        def fail(events: Any, **kwargs: Any) -> Any:
            if any(e.event_type == "RECOVERY_DECIDED" for e in events):
                raise OSError("audit failure")
            return original(events, **kwargs)

        monkeypatch.setattr(service.ledger, "append", fail)
        with pytest.raises(OSError):
            service.reconcile(
                review_hash=before["review_hash"],
                auth_session=f"local-uid:{os.getuid()}",
                reason="reconcile-quiescent-source",
            )
    assert review_recovery(runtime, cas) == before
    assert rows(database, "SELECT COUNT(*) FROM approval_grant") == [(0,)]


def test_migration_keeps_legacy_rows_unowned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import harness.infrastructure.sqlite.migrations as module

    database = tmp_path / "old.db"
    factory = ConnectionFactory(database)
    with monkeypatch.context() as old:
        old.setattr(module, "_MIGRATIONS", tuple(row for row in module._MIGRATIONS if row[0] <= 11))
        old.setattr(module, "SCHEMA_VERSION", 11)
        migrate(factory, recorded_at="2026-09-24T00:00:00Z")
    conn = factory.connect()
    try:
        with factory.begin_immediate(conn):
            conn.execute("INSERT INTO operation_admission VALUES ('legacy','intake')")
    finally:
        conn.close()
    assert migrate(factory, recorded_at="2026-09-24T00:00:00Z") == module.SCHEMA_VERSION
    assert rows(database, "SELECT token,kind FROM operation_admission") == [("legacy", "intake")]
    assert rows(database, "SELECT COUNT(*) FROM operation_admission_owner") == [(0,)]


def test_partial_copy_is_preserved_and_never_reported_as_verified(tmp_path: Path) -> None:
    runtime, database, cas = setup_runtime(tmp_path)
    cas.mkdir()
    backup = runtime.create_backup(
        source_cas=cas,
        destination_database=tmp_path / "backup.db",
        destination_cas=tmp_path / "backup-cas",
        backup_id="partial-copy",
        created_at="2026-09-24T00:00:00Z",
    )
    target = tmp_path / "partial.db"
    service = runtime.backup_restore_service(
        restore_database=target, restore_cas=tmp_path / "partial-cas"
    )

    def fail_after_database_copy(_guard: object) -> None:
        assert target.is_file()
        raise OSError("injected failure after database copy")

    with pytest.raises(OSError, match="injected failure"):
        service.restore_and_verify(
            backup=backup,
            backup_restore_id="partial-verify",
            during_restore=fail_after_database_copy,
        )
    partial_bytes = target.read_bytes()
    review = review_recovery(runtime, cas)
    assert review["eligible"] and review["snapshot"]["mode"] == "RESTORING"
    result = reconcile(runtime, cas, review)
    assert result["restore_verified"] is False and result["mode"] == "DRAINING"
    assert target.read_bytes() == partial_bytes
    assert rows(database, "SELECT status FROM operation_restore") == [("ABANDONED",)]


def test_crashed_restore_owner_can_be_reconciled_without_repeating_copy(tmp_path: Path) -> None:
    runtime, database, cas = setup_runtime(tmp_path)
    script = """import os,sys
from pathlib import Path
from harness.infrastructure.runtime_facade import HarnessRuntimeService
with HarnessRuntimeService(Path(sys.argv[1])).intake_gate().restoration(purpose='VERIFY_COPY'):
    os._exit(19)
"""
    # Fixed script and synthetic DB owned by the test.
    child = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script, str(database)],
        check=False,
        capture_output=True,
        timeout=15,
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
    )
    assert child.returncode == 19, child.stderr
    review = review_recovery(runtime, cas)
    assert review["snapshot"]["restore"]["status"] == "ACTIVE"
    assert review["snapshot"]["restore"]["ownership"] == "UNHELD" and review["eligible"]
    assert reconcile(runtime, cas, review)["restore_verified"] is False
    assert rows(database, "SELECT status FROM operation_restore") == [("ABANDONED",)]


def test_directory_owner_survives_sqlite_close_and_fork_until_last_holder_exits(
    tmp_path: Path,
) -> None:
    _runtime, database, _cas = setup_runtime(tmp_path)
    owner = LinuxOperationOwner(database)
    context = multiprocessing.get_context("fork")
    reader, writer = context.Pipe(duplex=False)

    def wait_for_release() -> None:
        reader.recv()

    child = context.Process(target=wait_for_release)
    try:
        with owner.hold("held-across-fork") as held:
            connection = ConnectionFactory(database).connect()
            connection.close()  # POSIX lock cleanup must not release the directory flock.
            with owner.hold("held-across-fork", held.scope) as same:
                assert same.status == "ACTIVE"
            with owner.hold("different-token") as other:
                assert other.status == "ACQUIRED"
            child.start()
        # Parent closed its descriptor. The child still holds the inherited shared lock.
        with owner.hold("held-across-fork", held.scope) as inherited:
            assert inherited.status == "ACTIVE"
        writer.send("release")
        child.join(timeout=5)
        assert child.exitcode == 0
        with owner.hold("held-across-fork", held.scope) as released:
            assert released.status == "ACQUIRED"
    finally:
        if child.is_alive():
            child.kill()
            child.join(timeout=5)
        reader.close()
        writer.close()


def test_replaced_database_identity_and_symlink_cannot_reclaim_owner(tmp_path: Path) -> None:
    database = tmp_path / "owner.db"
    database.write_bytes(b"original")
    owner = LinuxOperationOwner(database)
    with owner.hold("same-token") as held:
        database.rename(tmp_path / "previous.db")
        database.write_bytes(b"different inode")
        with owner.hold("same-token", held.scope) as replaced:
            assert replaced.status == "UNKNOWN"
    link = tmp_path / "linked.db"
    link.symlink_to(database)
    with (
        pytest.raises(HarnessError, match="owner lock unavailable"),
        LinuxOperationOwner(link).hold("token"),
    ):
        pytest.fail("symlink must not be opened")


def test_owner_close_preserves_sqlite_wal_when_another_process_closes(tmp_path: Path) -> None:
    _runtime, database, _cas = setup_runtime(tmp_path)
    factory = ConnectionFactory(database)
    connection = factory.connect()
    try:
        connection.execute("SELECT mode FROM operation_control").fetchall()
        with LinuxOperationOwner(database).hold("wal-regression"):
            pass
        child_code = """import sys
from pathlib import Path
from harness.infrastructure.sqlite.connection_factory import ConnectionFactory
connection = ConnectionFactory(Path(sys.argv[1])).connect()
connection.execute('SELECT mode FROM operation_control').fetchall()
connection.close()
"""
        # The executable and Python fixture are fixed; only a pytest-owned DB path is passed.
        child = subprocess.run(  # noqa: S603
            [sys.executable, "-c", child_code, str(database)],
            check=False,
            capture_output=True,
            timeout=10,
            env={**os.environ, "PYTHONPATH": str(REPO / "src")},
        )
        assert child.returncode == 0, child.stderr
        assert database.with_name(database.name + "-wal").exists()
        assert database.with_name(database.name + "-shm").exists()
        with factory.begin_immediate(connection):
            connection.execute("UPDATE operation_control SET version=version+1")
    finally:
        connection.close()


def test_single_fence_reconciles_multiple_orphans_and_blocks_new_holders(tmp_path: Path) -> None:
    runtime, database, cas = setup_runtime(tmp_path)
    orphan(database)
    orphan(database)
    review = review_recovery(runtime, cas)
    assert review["eligible"] and len(review["snapshot"]["reservations"]) == 2
    with LinuxOperationOwner(database).fence() as fence:
        assert fence.status == "ACQUIRED"
        with LinuxOperationOwner(database).hold("new-owner") as blocked:
            assert blocked.status == "ACTIVE"
    assert reconcile(runtime, cas, review)["reconciled"]
    assert runtime.intake_gate().active_count() == 0
