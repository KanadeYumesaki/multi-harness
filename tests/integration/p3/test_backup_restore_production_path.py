"""CC-RC-02: Backup/Restore の本番経路（Drain・Effect遮断・Operator入口）。

## 独立Probeと本番経路を分ける

`tools/measure_backup_restore.py` は SQLite/CAS の往復だけを見る独立Probeで、
自分でも `release_area_status: UNVERIFIED` と `unmeasured` を宣言している。
ここで確かめるのは**Production入口から観測できること**である。

| 項目 | Production入口 | 観測元 |
|---|---|---|
| `application_drain` | `deploy drain --check` | `operation_journal` と `approval_grant` の実数 |
| `effects_during_restore` | `backup verify` | Restore区間の Guard の**実呼出し回数** |
| `operator_restore_workflow` | 上記CLI3種の連結 | 実returncodeと出力 |

## 0 を書かない

`effects_during_restore == 0` は「Guardを張った状態で1度も呼ばれなかった」を
意味する。同じGuardが**実際に遮断して数える**ことを別Caseで確かめてあるので、
0 が「測っていない」ではないと言える。

## 合成環境だけを使う

すべて `tmp_path` に新規作成したDB/CASである。**既存ユーザーDBを開かない。**
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
STAMP = "2026-09-16T12:00:00Z"

from harness.application.backup_restore_service import (  # noqa: E402
    BackupRestoreService,
    RestoreEffectBlocked,
    RestoreGuard,
)
from harness.application.intake_gate import IntakeRefused  # noqa: E402
from harness.domain._registry_generated import EventType  # noqa: E402
from harness.domain.artifact import ArtifactMetadata  # noqa: E402
from harness.domain.hashing import hash_bytes  # noqa: E402
from harness.domain.input_read import CapabilityScope  # noqa: E402
from harness.infrastructure.artifact.filesystem_cas import FilesystemArtifactCas  # noqa: E402
from harness.infrastructure.artifact.store import ArtifactStore  # noqa: E402
from harness.infrastructure.restore_probe import (  # noqa: E402
    build_restore_window_probe,
    restore_window_probe_request,
)
from harness.infrastructure.runtime_facade import (  # noqa: E402
    HarnessRuntimeService,
    TaskPlanSetup,
)
from harness.infrastructure.sqlite.artifact_manifest_repository import (  # noqa: E402
    SqliteArtifactManifestRepository,
)
from harness.infrastructure.sqlite.connection_factory import ConnectionFactory  # noqa: E402
from harness.infrastructure.sqlite.event_ledger_repository import (  # noqa: E402
    SqliteEventLedgerRepository,
)
from harness.infrastructure.sqlite.migrations import migrate  # noqa: E402
from harness.infrastructure.sqlite.operations_repository import (  # noqa: E402
    ACTIVE_JOURNAL_STATES,
    RestoreVerificationError,
    SqliteDrainInspection,
    SqliteRestoreTarget,
    create_backup,
)
from harness.ports.event_ledger import NewEvent  # noqa: E402


class _FixedClock:
    def now(self) -> str:
        return STAMP


def _seed(database: Path, cas_root: Path) -> None:
    """合成データだけを作る。ユーザーDBは一切開かない。"""
    factory = ConnectionFactory(database)
    migrate(factory, recorded_at=STAMP)
    connection = factory.connect()
    try:
        store = ArtifactStore(
            FilesystemArtifactCas(cas_root), SqliteArtifactManifestRepository(connection)
        )
        ledger = SqliteEventLedgerRepository(connection)
        with factory.begin_immediate(connection):
            for index, payload in enumerate((b"cc-rc-02 alpha\n", b"cc-rc-02 beta\n")):
                store.put(
                    payload,
                    ArtifactMetadata(
                        media_type="text/plain",
                        size_bytes=len(payload),
                        data_classification="INTERNAL",
                        trust_level="VERIFIED_INTERNAL",
                    ),
                    artifact_id=f"synthetic-{index}",
                    stored_at=STAMP,
                )
                ledger.append(
                    [
                        NewEvent(
                            stream_id=f"synthetic-{index}",
                            event_type=event_type,
                            payload_hash=hash_bytes(payload),
                            recorded_at=STAMP,
                        )
                        for event_type in ("RUN_CREATED", "INTENT_CREATED", "PLAN_RESOLVED")
                    ],
                    expected_stream_sequence=0,
                )
    finally:
        connection.close()


def _insert_active_journal(database: Path, state: str) -> None:
    """進行中の `operation_journal` を1件入れる。**Drainが数える対象である。**"""
    connection = ConnectionFactory(database).connect()
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO operation_journal (
                    operation_journal_id, operation_id, effect_id, run_id, action_id, attempt_id,
                    operation_type, target_resource_identity, fencing_token, before_hash,
                    expected_after_hash, prepared_event_id, prepared_at, durability_level, state,
                    store_version
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"oj-{state}",
                    f"op-{state}",
                    f"eff-{state}",
                    "run-1",
                    "act-1",
                    "att-1",
                    "WORKSPACE_COMMIT",
                    "synthetic://target",
                    1,
                    "sha256:" + "0" * 64,
                    "sha256:" + "1" * 64,
                    "ev-1",
                    STAMP,
                    "T2",
                    state,
                    1,
                ),
            )
    finally:
        connection.close()


def _service(source: Path, restore_db: Path, restore_cas: Path) -> BackupRestoreService:
    return BackupRestoreService(
        drain=SqliteDrainInspection(source),
        target=SqliteRestoreTarget(database=restore_db, cas_root=restore_cas),
        clock=_FixedClock(),
    )


def _cli(*args: str) -> tuple[int, str]:
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "harness.presentation.cli", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, done.stdout.strip() or done.stderr.strip()


# ==========================================================================
# application_drain
# ==========================================================================


def test_drain_counts_the_real_store_not_a_probe_constant(tmp_path: Path) -> None:
    """Drainの数は**実DBを数えて**得ること。内訳と観測元を残す。"""
    database = tmp_path / "source.sqlite3"
    _seed(database, tmp_path / "cas")
    _insert_active_journal(database, "PREPARED_DURABLE")

    snapshot = SqliteDrainInspection(database).inspect()
    assert snapshot.active_run_count == 1
    assert snapshot.active_states == (("PREPARED_DURABLE", 1),)
    # どのDBを数えたかがEvidenceから読めること。
    assert snapshot.source == str(database)
    assert snapshot.observed_at.endswith("Z")
    assert not snapshot.quiesced


@pytest.mark.parametrize("state", ACTIVE_JOURNAL_STATES)
def test_every_non_terminal_state_blocks_deploy(tmp_path: Path, state: str) -> None:
    """終端でないstateはすべて進行中として数える。**EFFECT_UNKNOWNも含む。**"""
    database = tmp_path / "source.sqlite3"
    _seed(database, tmp_path / "cas")
    _insert_active_journal(database, state)

    service = _service(database, tmp_path / "r.sqlite3", tmp_path / "r-cas")
    outcome = service.drain_check(deployment_result_id="dep-1")
    assert outcome.verdict.state == "REJECTED"
    assert outcome.verdict.error_code is not None
    assert outcome.verdict.error_code.value == "DEPLOY_DRAIN_REQUIRED"
    # 拒否したのだからMigrationへ進まない。
    assert outcome.verdict.migration_started is False
    # 待っても捌けなかったので期限超過として残す。安全側で止めている。
    assert outcome.deadline_exceeded is True
    assert outcome.intake_stopped is True


def test_quiesced_store_allows_deploy(tmp_path: Path) -> None:
    """進行中も承認待ちも無ければDeployを許す。"""
    database = tmp_path / "source.sqlite3"
    _seed(database, tmp_path / "cas")
    service = _service(database, tmp_path / "r.sqlite3", tmp_path / "r-cas")
    outcome = service.drain_check(deployment_result_id="dep-ok")
    assert outcome.verdict.state == "ACCEPTED"
    assert outcome.verdict.migration_started is True
    assert outcome.deadline_exceeded is False
    assert outcome.snapshot.quiesced


def test_pending_approval_blocks_deploy(tmp_path: Path) -> None:
    """承認待ちが残っていれば拒否する。人がまだ決めていない。"""
    database = tmp_path / "source.sqlite3"
    _seed(database, tmp_path / "cas")
    connection = ConnectionFactory(database).connect()
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO approval_grant (
                    grant_id, nonce, plan_content_hash, execution_plan_hash, action_scope,
                    approver_subject_id, approver_tenant_id, authentication_context_class,
                    mfa_performed, authentication_time, issued_at, not_before, expires_at,
                    maximum_clock_skew_seconds, revocation_epoch, issuer_id, issuer_key_id,
                    signature_algorithm, signature, status, store_version
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "g-1",
                    "n-1",
                    "sha256:" + "0" * 64,
                    "sha256:" + "1" * 64,
                    "WORKSPACE_COMMIT",
                    "subject-1",
                    "tenant-1",
                    "AAL2",
                    1,
                    STAMP,
                    STAMP,
                    STAMP,
                    STAMP,
                    60,
                    0,
                    "issuer-1",
                    "key-1",
                    "ed25519",
                    "sig",
                    "ISSUED",
                    1,
                ),
            )
    finally:
        connection.close()

    service = _service(database, tmp_path / "r.sqlite3", tmp_path / "r-cas")
    outcome = service.drain_check(deployment_result_id="dep-2")
    assert outcome.verdict.state == "REJECTED"
    assert outcome.snapshot.pending_approval_count == 1
    assert outcome.snapshot.pending_statuses == (("ISSUED", 1),)


# ==========================================================================
# effects_during_restore
# ==========================================================================


def test_restore_guard_refuses_and_counts_every_effect_port() -> None:
    """遮断は**拒否したうえで数える**。数えるだけで通さない。"""
    guard = RestoreGuard()
    for call in (
        lambda: guard.send("https://example.invalid", b"x"),
        lambda: guard.launch(["/bin/true"]),
        lambda: guard.commit("a.txt", b"x"),
        lambda: guard.reserve_budget(1),
        lambda: guard.invoke_provider("r-1", "prompt"),
    ):
        with pytest.raises(RestoreEffectBlocked):
            call()
    assert guard.effect_count == 5
    assert {a.port for a in guard.attempts} == {
        "ExternalSendPort",
        "ProcessLaunchPort",
        "WorkspaceWritePort",
        "PaidExecutionPort",
    }
    # Secret が混ざる prompt 本文を記録していないこと（不変条件#7）。
    assert all("prompt" not in a.detail or "prompt_len=" in a.detail for a in guard.attempts)


def test_normal_restore_observes_zero_effects(tmp_path: Path) -> None:
    """正常復元。**Guardを張った状態で 0 件**であることを観測する。"""
    database = tmp_path / "source.sqlite3"
    cas_root = tmp_path / "cas"
    _seed(database, cas_root)
    backup = create_backup(
        source_database=database,
        source_cas=cas_root,
        destination_database=tmp_path / "backup.sqlite3",
        destination_cas=tmp_path / "backup-cas",
        backup_id="bkp-1",
        created_at=STAMP,
    )
    service = _service(database, tmp_path / "restored.sqlite3", tmp_path / "restored-cas")
    outcome = service.restore_and_verify(backup=backup, backup_restore_id="br-1")

    assert outcome.result.state == "ACCEPTED"
    assert outcome.effects_during_restore == 0
    assert outcome.result.artifact_manifest_count_equal is True
    assert outcome.result.restored_chain_head == outcome.result.original_chain_head
    assert outcome.restored_artifact_ids == backup.artifact_ids


def test_an_effect_attempted_during_restore_is_blocked_and_fails_the_restore(
    tmp_path: Path,
) -> None:
    """復元中に作用を試みたら、遮断し、かつ Restore を不合格にする。"""
    database = tmp_path / "source.sqlite3"
    cas_root = tmp_path / "cas"
    _seed(database, cas_root)
    backup = create_backup(
        source_database=database,
        source_cas=cas_root,
        destination_database=tmp_path / "backup.sqlite3",
        destination_cas=tmp_path / "backup-cas",
        backup_id="bkp-2",
        created_at=STAMP,
    )
    service = _service(database, tmp_path / "restored.sqlite3", tmp_path / "restored-cas")
    outcome = service.restore_and_verify(
        backup=backup,
        backup_restore_id="br-2",
        during_restore=lambda guard: guard.send("https://example.invalid", b"probe"),
    )

    # 遮断が実際に働いた。変数へ 0 を入れていないことの根拠になる。
    assert outcome.effects_during_restore == 1
    assert outcome.guard_attempts[0].port == "ExternalSendPort"
    # Restore は不合格。作用が起きたならそれは Restore ではない。
    assert outcome.result.state == "REJECTED"
    assert outcome.result.error_code is not None
    assert outcome.result.error_code.value == "BACKUP_RESTORE_FAILED"
    assert "effects executed during restore" in outcome.result.problems


# ==========================================================================
# 途中失敗・不整合
# ==========================================================================


def test_tampered_backup_bytes_are_refused(tmp_path: Path) -> None:
    """Backup自体が改変されていたら復元して一致を主張しない。"""
    database = tmp_path / "source.sqlite3"
    cas_root = tmp_path / "cas"
    _seed(database, cas_root)
    backup = create_backup(
        source_database=database,
        source_cas=cas_root,
        destination_database=tmp_path / "backup.sqlite3",
        destination_cas=tmp_path / "backup-cas",
        backup_id="bkp-3",
        created_at=STAMP,
    )
    target = tmp_path / "backup.sqlite3"
    target.write_bytes(target.read_bytes() + b"\x00tamper")

    service = _service(database, tmp_path / "restored.sqlite3", tmp_path / "restored-cas")
    with pytest.raises(RestoreVerificationError, match="BACKUP_BYTES_TAMPERED"):
        service.restore_and_verify(backup=backup, backup_restore_id="br-3")


def test_missing_backup_input_is_refused(tmp_path: Path) -> None:
    """Backupが無いのに復元成功と書かない。"""
    database = tmp_path / "source.sqlite3"
    cas_root = tmp_path / "cas"
    _seed(database, cas_root)
    backup = create_backup(
        source_database=database,
        source_cas=cas_root,
        destination_database=tmp_path / "backup.sqlite3",
        destination_cas=tmp_path / "backup-cas",
        backup_id="bkp-4",
        created_at=STAMP,
    )
    (tmp_path / "backup.sqlite3").unlink()
    service = _service(database, tmp_path / "restored.sqlite3", tmp_path / "restored-cas")
    with pytest.raises(RestoreVerificationError, match="BACKUP_INPUT_MISSING"):
        service.restore_and_verify(backup=backup, backup_restore_id="br-4")


def test_reading_before_restore_is_refused(tmp_path: Path) -> None:
    """復元していない先を読んで一致と書かない。"""
    target = SqliteRestoreTarget(database=tmp_path / "r.sqlite3", cas_root=tmp_path / "r-cas")
    with pytest.raises(RestoreVerificationError, match="RESTORE_NOT_PERFORMED"):
        target.chain_heads()


def test_a_missing_artifact_payload_breaks_the_manifest_set(tmp_path: Path) -> None:
    """CAS側が欠けた復元をManifest一致として通さない。"""
    database = tmp_path / "source.sqlite3"
    cas_root = tmp_path / "cas"
    _seed(database, cas_root)
    backup = create_backup(
        source_database=database,
        source_cas=cas_root,
        destination_database=tmp_path / "backup.sqlite3",
        destination_cas=tmp_path / "backup-cas",
        backup_id="bkp-5",
        created_at=STAMP,
    )
    # 復元後に参照されるCASを壊す。
    for path in sorted((tmp_path / "backup-cas" / "objects").rglob("*")):
        if path.is_file():
            path.unlink()
            break
    service = _service(database, tmp_path / "restored.sqlite3", tmp_path / "restored-cas")
    with pytest.raises(Exception):  # noqa: B017 - CAS層の例外型は実装依存
        service.restore_and_verify(backup=backup, backup_restore_id="br-5")


# ==========================================================================
# operator_restore_workflow（実在するCLI入口）
# ==========================================================================


def test_operator_cli_reaches_restore_verify_and_resume(tmp_path: Path) -> None:
    """Operator入口から復元・検証・再開判定まで到達すること。

    設計書 §3.12 の `harness backup create` / `backup verify` /
    `deploy drain --check` をそのまま叩く。
    """
    database = tmp_path / "source.sqlite3"
    cas_root = tmp_path / "cas"
    _seed(database, cas_root)

    code, out = _cli(
        "backup",
        "create",
        "--database",
        str(database),
        "--cas-root",
        str(cas_root),
        "--backup-database",
        str(tmp_path / "backup.sqlite3"),
        "--backup-cas-root",
        str(tmp_path / "backup-cas"),
        "--backup-id",
        "bkp-cli",
        "--recorded-at",
        STAMP,
    )
    assert code == 0, out
    created = json.loads(out)
    assert created["backup_id"] == "bkp-cli"
    assert created["artifact_ids"] == ["synthetic-0", "synthetic-1"]

    code, out = _cli(
        "backup",
        "verify",
        "bkp-cli",
        "--backup-database",
        str(tmp_path / "backup.sqlite3"),
        "--backup-cas-root",
        str(tmp_path / "backup-cas"),
        "--restore-database",
        str(tmp_path / "restored.sqlite3"),
        "--restore-cas-root",
        str(tmp_path / "restored-cas"),
        "--source-database",
        str(database),
        "--source-cas-root",
        str(cas_root),
    )
    assert code == 0, out
    verified = json.loads(out)
    assert verified["state"] == "ACCEPTED"
    assert verified["effects_during_restore"] == 0
    assert verified["artifact_manifest_count_equal"] is True

    # 再開判定まで到達する。
    code, out = _cli(
        "deploy", "drain", "--check", "--database", str(database), "--deployment-id", "dep-cli"
    )
    assert code == 0, out
    drained = json.loads(out)
    assert drained["state"] == "ACCEPTED"
    assert drained["intake_stopped"] is True
    assert drained["observation_source"] == str(database)


def test_operator_cli_reports_a_blocked_effect_probe(tmp_path: Path) -> None:
    """`--probe-effect` は遮断を実測する経路であり、Restoreは不合格になる。"""
    database = tmp_path / "source.sqlite3"
    cas_root = tmp_path / "cas"
    _seed(database, cas_root)
    code, out = _cli(
        "backup",
        "create",
        "--database",
        str(database),
        "--cas-root",
        str(cas_root),
        "--backup-database",
        str(tmp_path / "backup.sqlite3"),
        "--backup-cas-root",
        str(tmp_path / "backup-cas"),
        "--backup-id",
        "bkp-probe",
        "--recorded-at",
        STAMP,
    )
    assert code == 0, out

    code, out = _cli(
        "backup",
        "verify",
        "bkp-probe",
        "--backup-database",
        str(tmp_path / "backup.sqlite3"),
        "--backup-cas-root",
        str(tmp_path / "backup-cas"),
        "--restore-database",
        str(tmp_path / "restored.sqlite3"),
        "--restore-cas-root",
        str(tmp_path / "restored-cas"),
        "--source-database",
        str(database),
        "--source-cas-root",
        str(cas_root),
        "--probe-effect",
    )
    assert code == 2, out
    body = json.loads(out)
    assert body["effects_during_restore"] == 1
    assert body["blocked_effect_attempts"] == [{"operation": "send", "port": "ExternalSendPort"}]
    assert body["state"] == "REJECTED"


def test_operator_cli_blocks_deploy_when_a_run_is_active(tmp_path: Path) -> None:
    """進行中Runがある状態のDrain検査は終了値2で拒否すること。"""
    database = tmp_path / "source.sqlite3"
    _seed(database, tmp_path / "cas")
    _insert_active_journal(database, "EFFECT_UNKNOWN")
    code, out = _cli(
        "deploy", "drain", "--check", "--database", str(database), "--deployment-id", "dep-blk"
    )
    assert code == 2, out
    body = json.loads(out)
    assert body["state"] == "REJECTED"
    assert body["error_code"] == "DEPLOY_DRAIN_REQUIRED"
    assert body["events"] == ["ACTION_BLOCKED"]
    assert body["migration_started"] is False
    assert body["observed_active_states"] == [["EFFECT_UNKNOWN", 1]]


# ==========================================================================
# 指摘対応: Guard直叩き / フラグだけ、では完了にしない
# ==========================================================================


def test_drain_makes_the_real_intake_entrypoint_refuse_new_requests(tmp_path: Path) -> None:
    """Drainが**実際の受付入口**を止めること。

     がService内のBool値を立てるだけでは「止めた」ことにならない。
    受付入口（新規Planを組む経路）が実際に拒否することを確かめる。
    """
    database = tmp_path / "source.sqlite3"
    _seed(database, tmp_path / "cas")
    runtime = HarnessRuntimeService(database)
    gate = runtime.intake_gate()

    # Drain前は受け付ける。
    assert gate.evaluate("req-1").admitted is True
    gate.admit("req-1")

    service = runtime.backup_restore_service(
        restore_database=tmp_path / "r.sqlite3", restore_cas=tmp_path / "r-cas"
    )
    outcome = service.drain_check(deployment_result_id="dep-intake")

    # 受付入口の状態として止まっていること。Service内のBool値ではない。
    assert outcome.intake_refuses_new_requests is True
    assert outcome.intake_state_source.endswith("#operation_control")
    assert not list(tmp_path.glob("*.intake-stopped"))

    # **実際の受付入口が拒否する。**
    verdict = gate.evaluate("req-2")
    assert verdict.admitted is False
    assert verdict.error_code is not None
    assert verdict.error_code.value == "DEPLOY_DRAIN_REQUIRED"
    assert verdict.events == (EventType.ACTION_BLOCKED,)
    with pytest.raises(IntakeRefused, match="INTAKE_STOPPED"):
        gate.admit("req-2")

    # 再開したら受け付ける。止めっぱなしにしない。
    gate.resume()
    assert gate.evaluate("req-3").admitted is True


def test_the_task_plan_entrypoint_is_refused_while_draining(tmp_path: Path) -> None:
    """実在の受付入口  がDrain中に拒否されること。

    Gateを通らない入口が増えれば、その入口は止まらない。ここでは
    **Production の受付入口そのもの**が止まることを見る。
    """
    database = tmp_path / "source.sqlite3"
    _seed(database, tmp_path / "cas")
    runtime = HarnessRuntimeService(database)
    runtime.intake_gate().stop()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    setup = TaskPlanSetup(
        repo_root=REPO_ROOT,
        workspace_root=workspace,
        artifact_root=tmp_path / "artifacts",
        capability_id="cap-drain",
        capability_scope=CapabilityScope(allowed_prefixes=("docs",)),
        now=STAMP,
        allow_test_filesystems=True,
    )
    with pytest.raises(IntakeRefused, match="DEPLOY_DRAIN_REQUIRED"):
        runtime.initialize_task_plan(setup)
    # 拒否したのだからDB/CASを作らない。受け付けていない要求の痕跡を残さない。
    assert not (tmp_path / "artifacts").exists()


def test_the_real_effect_path_is_blocked_during_restore(tmp_path: Path) -> None:
    """**本番の作用経路**がRestore区間で遮断されること。

    Guardを直接呼ぶのではなく、Production の  を組んで
     を通す。鮮度は通したうえで  が作用Portへ到達し、
    そこで遮断される。Guard単体の拒否とは別の主張である。
    """
    database = tmp_path / "source.sqlite3"
    cas_root = tmp_path / "cas"
    _seed(database, cas_root)
    backup = create_backup(
        source_database=database,
        source_cas=cas_root,
        destination_database=tmp_path / "backup.sqlite3",
        destination_cas=tmp_path / "backup-cas",
        backup_id="bkp-real",
        created_at=STAMP,
    )
    service = _service(database, tmp_path / "restored.sqlite3", tmp_path / "restored-cas")

    def real_effect_path(guard: RestoreGuard) -> None:
        orchestrator = build_restore_window_probe(guard)
        orchestrator.execute(restore_window_probe_request())

    outcome = service.restore_and_verify(
        backup=backup, backup_restore_id="br-real", during_restore=real_effect_path
    )

    # Orchestrator は鮮度を通してから Port へ届く。そこで止まっている。
    assert outcome.effects_during_restore == 1
    assert outcome.guard_attempts[0].port == "ExternalSendPort"
    assert outcome.guard_attempts[0].operation == "send"
    assert outcome.result.state == "REJECTED"
    assert "effects executed during restore" in outcome.result.problems


def test_the_probe_reaches_the_port_rather_than_stopping_earlier() -> None:
    """Probeが鮮度判定で止まらず、**作用Portまで到達**していること。

    鮮度で止まっていたら Guard は呼ばれず、 は 0 になる。
    それでは「遮断した」ことにならないので、到達自体を固定する。
    """
    guard = RestoreGuard()
    orchestrator = build_restore_window_probe(guard)
    with pytest.raises(RestoreEffectBlocked):
        orchestrator.execute(restore_window_probe_request())
    # 鮮度の再検証を通ってから Port へ届いている。
    assert orchestrator.revalidations == 1
    assert guard.effect_count == 1
