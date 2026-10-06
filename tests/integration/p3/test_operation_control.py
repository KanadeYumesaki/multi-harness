"""Gateの本番接続と短いTransactionの競合境界。外部通信は行わない。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_backup_restore_production_path import STAMP, _FixedClock, _seed

from harness.application.effect_orchestrator import EffectOrchestrator, EffectRequest
from harness.application.intake_gate import IntakeRefused
from harness.domain.policy_freshness import EffectKind
from harness.infrastructure.runtime_facade import HarnessRuntimeService
from harness.infrastructure.sqlite.connection_factory import ConnectionFactory
from harness.infrastructure.sqlite.migrations import migrate
from harness.infrastructure.sqlite.operations_repository import create_backup


class Spy:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, endpoint: str, payload: bytes) -> int:
        self.calls += 1
        return 0

    def launch(self, argv: list[str]) -> int:
        self.calls += 1
        return 0

    def commit(self, relative_path: str, payload: bytes) -> str:
        self.calls += 1
        return ""

    def reserve_budget(self, tokens: int) -> str:
        self.calls += 1
        return ""

    def invoke_provider(self, reservation_id: str, prompt: str) -> str:
        self.calls += 1
        return ""


@pytest.mark.parametrize("kind", list(EffectKind))
def test_normal_effect_entry_is_refused_inside_real_restore(
    tmp_path: Path, kind: EffectKind
) -> None:
    source, cas = tmp_path / "source.db", tmp_path / "cas"
    _seed(source, cas)
    runtime = HarnessRuntimeService(source)
    backup = create_backup(
        source_database=source,
        source_cas=cas,
        destination_database=tmp_path / "backup.db",
        destination_cas=tmp_path / "backup-cas",
        backup_id="backup",
        created_at=STAMP,
    )
    service = runtime.backup_restore_service(
        restore_database=tmp_path / "restore.db", restore_cas=tmp_path / "restore-cas"
    )
    spy = Spy()
    # 通常Portを持った実行器を事前に構築。Guard Portへ差し替えない。
    effect = EffectOrchestrator(
        clock=_FixedClock(),
        ledger=None,
        external_send=spy,
        process_launch=spy,
        workspace_write=spy,
        paid_execution=spy,
        operation_gate=runtime.intake_gate(),
    )
    request = EffectRequest(
        attempt_id="ordinary",
        stream_id="ordinary",
        effect_kind=kind,
        policy_expires_at="2999-01-01T00:00:00Z",
    )

    def checkpoint(_guard: object) -> None:
        assert (tmp_path / "restore.db").exists()
        assert not (tmp_path / "restore-cas").exists()
        # 復元Threadとは別のDB接続から通常入口へ入る。時刻待ちで順序を作らない。
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(IntakeRefused):
                pool.submit(effect.execute, request).result(timeout=5)

    outcome = service.restore_and_verify(
        backup=backup, backup_restore_id="verify", during_restore=checkpoint
    )
    assert spy.calls == 0
    assert outcome.effects_during_restore == 1
    assert not outcome.result.accepted
    assert not runtime.intake_gate().evaluate("new").admitted
    with pytest.raises(IntakeRefused), runtime.intake_gate().admission("later", kind="effect"):
        pytest.fail("rejected verification reopened effect admission")


def test_admission_reservation_prevents_restore_until_released(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _seed(source, tmp_path / "cas")
    first = HarnessRuntimeService(source).intake_gate()
    second = HarnessRuntimeService(source).intake_gate()
    with first.admission("request"):
        second.stop()
        assert second.active_count() == 1
        with pytest.raises(IntakeRefused), second.restoration():
            pytest.fail("restore overlapped an admitted request")
    with second.restoration():
        with pytest.raises(IntakeRefused), first.admission("other", kind="effect"):
            pytest.fail("effect admitted during restore")
    assert second.active_count() == 0
    assert not list(tmp_path.glob("*.intake-stopped"))


def test_restore_failure_persists_closed_state_across_new_runtime(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _seed(source, tmp_path / "cas")
    gate = HarnessRuntimeService(source).intake_gate()
    with pytest.raises(OSError), gate.restoration():
        raise OSError("synthetic copy interruption")
    fresh = HarnessRuntimeService(source).intake_gate()
    with pytest.raises(IntakeRefused), fresh.admission("effect", kind="effect"):
        pytest.fail("failed restore reopened")
    with pytest.raises(IntakeRefused):
        fresh.resume()


REPO_ROOT = Path(__file__).resolve().parents[3]


def _verify_cli(tmp_path: Path, source: Path) -> tuple[int, str, str]:
    """`harness backup verify` を子Processで起動する。**stdoutとstderrを分けて返す。**"""
    done = subprocess.run(  # noqa: S603 - argv list, no shell, explicit env
        [
            sys.executable,
            "-m",
            "harness.presentation.cli",
            "backup",
            "verify",
            "bkp",
            "--backup-database",
            str(tmp_path / "backup.db"),
            "--backup-cas-root",
            str(tmp_path / "backup-cas"),
            "--restore-database",
            str(tmp_path / "restore.db"),
            "--restore-cas-root",
            str(tmp_path / "restore-cas"),
            "--source-database",
            str(source),
            "--source-cas-root",
            str(tmp_path / "cas"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env={
            "PATH": os.defpath,
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    return done.returncode, done.stdout, done.stderr


def _backup(tmp_path: Path, source: Path) -> None:
    create_backup(
        source_database=source,
        source_cas=tmp_path / "cas",
        destination_database=tmp_path / "backup.db",
        destination_cas=tmp_path / "backup-cas",
        backup_id="bkp",
        created_at=STAMP,
    )


def test_cli_reports_a_refused_restore_start_without_starting_it(tmp_path: Path) -> None:
    """受付予約が残っている間の復元開始は、Codeを添えたJSONで拒否されること。"""
    source = tmp_path / "source.db"
    _seed(source, tmp_path / "cas")
    _backup(tmp_path, source)
    gate = HarnessRuntimeService(source).intake_gate()
    with gate.admission("holding", kind="intake"):
        returncode, stdout, _stderr = _verify_cli(tmp_path, source)

    body = json.loads(stdout)
    assert returncode == 2
    assert body["state"] == "REJECTED"
    assert body["error_code"] == "DEPLOY_DRAIN_REQUIRED"
    assert body["error_type"] == "IntakeRefused"
    # 復元は始まっていない。停止状態も作られていない。
    assert not (tmp_path / "restore.db").exists()
    assert not (tmp_path / "restore-cas").exists()
    assert HarnessRuntimeService(source).intake_gate().evaluate("after").admitted


def test_cli_reports_a_copy_failure_and_keeps_the_stop(tmp_path: Path) -> None:
    """CASコピーの失敗はCodeを添えて非0で終え、RESTORINGを別Runtimeでも保持すること。"""
    source = tmp_path / "source.db"
    _seed(source, tmp_path / "cas")
    _backup(tmp_path, source)
    objects = sorted(p for p in (tmp_path / "backup-cas" / "objects").rglob("*") if p.is_file())
    objects[-1].unlink()

    returncode, stdout, _stderr = _verify_cli(tmp_path, source)

    body = json.loads(stdout)
    assert returncode == 2
    assert body["state"] == "REJECTED"
    assert body["error_code"] == "ARTIFACT_CONTENT_CONFLICT"
    fresh = HarnessRuntimeService(source).intake_gate()
    assert not fresh.evaluate("after").admitted
    with pytest.raises(IntakeRefused), fresh.admission("effect", kind="effect"):
        pytest.fail("a failed CAS copy reopened the intake")


def test_upgrade_keeps_existing_ledger_and_cas(tmp_path: Path) -> None:
    source, cas = tmp_path / "source.db", tmp_path / "cas"
    _seed(source, cas)
    factory = ConnectionFactory(source)
    connection = factory.connect()
    try:
        before = [tuple(row) for row in connection.execute("SELECT * FROM event_ledger")]
        migrate(factory, recorded_at=STAMP)
        assert [tuple(row) for row in connection.execute("SELECT * FROM event_ledger")] == before
        assert connection.execute("SELECT mode FROM operation_control").fetchone()[0] == "OPEN"
    finally:
        connection.close()


def test_v10_upgrade_retains_existing_ledger_and_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.infrastructure.sqlite import migrations

    source, cas = tmp_path / "source.db", tmp_path / "cas"
    with monkeypatch.context() as legacy:
        legacy.setattr(migrations, "SCHEMA_VERSION", 10)
        legacy.setattr(
            migrations,
            "_MIGRATIONS",
            tuple(item for item in migrations._MIGRATIONS if item[0] <= 10),
        )
        _seed(source, cas)
    factory = ConnectionFactory(source)
    connection = factory.connect()
    try:
        assert connection.execute("SELECT MAX(version) FROM schema_migration").fetchone()[0] == 10
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='operation_control'"
            ).fetchone()
            is None
        )
        before = [tuple(row) for row in connection.execute("SELECT * FROM event_ledger")]
        cas_before = {p.relative_to(cas): p.read_bytes() for p in cas.rglob("*") if p.is_file()}
        assert migrate(factory, recorded_at=STAMP) == migrations.SCHEMA_VERSION
        assert [tuple(row) for row in connection.execute("SELECT * FROM event_ledger")] == before
        assert connection.execute("SELECT mode FROM operation_control").fetchone()[0] == "OPEN"
        assert {
            p.relative_to(cas): p.read_bytes() for p in cas.rglob("*") if p.is_file()
        } == cas_before
    finally:
        connection.close()


def test_observed_effect_without_durable_receipt_blocks_deploy(tmp_path: Path) -> None:
    from test_backup_restore_production_path import _insert_active_journal

    source = tmp_path / "source.db"
    _seed(source, tmp_path / "cas")
    _insert_active_journal(source, "EFFECT_OBSERVED")
    runtime = HarnessRuntimeService(source)
    outcome = runtime.backup_restore_service(
        restore_database=tmp_path / "restore.db", restore_cas=tmp_path / "restore-cas"
    ).drain_check(deployment_result_id="missing-receipt")
    assert not outcome.verdict.accepted
    assert ("EFFECT_OBSERVED", 1) in outcome.snapshot.active_states
    with pytest.raises(IntakeRefused), runtime.intake_gate().restoration():
        pytest.fail("restore admitted an effect without its durable receipt")
