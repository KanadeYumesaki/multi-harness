"""MVP0-A Harness CLI。

危険な実行・承認・Workspace変更はこの入口から直接行わない。Approval Grant、Lease、
Runtime AttestationをApplication Serviceで束縛した統合フローだけが将来追加できる。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from harness.application.task_plan_service import TaskPlanOutcome, TaskPlanRequest
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash, hash_bytes
from harness.domain.timestamps import canonical_timestamp
from harness.infrastructure.restore_probe import (
    build_restore_window_probe,
    restore_window_probe_request,
)
from harness.infrastructure.runtime_facade import HarnessRuntimeService
from harness.ports.operations import BackupArtifact

__all__ = ["main"]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness")
    subcommands = parser.add_subparsers(dest="command", required=True)

    migrate = subcommands.add_parser("migrate", help="SQLite schemaをMigrationする")
    migrate.add_argument("--database", required=True, type=Path)
    migrate.add_argument("--recorded-at", required=True)

    verify = subcommands.add_parser("verify-ledger", help="Event Ledger Hash Chainを検証する")
    verify.add_argument("--database", required=True, type=Path)
    verify.add_argument("--stream-id", required=True)

    # 設計書 §3.12 の Operator 入口。`harness backup create` / `backup verify` /
    # `deploy drain --check`。**ここで新しい契約を作らない。**
    backup = subcommands.add_parser("backup", help="Backupの作成と検証（§27）")
    backup_actions = backup.add_subparsers(dest="backup_command", required=True)
    backup_create = backup_actions.add_parser("create", help="Backupを作成する")
    backup_create.add_argument("--database", required=True, type=Path)
    backup_create.add_argument("--cas-root", required=True, type=Path)
    backup_create.add_argument("--backup-database", required=True, type=Path)
    backup_create.add_argument("--backup-cas-root", required=True, type=Path)
    backup_create.add_argument("--backup-id", required=True)
    backup_create.add_argument("--recorded-at", required=True)
    backup_verify = backup_actions.add_parser(
        "verify", help="Backupを復元して Chain / Manifest 一致を検証する"
    )
    backup_verify.add_argument("backup_id")
    backup_verify.add_argument("--backup-database", required=True, type=Path)
    backup_verify.add_argument("--backup-cas-root", required=True, type=Path)
    backup_verify.add_argument("--restore-database", required=True, type=Path)
    backup_verify.add_argument("--restore-cas-root", required=True, type=Path)
    backup_verify.add_argument("--source-database", required=True, type=Path)
    backup_verify.add_argument("--source-cas-root", required=True, type=Path)
    backup_verify.add_argument(
        "--probe-effect",
        action="store_true",
        help=(
            "合成Probe。RestoreGuardを差し込んだ作用Portを復元中に呼ぶ部品試験で、"
            "通常入口の保護の証跡にはならない（試行があるためRestoreは不合格になる）"
        ),
    )

    deploy = subcommands.add_parser("deploy", help="Deploy前のDrain検査（§3.10）")
    deploy_actions = deploy.add_subparsers(dest="deploy_command", required=True)
    deploy_drain = deploy_actions.add_parser("drain", help="Drain状態を検査する")
    deploy_drain.add_argument("--check", action="store_true", required=True)
    deploy_drain.add_argument("--database", required=True, type=Path)
    deploy_drain.add_argument("--deployment-id", required=True)

    for name, description in (
        ("inspect", "停止状態と再開の承認対象を表示する（変更しない）"),
        ("resume", "確認したHashに対して署名承認し、正常なDrain後の受付を再開する"),
        ("reconcile", "所有者が終了した予約と失敗したコピー検証を承認して整理する"),
    ):
        operator = deploy_actions.add_parser(name, help=description)
        operator.add_argument("--database", required=True, type=Path)
        operator.add_argument("--artifact-root", required=True, type=Path)
        operator.add_argument("--repo-root", type=Path, default=Path.cwd())
        if name == "inspect":
            operator.add_argument("--operation", choices=("resume", "reconcile"), default="resume")
        else:
            operator.add_argument("--review-hash", required=True)
            operator.add_argument(
                "--auth-session", required=True, help="現在のOS利用者 local-uid:<uid>"
            )
            operator.add_argument(
                "--reason",
                required=True,
                choices=(
                    "maintenance-complete" if name == "resume" else "reconcile-quiescent-source",
                ),
            )

    ui = subcommands.add_parser("ui", help="127.0.0.1だけに開くローカルUIを起動する")
    ui.add_argument("--database", required=True, type=Path)
    ui.add_argument("--artifact-root", required=True, type=Path)
    ui.add_argument("--repo-root", type=Path, default=Path.cwd())
    ui.add_argument(
        "--operator-auth-session",
        default=None,
        help="運用操作を有効にする現在のOS利用者 local-uid:<uid>。省略時は参照専用",
    )
    # **`--host` を作らない。** Bind先は定数であり、選ばせない。
    ui.add_argument("--port", type=int, default=0, help="既定は0（OSが空きPortを選ぶ）")
    ui.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="コード編集の対象にする隔離Git Worktree。省略するとWorkbenchは無効",
    )
    ui.add_argument(
        "--cli-runtime-profile",
        type=Path,
        default=None,
        help="公式CLIの実行体を書いたprofile JSON。--workspaceと同時に必要",
    )
    ui.add_argument(
        "--workspace-label",
        default=None,
        help="画面に出すWorkspace名。省略するとDirectory名を使う",
    )

    profile = subcommands.add_parser(
        "workbench-profile", help="導入済み公式CLIからprofile JSONの雛形を書き出す"
    )
    profile.add_argument("--out", required=True, type=Path)
    profile.add_argument(
        "--cli-runtime-root",
        type=Path,
        default=Path.home() / ".local/share/fde-harness/cli-runtime",
    )
    profile.add_argument(
        "--state-dir", type=Path, default=Path.home() / ".local/share/fde-harness/workbench"
    )

    mock = subcommands.add_parser("mock-propose", help="外部通信なしでMock Artifactを生成する")
    mock.add_argument("--database", required=True, type=Path)
    mock.add_argument("--model", required=True)
    mock.add_argument("--instruction-hash", required=True)
    mock.add_argument("--context-bundle-hash", required=True)
    mock.add_argument("--input-artifact-hash", required=True)
    mock.add_argument("--output-schema-hash", required=True)

    plan = subcommands.add_parser(
        "plan-task",
        help="合成TaskとPlan宣言から決定論的ExecutionPlanを組む（Approvalも作用も起こさない）",
    )
    plan.add_argument("--database", required=True, type=Path)
    plan.add_argument("--artifact-root", required=True, type=Path)
    plan.add_argument("--workspace", required=True, type=Path, help="Capabilityを発行するRoot")
    plan.add_argument("--task-path", required=True, help="Workspace相対のTask File")
    plan.add_argument("--declaration-path", required=True, help="Workspace相対のPlan宣言JSON")
    plan.add_argument(
        "--scope",
        action="append",
        required=True,
        metavar="RELATIVE_DIR",
        help="Capabilityが読める相対Directory。複数指定できる",
    )
    plan.add_argument("--context-dir", action="append", default=[], help="列挙する相対Directory")
    plan.add_argument("--compress-context", action="store_true", help="任意候補のMock圧縮を許可")
    plan.add_argument("--run-id", required=True)
    plan.add_argument("--execution-plan-id", required=True)
    plan.add_argument("--bundle-id", required=True)
    plan.add_argument("--receipt-id", required=True)
    plan.add_argument("--stream-id", required=True)
    plan.add_argument("--capability-id", required=True)
    plan.add_argument("--workspace-snapshot-id", required=True)
    plan.add_argument("--issued-at", required=True)
    plan.add_argument("--expires-at", required=True)
    plan.add_argument("--now", required=True, help="Clockが返す値。Plan Contentへは入らない")
    plan.add_argument("--repo-root", type=Path, default=Path.cwd())

    from harness.presentation.local_workflow_cli import configure_workflow, execute_workflow

    configure_workflow(
        subcommands.add_parser("workflow", help="Offline承認・実行・評価・Human Release")
    )
    args = parser.parse_args(argv)
    if args.command == "workflow":
        return execute_workflow(args)
    if args.command == "ui":
        return _serve_ui(
            repo_root=args.repo_root,
            database=args.database,
            artifact_root=args.artifact_root,
            port=args.port,
            workspace=args.workspace,
            runtime_profile=args.cli_runtime_profile,
            workspace_label=args.workspace_label,
            operator_auth_session=args.operator_auth_session,
        )
    if args.command == "workbench-profile":
        return _write_workbench_profile(
            out=args.out, cli_runtime_root=args.cli_runtime_root, state_dir=args.state_dir
        )
    if args.command == "migrate":
        recorded_at = canonical_timestamp(args.recorded_at)
        version = HarnessRuntimeService(args.database).migrate(recorded_at=recorded_at)
        _emit({"schema_version": version})
        return 0
    if args.command == "verify-ledger":
        valid = HarnessRuntimeService(args.database).verify_ledger(args.stream_id)
        _emit({"stream_id": args.stream_id, "valid": valid})
        return 0 if valid else 2
    if args.command == "backup":
        return _backup_command(args)
    if args.command == "deploy":
        return _deploy_command(args)
    if args.command == "mock-propose":
        response = HarnessRuntimeService(args.database).mock_propose(
            model_id=args.model,
            instruction_hash=ContentHash.parse(args.instruction_hash),
            context_bundle_hash=ContentHash.parse(args.context_bundle_hash),
            input_artifact_hash=ContentHash.parse(args.input_artifact_hash),
            output_schema_hash=ContentHash.parse(args.output_schema_hash),
        )
        _emit(
            {
                "provider_id": response.provider_id,
                "model_id": response.model_id,
                "adapter_version": response.adapter_version,
                "artifact_hash": str(response.artifact_hash),
                "network_used": response.network_used,
                "billing_mode": response.billing_mode,
            }
        )
        return 0
    if args.command == "plan-task":
        return _plan_task(args)
    raise AssertionError(f"unhandled command {args.command!r}")


def _plan_task(args: argparse.Namespace) -> int:
    """合成TaskからPlanを組んで結果を出す。**Approvalも作用も起こさない。**

    拒否・不正入力は握り潰さず、Error Codeを添えて非0で終える（不変条件#9）。
    """
    from harness.domain.errors import HarnessError
    from harness.domain.input_read import CapabilityScope
    from harness.infrastructure.runtime_facade import TaskPlanSetup

    service = HarnessRuntimeService(args.database)
    setup = TaskPlanSetup(
        repo_root=args.repo_root.resolve(),
        workspace_root=args.workspace,
        artifact_root=args.artifact_root,
        capability_id=args.capability_id,
        capability_scope=CapabilityScope(tuple(args.scope)),
        now=canonical_timestamp(args.now),
    )
    request = TaskPlanRequest(
        stream_id=args.stream_id,
        capability_id=args.capability_id,
        task_relative_path=args.task_path,
        declaration_relative_path=args.declaration_path,
        task_read_decision_id=f"{args.run_id}:task",
        declaration_read_decision_id=f"{args.run_id}:declaration",
        bundle_id=args.bundle_id,
        receipt_id=args.receipt_id,
        run_id=args.run_id,
        execution_plan_id=args.execution_plan_id,
        issued_at=canonical_timestamp(args.issued_at),
        expires_at=canonical_timestamp(args.expires_at),
        now=canonical_timestamp(args.now),
        workspace_snapshot_id=args.workspace_snapshot_id,
        candidate_directories=tuple(args.context_dir),
        compress_candidates=args.compress_context,
    )
    try:
        service.initialize_task_plan(setup)
        outcome = service.plan_task(setup, request)
    except HarnessError as exc:
        # 「なぜ Plan を組まなかったか」を捨てない。Error Code と Classification を
        # そのまま出す。呼出側が理由を読めなければ、次に何をすべきか決められない。
        _emit(
            {
                "planned": False,
                "error_code": exc.code.value,
                "classification": exc.classification.value,
                "detail": str(exc),
            }
        )
        return 2
    _emit(_plan_report(outcome))
    return 0


def _plan_report(outcome: TaskPlanOutcome) -> dict[str, object]:
    """Plan結果の表示用Projection。**Hashは実物をそのまま出す。**"""
    bundle = outcome.assembly.bundle
    receipt = outcome.assembly.receipt
    return {
        "planned": True,
        "task_format": outcome.task_format.value,
        "classification": outcome.task_read.classification,
        "task_read_state": outcome.task_read.state,
        "ledger_head_before": outcome.task_read.ledger_head_before,
        "ledger_head_after": outcome.task_read.ledger_head_after,
        "context": {
            "bundle_hash": str(bundle.bundle_hash),
            "ordered_fragment_ids": list(bundle.ordered_fragment_ids),
            "total_token_count": bundle.total_token_count,
            "estimated_token_total": receipt.estimated_token_total,
            "candidate_fragment_ids": list(receipt.candidate_fragment_ids),
            "excluded_fragment_ids": list(receipt.excluded_fragment_ids),
            # MVP0-A に圧縮器は無い。常に空であることを隠さずそのまま出す。
            "compression_artifact_ids": list(bundle.compression_artifact_ids),
        },
        "plan": {
            "plan_content_hash": str(outcome.plan.plan_content_hash),
            "execution_plan_hash": str(outcome.plan.execution_plan_hash),
            "action_graph_nodes": list(outcome.plan.action_graph["nodes"]),
        },
        # **承認も作用も起きていないことを明示する。**
        "approval_requested": False,
        "effect_executed": False,
        "provider_invoked": False,
    }


def _write_workbench_profile(*, out: Path, cli_runtime_root: Path, state_dir: Path) -> int:
    """導入済みCLIから profile の雛形を書く。**存在するものだけを書く。**"""
    from harness.infrastructure.provider.cli_profiles import discover_manifest

    document = discover_manifest(state_dir.resolve(), cli_runtime_root.resolve(), Path.home())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _emit(
        {
            "profile": str(out),
            "providers": sorted(document["providers"]),
            "note": "モデルIDは実在確認したものだけを suggested_models へ書く",
        }
    )
    return 0 if document["providers"] else 2


def _serve_ui(
    *,
    repo_root: Path,
    database: Path,
    artifact_root: Path,
    port: int,
    workspace: Path | None = None,
    runtime_profile: Path | None = None,
    workspace_label: str | None = None,
    operator_auth_session: str | None = None,
) -> int:
    """ローカルUIを開く。

    `--workspace` を渡さない限り、外部CLIを起動する経路そのものが存在しない。
    渡した場合も、送信と適用はそれぞれ別の明示承認を消費しないと進まない。
    """
    import os

    from harness.presentation.local_ui import build_services
    from harness.presentation.local_ui.api import LocalUiApi
    from harness.presentation.local_ui.composition import WorkbenchSetup
    from harness.presentation.local_ui.server import start_server

    setup: WorkbenchSetup | None = None
    if workspace is None and runtime_profile is not None:
        sys.stderr.write("--cli-runtime-profile には --workspace の指定が必要です\n")
        return 2
    if workspace is not None:
        resolved_workspace = workspace.resolve()
        setup = WorkbenchSetup(
            workspace=resolved_workspace,
            runtime_profile=None if runtime_profile is None else runtime_profile.resolve(),
            workspace_label=workspace_label or resolved_workspace.name,
            auth_session="local-uid:" + str(os.getuid()),
        )

    services = build_services(
        repo_root=repo_root.resolve(),
        database_path=database.resolve(),
        artifact_root=artifact_root.resolve(),
        workbench=setup,
        operator_auth_session=operator_auth_session,
    )

    def build_api(origin: str, token: str, assets: dict[str, tuple[bytes, str]]) -> LocalUiApi:
        return LocalUiApi(services, origin=origin, session_token=token, assets=assets)

    server = start_server(build_api=build_api, port=port, max_body_bytes=services.max_body_bytes)
    _emit(
        {
            "url": server.url,
            "bind": "127.0.0.1",
            # Workbench を有効にした場合、承認を消費した送信だけが外部通信を起こす。
            "external_network": setup is not None,
            "workbench": None
            if setup is None
            else {
                "workspace": str(setup.workspace),
                "workspace_label": setup.workspace_label,
                "cli_runtime_profile": None
                if setup.runtime_profile is None
                else str(setup.runtime_profile),
                "network_requires_explicit_send_approval": True,
            },
        }
    )
    # `serve_forever()` に入ると終わらない。Flush しないと URL が見えないまま
    # Buffer に残る。
    sys.stdout.flush()
    sys.stderr.write(f"ローカルUI: {server.url}\n")
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        services.close()
    return 0


def _backup_command(args: argparse.Namespace) -> int:
    """`harness backup create` / `harness backup verify <backup-id>`。

    CLIは判定しない。Application Serviceが Domain の判定器へ観測値を渡した
    結果をそのまま出す（`AGENTS.md` §2「CLIはApplication Serviceだけを呼ぶ」）。
    """
    if args.backup_command == "create":
        artifact = HarnessRuntimeService(args.database).create_backup(
            source_cas=args.cas_root,
            destination_database=args.backup_database,
            destination_cas=args.backup_cas_root,
            backup_id=args.backup_id,
            created_at=canonical_timestamp(args.recorded_at),
        )
        _emit(
            {
                "backup_id": artifact.backup_id,
                "database_sha256": artifact.database_sha256,
                "created_at": artifact.created_at,
                "source_chain_heads": [list(pair) for pair in artifact.source_chain_heads],
                "artifact_ids": list(artifact.artifact_ids),
            }
        )
        return 0

    from harness.domain.errors import HarnessError

    runtime = HarnessRuntimeService(args.source_database)
    backup = _load_backup_artifact(args, runtime)
    service = runtime.backup_restore_service(
        restore_database=args.restore_database, restore_cas=args.restore_cas_root
    )
    try:
        outcome = service.restore_and_verify(
            backup=backup,
            backup_restore_id=args.backup_id,
            during_restore=_effect_probe if args.probe_effect else None,
        )
    except HarnessError as exc:
        # 復元開始の拒否（未決の受付予約・Journal・承認）や、コピー中のCAS欠損。
        # 握り潰さない: Codeと種別を出して非0で終える。停止状態はDB側に残る。
        # 復元を始めたか・どこまで写したかはここで推測せず、呼出側がDBと復元先を読む。
        _emit(
            {
                "backup_id": args.backup_id,
                "state": "REJECTED",
                "error_code": exc.code.value,
                "error_type": type(exc).__name__,
                "detail": str(exc),
            }
        )
        return 2
    _emit(
        {
            "backup_id": args.backup_id,
            "state": outcome.result.state,
            "error_code": (
                outcome.result.error_code.value if outcome.result.error_code is not None else None
            ),
            "restored_chain_head": str(outcome.result.restored_chain_head or ""),
            "original_chain_head": str(outcome.result.original_chain_head or ""),
            "artifact_manifest_count_equal": outcome.result.artifact_manifest_count_equal,
            "effects_during_restore": outcome.effects_during_restore,
            "blocked_effect_attempts": [
                {"port": a.port, "operation": a.operation} for a in outcome.guard_attempts
            ],
            "problems": list(outcome.result.problems),
        }
    )
    return 0 if outcome.result.accepted else 2


def _effect_probe(guard: object) -> None:
    """合成Probe。`RestoreGuard` を作用Portへ差し込んだ `EffectOrchestrator` を呼ぶ。

    **通常の本番保護の証跡ではない。** 通常の作用Portと同一DBの受付Gateを
    持つ経路を通していないので、示せるのは「Guardを差し込めば拒否して数える」
    という部品の接続だけである。通常入口の拒否は、同じDBの `IntakeGate` を持つ
    Facade・Workbench・`EffectOrchestrator` を復元区間で呼んで別に観測する
    （`tools/collect_backup_restore_area.py`）。
    """
    orchestrator = build_restore_window_probe(guard)
    orchestrator.execute(restore_window_probe_request())


def _load_backup_artifact(
    args: argparse.Namespace, runtime: HarnessRuntimeService
) -> BackupArtifact:
    """検証対象Backupの束縛を、**Backup側の実Bytesから**読み直す。"""
    heads, artifact_ids = runtime.source_bindings(cas_root=args.source_cas_root)
    return BackupArtifact(
        backup_id=args.backup_id,
        database_path=str(args.backup_database),
        cas_root=str(args.backup_cas_root),
        database_sha256=str(hash_bytes(Path(args.backup_database).read_bytes())),
        created_at=canonical_timestamp("1970-01-01T00:00:00Z"),
        source_chain_heads=heads,
        artifact_ids=artifact_ids,
    )


def _deploy_command(args: argparse.Namespace) -> int:
    """`harness deploy drain --check`。Drain可否を実DBの数え上げから判定する。"""
    if args.deploy_command in ("inspect", "resume", "reconcile"):
        try:
            with HarnessRuntimeService(args.database).operator_resume(
                artifact_root=args.artifact_root, repo_root=args.repo_root
            ) as operator:
                result = (
                    operator.inspect(args.operation)
                    if args.deploy_command == "inspect"
                    else (
                        operator.resume if args.deploy_command == "resume" else operator.reconcile
                    )(
                        review_hash=args.review_hash,
                        auth_session=args.auth_session,
                        reason=args.reason,
                    )
                )
            _emit(result)
            return 0
        except HarnessError as exc:
            unknown = exc.code is ErrorCode.STORAGE_WRITE_FAILED
            _emit(
                {
                    "state": "UNVERIFIED" if unknown else "REJECTED",
                    "resumed": None if unknown else False,
                    "error_code": exc.code.value,
                    "detail": str(exc),
                }
            )
            return 2
        except OSError as exc:
            # Durability failure may occur after bytes/commit. Never claim OPEN or CLOSED.
            _emit(
                {
                    "state": "UNVERIFIED",
                    "resumed": None,
                    "error_code": "STORAGE_WRITE_FAILED",
                    "error_type": type(exc).__name__,
                    "detail": "Storage failure; inspect mode and receipt before another approval.",
                }
            )
            return 2
    service = HarnessRuntimeService(args.database).backup_restore_service(
        restore_database=args.database, restore_cas=args.database.parent
    )
    outcome = service.drain_check(deployment_result_id=args.deployment_id)
    verdict = outcome.verdict
    _emit(
        {
            "deployment_result_id": verdict.deployment_result_id,
            "state": verdict.state,
            "error_code": verdict.error_code.value if verdict.error_code is not None else None,
            "events": [event.value for event in verdict.events],
            "active_run_count": verdict.active_run_count,
            "pending_approval_count": verdict.pending_approval_count,
            "migration_started": verdict.migration_started,
            "intake_stopped": outcome.intake_stopped,
            "deadline_exceeded": outcome.deadline_exceeded,
            "observed_active_states": [list(pair) for pair in outcome.snapshot.active_states],
            "observed_pending_statuses": [list(pair) for pair in outcome.snapshot.pending_statuses],
            "observed_at": outcome.snapshot.observed_at,
            "observation_source": outcome.snapshot.source,
        }
    )
    return 0 if verdict.accepted else 2


def _emit(value: object) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
