"""CLI Workbench Preview の組み立て点。**具象を知るのはここだけである。**

## Connection を 2 本使う

| 用途 | Thread | 直列化 |
|---|---|---|
| HTTP Request 用 | `ThreadingHTTPServer` が作る各 Thread | 呼出側の Lock で 1 件ずつ |
| 送信 Worker 用 | 1 本の Worker Thread | Worker が 1 実行ずつ |

同じ DB File を WAL で共有する。Worker が別 Connection を持つので、
「spawn 前の承認消費と Journal が **別 Connection から観測できる**」ことが
設計上そのまま成り立つ。

## Harness 自身の同一性は起動時に確定する

読み込み済みの Python Code は実行中に差し替わらない。だから Harness の runtime
identity は組み立て時に 1 度だけ計算する。**CLI 実行体の同一性は毎回再計算する**
（`CliRuntimeProfiles.verify`）。両者を混同しない。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.application.approval_consume_coordinator import ApprovalConsumeCoordinator
from harness.application.chatgpt_connection import ChatGptConnectionService
from harness.application.intake_gate import IntakeGate
from harness.application.storage_fence_guard import StorageFenceGuard
from harness.application.workbench_service import (
    _CLOCK_SKEW_TOLERANCE_SECONDS,
    WorkbenchService,
)
from harness.domain.code_proposal import CodeProposal
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_canonical
from harness.domain.schema_set import compute_schema_set_hash
from harness.domain.workbench import (
    ALLOWED_SUFFIXES,
    DENIED_NAMES,
    DENIED_SEGMENTS,
    DENIED_SUFFIXES,
    WorkbenchLimits,
)
from harness.domain.workspace_task import TextArtifact
from harness.infrastructure.artifact.filesystem_cas import FilesystemArtifactCas
from harness.infrastructure.artifact.store import ArtifactStore
from harness.infrastructure.conversation_history import SavedConversationReader
from harness.infrastructure.crypto.local_authority import LocalAuthority
from harness.infrastructure.filesystem.local_workflow_workspace import LocalWorktree
from harness.infrastructure.filesystem.workspace_boundary import FilesystemPolicy
from harness.infrastructure.masking.mock_masker import StaticPhraseMasker
from harness.infrastructure.masking.pipeline import MaskingPipeline
from harness.infrastructure.masking.policy import MaskingPolicy
from harness.infrastructure.planning.verified_builder import VerifiedPlanBuilder
from harness.infrastructure.provider.chatgpt_auth import ChatGptAuth
from harness.infrastructure.provider.chatgpt_generation import ChatGptGeneration
from harness.infrastructure.provider.cli_profiles import (
    CliRuntimeProfiles,
    RuntimeManifest,
    load_runtime_manifest,
)
from harness.infrastructure.provider.cli_response import parse_cli_response, parse_cli_text_response
from harness.infrastructure.provider.cli_runner import SubprocessCliRunner
from harness.infrastructure.schema.registry import CoreSchemaRegistry
from harness.infrastructure.sqlite.action_attempt_repository import SqliteActionAttemptRepository
from harness.infrastructure.sqlite.approval_consume_repository import SqliteConsumeRepository
from harness.infrastructure.sqlite.approval_grant_repository import SqliteApprovalGrantRepository
from harness.infrastructure.sqlite.artifact_manifest_repository import (
    SqliteArtifactManifestRepository,
)
from harness.infrastructure.sqlite.chatgpt_metadata import SqliteChatGptMetadata
from harness.infrastructure.sqlite.connection_factory import ConnectionFactory
from harness.infrastructure.sqlite.conversation_repository import (
    SqliteConversationMessageRepository,
    SqliteConversationRepository,
)
from harness.infrastructure.sqlite.effect_journal_repository import SqliteEffectJournalRepository
from harness.infrastructure.sqlite.event_ledger_repository import SqliteEventLedgerRepository
from harness.infrastructure.sqlite.lease_repository import SqliteLeaseRepository
from harness.infrastructure.sqlite.operation_control import SqliteOperationControl
from harness.infrastructure.sqlite.unit_of_work import SqliteUnitOfWork
from harness.infrastructure.sqlite.workbench_repository import (
    SqliteInvocationJournalRepository,
    SqliteWorkbenchPreferenceRepository,
    SqliteWorkbenchRepository,
)
from harness.ports.chatgpt import ChatGptGenerationPort
from harness.ports.cli_workbench import (
    BoundaryProbePort,
    CliLaunchSpec,
    CliProviderStatus,
    CliRunnerPort,
)

__all__ = ["WorkbenchComposition", "build_workbench_service", "workbench_policy_snapshot"]


def _proposal_parser(provider_id: str, payload: bytes, maximum_bytes: int) -> CodeProposal:
    if provider_id == "chatgpt":
        return CodeProposal.parse(payload, maximum_bytes=maximum_bytes)
    return parse_cli_response(provider_id, payload, maximum_bytes=maximum_bytes)


def workbench_policy_snapshot(
    *, filesystem: FilesystemPolicy, masking: MaskingPolicy, limits: WorkbenchLimits
) -> dict[str, Any]:
    """Plan へ束縛する Policy の投影。**数を本文へ手入力しない。**"""
    return {
        "filesystem_policy_version": filesystem.policy_version,
        "masking": {
            "policy_version": masking.policy_version,
            "snapshot_hash": masking.snapshot_hash,
            "reject_categories": sorted(masking.reject_categories),
            "scanner": "DETERMINISTIC_ONLY",
            "llm_masker_used": False,
        },
        "target_rules": {
            "denied_segments": sorted(DENIED_SEGMENTS),
            "denied_names": sorted(DENIED_NAMES),
            "denied_suffixes": sorted(DENIED_SUFFIXES),
            "allowed_suffixes": sorted(ALLOWED_SUFFIXES),
        },
        "limits": limits.projection(),
        "apply_mode": "SINGLE_EXISTING_FILE_FULL_REPLACEMENT",
        "generated_code_execution": "NEVER",
        "approval_clock_skew_tolerance_seconds": _CLOCK_SKEW_TOLERANCE_SECONDS,
    }


@dataclass
class WorkbenchComposition:
    """組み立て結果。**閉じる責任も一緒に持つ。**"""

    service: WorkbenchService
    connection: sqlite3.Connection
    worktree: LocalWorktree
    manifest: RuntimeManifest | None

    def close(self) -> None:
        self.connection.close()


def build_workbench_service(
    *,
    repo_root: Path,
    database_path: Path,
    artifact_root: Path,
    workspace: Path,
    runtime_profile: Path | None,
    workspace_label: str,
    clock: Any,
    ids: Any,
    runtime_identity: str,
    factory: ConnectionFactory,
    worktree: LocalWorktree,
    limits: WorkbenchLimits | None = None,
    runner: CliRunnerPort | None = None,
    boundary_probe: BoundaryProbePort | None = None,
    chatgpt: ChatGptGenerationPort | None = None,
) -> WorkbenchComposition:
    """Workbench Service を 1 本組む。Migration は呼出側が先に通しておく。"""
    resolved_limits = limits or WorkbenchLimits()
    root = Path(workspace).resolve()
    for other in (database_path, artifact_root):
        if Path(other).resolve().is_relative_to(root):
            raise HarnessError(
                ErrorCode.PATH_OUTSIDE_CAPABILITY,
                "the database and the artifact store must live outside the worktree",
            )
    manifest = None if runtime_profile is None else load_runtime_manifest(runtime_profile)
    if manifest is not None and Path(manifest.neutral_workdir).resolve().is_relative_to(root):
        raise HarnessError(
            ErrorCode.PATH_OUTSIDE_CAPABILITY,
            "the neutral CLI working directory must be outside the worktree",
        )
    connection = factory.connect(cross_thread=True)
    try:
        artifacts = ArtifactStore(
            FilesystemArtifactCas(artifact_root), SqliteArtifactManifestRepository(connection)
        )
        uow = SqliteUnitOfWork(factory, connection)
        leases = SqliteLeaseRepository(connection)
        journals = SqliteEffectJournalRepository(connection)
        ledger = SqliteEventLedgerRepository(connection)
        schemas = CoreSchemaRegistry(repo_root)
        approval_consumer = ApprovalConsumeCoordinator(
            clock=clock,
            grants=SqliteApprovalGrantRepository(connection),
            store=SqliteConsumeRepository(connection),
            artifacts=artifacts,
            ledger=ledger,
            uow=uow,
            schema_set_hash=compute_schema_set_hash((schemas.active_ref("ApprovalConsumeResult"),)),
            validate=lambda name, record: schemas.validate_or_raise(
                name, str(record["schema_version"]), record
            ),
        )
        filesystem_policy = FilesystemPolicy.load(repo_root)
        masking_policy = MaskingPolicy.load(repo_root)
        pipeline = MaskingPipeline(
            masking_policy, StaticPhraseMasker(phrases={}), repo_root=repo_root
        )
        policy = workbench_policy_snapshot(
            filesystem=filesystem_policy, masking=masking_policy, limits=resolved_limits
        )
        service = WorkbenchService(
            chatgpt=chatgpt,
            operation_gate=IntakeGate(SqliteOperationControl(database_path)),
            sessions=SqliteWorkbenchRepository(connection),
            conversations=SavedConversationReader(
                SqliteConversationRepository(connection),
                SqliteConversationMessageRepository(connection),
                artifacts,
            ),
            preferences=SqliteWorkbenchPreferenceRepository(connection),
            preference_scope=hash_canonical(
                {"workspace": str(root)}, artifact_type="workbench-preference-scope", schema_major=1
            ),
            invocations=SqliteInvocationJournalRepository(connection),
            artifacts=artifacts,
            ledger=ledger,
            uow=uow,
            clock=clock,
            ids=ids,
            workspace=worktree,
            workspace_label=workspace_label,
            authority=LocalAuthority(),
            grants=SqliteApprovalGrantRepository(connection),
            approval_consumer=approval_consumer,
            leases=leases,
            journals=journals,
            attempts=SqliteActionAttemptRepository(connection),
            builder=VerifiedPlanBuilder(),
            profiles=NoCliProfiles()
            if manifest is None
            else CliRuntimeProfiles(
                manifest=manifest,
                # 境界の実効性は「この 3 つへ触れられないこと」で測る。
                # 書き換え対象の Worktree、状態 DB、Artifact CAS である。
                probe_paths=(
                    str(root),
                    str(Path(database_path).resolve().parent),
                    str(Path(artifact_root).resolve()),
                ),
                # 既定は `None`。`CliRuntimeProfiles` が実測 Probe を自分で作る。
                # 差し替えるのは試験だけで、CLI からは渡せない。
                boundary_probe=boundary_probe,
            ),
            runner=runner or SubprocessCliRunner(),
            masking=pipeline,
            response_parser=_proposal_parser,
            text_response_parser=lambda provider, payload, maximum: (
                TextArtifact.parse(payload, maximum_bytes=maximum)
                if provider == "chatgpt"
                else parse_cli_text_response(provider, payload, maximum_bytes=maximum)
            ),
            guard_factory=lambda lease: StorageFenceGuard(
                leases,
                journals,
                uow,
                clock,
                lease.lease_id,
                lease.resource_key,
                lease.fencing_token,
            ),
            runtime_identity=lambda: runtime_identity,
            validate_record=lambda name, record: schemas.validate_or_raise(
                name, str(record["schema_version"]), record
            ),
            policy_snapshot=lambda: policy,
            schema_set_hash=compute_schema_set_hash((schemas.active_ref("EffectReceipt"),)),
            limits=resolved_limits,
        )
    except BaseException:
        connection.close()
        raise
    return WorkbenchComposition(
        service=service, connection=connection, worktree=worktree, manifest=manifest
    )


def build_chatgpt_connection(
    *,
    factory: ConnectionFactory,
    ids: Any,
) -> tuple[ChatGptConnectionService, ChatGptGeneration, list[Any]]:
    connection = factory.connect(cross_thread=True)
    auth = ChatGptAuth()
    try:
        service = ChatGptConnectionService(
            auth=auth,
            metadata=SqliteChatGptMetadata(connection),
            uow=SqliteUnitOfWork(factory, connection),
            ids=ids,
        )
        auth.remember = service.remember
        return service, ChatGptGeneration(auth), [service.close, connection.close]
    except BaseException:
        auth.close()
        connection.close()
        raise


class NoCliProfiles:
    """No CLI installation/profile is required for the independent ChatGPT HTTP route."""

    def statuses(self) -> tuple[CliProviderStatus, ...]:
        return ()

    def resolve(
        self,
        *,
        provider_id: str,
        model_id: str,
        reasoning_effort: str | None = None,
    ) -> CliLaunchSpec:
        raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "CLI profile is not configured")

    def verify(self, spec: CliLaunchSpec) -> None:
        raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "CLI profile is not configured")

    def attest(self, spec: CliLaunchSpec) -> dict[str, Any]:
        raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "CLI profile is not configured")
