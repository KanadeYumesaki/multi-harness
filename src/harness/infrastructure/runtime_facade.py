"""CLI向けの安全なApplication Service。

## 組立て場所であって、業務規則の置き場ではない

ここが持つのは「具象をどう繋ぐか」だけである。Task Loader・Context・Plan の
規則は `application/task_plan_service.py` と `domain/` にある。CLI は
この Facade だけを呼び、Repository へ直接触らない（CLAUDE.md §2）。
"""

from __future__ import annotations

import datetime
import hashlib
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from harness.application.approval_consume_coordinator import ApprovalConsumeCoordinator
from harness.application.backup_restore_service import BackupRestoreService
from harness.application.context_assembly import ContextAssemblyService, control_data_policy_hash
from harness.application.input_read_orchestrator import InputReadOrchestrator
from harness.application.intake_gate import IntakeGate
from harness.application.masking_policy_gate import MaskingPolicyGate
from harness.application.operator_resume import OperatorResumeService
from harness.application.task_plan_service import (
    TaskArtifactClassifier,
    TaskPlanOutcome,
    TaskPlanRequest,
    TaskPlanService,
)
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import HASH_PROFILE_VERSION, ContentHash, hash_canonical
from harness.domain.input_read import CapabilityScope
from harness.domain.schema_set import compute_schema_set_hash
from harness.infrastructure.artifact.filesystem_cas import FilesystemArtifactCas
from harness.infrastructure.artifact.store import ArtifactStore
from harness.infrastructure.crypto.local_authority import LocalAuthority
from harness.infrastructure.filesystem.executable_digest import FilesystemExecutableDigest
from harness.infrastructure.filesystem.plan_paths import validate_plan_paths
from harness.infrastructure.filesystem.safe_reader import CapabilityBroker, SafeInputReader
from harness.infrastructure.filesystem.workspace_boundary import FilesystemPolicy
from harness.infrastructure.masking.mock_masker import StaticPhraseMasker
from harness.infrastructure.masking.pipeline import MaskingPipeline
from harness.infrastructure.masking.policy import MaskingPolicy
from harness.infrastructure.planning.verified_builder import VerifiedPlanBuilder
from harness.infrastructure.provider.mock_provider import DeterministicMockProvider
from harness.infrastructure.schema.registry import CoreSchemaRegistry
from harness.infrastructure.sqlite.approval_consume_repository import SqliteConsumeRepository
from harness.infrastructure.sqlite.approval_grant_repository import SqliteApprovalGrantRepository
from harness.infrastructure.sqlite.artifact_manifest_repository import (
    SqliteArtifactManifestRepository,
)
from harness.infrastructure.sqlite.connection_factory import ConnectionFactory, ConnectionRole
from harness.infrastructure.sqlite.event_ledger_repository import SqliteEventLedgerRepository
from harness.infrastructure.sqlite.migrations import migrate
from harness.infrastructure.sqlite.operation_control import (
    SqliteOperationControl,
    SqliteOperationControlTransaction,
)
from harness.infrastructure.sqlite.operations_repository import (
    SqliteDrainInspection,
    SqliteRestoreTarget,
    create_backup,
)
from harness.infrastructure.sqlite.unit_of_work import SqliteUnitOfWork
from harness.infrastructure.tokenizer.deterministic_counter import ByteBoundTokenCounter
from harness.ports.operations import BackupArtifact
from harness.ports.provider import ProviderRequest, ProviderResponse

__all__ = ["HarnessRuntimeService", "TaskPlanSetup"]

#: Policy Snapshot の材料。**正本 File の生 Bytes を測る。**
#: 手で写した値を使うと、Policy を変えても Snapshot Hash が動かない。
_POLICY_FILES = ("masking-policy.yaml", "filesystem-policy.yaml", "chat-context-policy.yaml")


class _SystemClock:
    """Facade が組む Clock。Application は Port 経由でしか時刻を見ない。"""

    def now(self) -> str:
        return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class HarnessRuntimeService:
    def __init__(self, database_path: Path) -> None:
        self._factory = ConnectionFactory(database_path)
        self._database_path = database_path

    def migrate(self, *, recorded_at: str) -> int:
        return migrate(self._factory, recorded_at=recorded_at)

    def verify_ledger(self, stream_id: str) -> bool:
        connection = self._factory.connect(ConnectionRole.RUNTIME)
        try:
            result = SqliteEventLedgerRepository(connection).verify_chain(stream_id)
            return result.valid
        finally:
            connection.close()

    # ------------------------------------------------------------- §27 / §3.10
    #
    # Backup / Restore / Drain の具象もここで組む。CLI が Repository を直接
    # 触ると Unit of Work の外で SQL が走りうる（CLAUDE.md §2）。

    def create_backup(
        self,
        *,
        source_cas: Path,
        destination_database: Path,
        destination_cas: Path,
        backup_id: str,
        created_at: str,
    ) -> BackupArtifact:
        """Backupを作る。判定はせず、作った事実と束縛だけを返す。"""
        return create_backup(
            source_database=self._database_path,
            source_cas=source_cas,
            destination_database=destination_database,
            destination_cas=destination_cas,
            backup_id=backup_id,
            created_at=created_at,
        )

    def backup_restore_service(
        self, *, restore_database: Path, restore_cas: Path
    ) -> BackupRestoreService:
        """Drain と Restore を担う Application Service を組む。"""
        return BackupRestoreService(
            drain=SqliteDrainInspection(self._database_path),
            target=SqliteRestoreTarget(database=restore_database, cas_root=restore_cas),
            clock=_SystemClock(),
            intake=self.intake_gate(),
        )

    @contextmanager
    def operator_resume(
        self, *, artifact_root: Path, repo_root: Path
    ) -> Iterator[OperatorResumeService]:
        """Compose administrative approval and audit on the existing database only."""
        database = Path(os.path.abspath(self._database_path))
        artifact_root = Path(os.path.abspath(artifact_root))
        validate_plan_paths(
            workspace=database.parent,
            database=database,
            artifact_root=artifact_root,
            policy=FilesystemPolicy.load(repo_root),
        )
        if not database.is_file():
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH, "operation control DB is not initialized"
            )
        factory = ConnectionFactory(database)
        connection = None
        try:
            connection = factory.connect()
            control = SqliteOperationControlTransaction(connection)
            control.mode()  # Never migrate an operator-selected database implicitly.
            clock = _SystemClock()
            uow = SqliteUnitOfWork(factory, connection)
            grants = SqliteApprovalGrantRepository(connection)
            ledger = SqliteEventLedgerRepository(connection)
            artifacts = ArtifactStore(
                FilesystemArtifactCas(artifact_root), SqliteArtifactManifestRepository(connection)
            )
            schemas = CoreSchemaRegistry(repo_root)
            consumer = ApprovalConsumeCoordinator(
                clock=clock,
                grants=grants,
                store=SqliteConsumeRepository(connection),
                artifacts=artifacts,
                ledger=ledger,
                uow=uow,
                schema_set_hash=compute_schema_set_hash(
                    (schemas.active_ref("ApprovalConsumeResult"),)
                ),
                validate=lambda name, record: schemas.validate_or_raise(
                    name, str(record["schema_version"]), record
                ),
            )
            stat = database.stat()
            identity = str(
                hash_canonical(
                    {
                        "database": str(database),
                        "device": stat.st_dev,
                        "inode": stat.st_ino,
                        "artifact_root": str(artifact_root),
                    },
                    artifact_type="operator-storage-identity",
                    schema_major=1,
                )
            )
            yield OperatorResumeService(
                control=control,
                owners=SqliteOperationControl(database).ownership(),
                identity=identity,
                authority=LocalAuthority(),
                clock=clock,
                uow=uow,
                grants=grants,
                consumer=consumer,
                artifacts=artifacts,
                ledger=ledger,
            )
        except sqlite3.Error as exc:
            raise HarnessError(
                ErrorCode.STORAGE_WRITE_FAILED,
                "operator storage unavailable; inspect state before retry",
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    def intake_gate(self) -> IntakeGate:
        """新規受付のGate。**受付入口はこれを通る。**"""
        return IntakeGate(SqliteOperationControl(self._database_path))

    def source_bindings(
        self, *, cas_root: Path
    ) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
        """復元前の Chain Head と Artifact 参照集合を読む。比較基準になる。"""
        target = SqliteRestoreTarget(database=self._database_path, cas_root=cas_root)
        return target.source_chain_heads(), target.source_artifact_ids()

    def mock_propose(
        self,
        *,
        model_id: str,
        instruction_hash: ContentHash,
        context_bundle_hash: ContentHash,
        input_artifact_hash: ContentHash,
        output_schema_hash: ContentHash,
    ) -> ProviderResponse:
        request = ProviderRequest(
            provider_id="mock",
            model_id=model_id,
            instruction_hash=instruction_hash,
            context_bundle_hash=context_bundle_hash,
            input_artifact_hash=input_artifact_hash,
            output_schema_hash=output_schema_hash,
        )
        return DeterministicMockProvider(adapter_version="mock-adapter/1.0").propose(request)

    @contextmanager
    def task_plan(self, setup: TaskPlanSetup) -> Iterator[TaskPlanService]:
        with self.intake_gate().admission("task-plan"):
            with self._task_plan(setup) as service:
                yield service

    @contextmanager
    def _task_plan(self, setup: TaskPlanSetup) -> Iterator[TaskPlanService]:
        """Task Loader・Context・Plan を組んで貸す。

        Capability の Root FD は Broker が private に持つ。`with` を抜けるまで
        生かし、抜けたら閉じる。FD 番号の再利用による Workspace すり替えを
        防ぐのは Broker 側の責務である（`safe_reader` の docstring 参照）。

        **Provider をここへ繋がない。** CC-03 の経路は Plan までであり、
        繋がなければ呼ぶ経路が無い。
        """
        # **新規要求の受付入口。** Drain 中は受け付けない（§3.10）。
        # Gate を通らない入口を増やすと、その入口は止まらない。
        validate_plan_paths(
            workspace=setup.workspace_root,
            artifact_root=setup.artifact_root,
            database=self._factory.database_path,
            policy=FilesystemPolicy.load(setup.repo_root),
            allow_test_filesystems=setup.allow_test_filesystems,
        )
        connection = self._factory.connect(ConnectionRole.RUNTIME)
        try:
            unit_of_work = SqliteUnitOfWork(self._factory, connection)
            masking_policy = MaskingPolicy.load(setup.repo_root)
            gate = MaskingPolicyGate(
                MaskingPipeline(
                    masking_policy, StaticPhraseMasker(phrases={}), repo_root=setup.repo_root
                )
            )
            setup.artifact_root.mkdir(parents=True, exist_ok=True)
            artifacts = ArtifactStore(
                FilesystemArtifactCas(setup.artifact_root),
                SqliteArtifactManifestRepository(connection),
            )
            broker = CapabilityBroker(
                FilesystemPolicy.load(setup.repo_root),
                allow_test_filesystems=setup.allow_test_filesystems,
            )
            try:
                broker.issue(setup.capability_id, setup.workspace_root, setup.capability_scope)
                reader = SafeInputReader(broker)
                input_service = InputReadOrchestrator(
                    reader=reader,
                    enumerator=reader,
                    ledger=SqliteEventLedgerRepository(connection),
                    unit_of_work=unit_of_work,
                    clock=_FixedClock(setup.now),
                    classifier=TaskArtifactClassifier(),
                    masking_gate=gate,
                )
                yield TaskPlanService(
                    input_read=input_service,
                    input_enumeration=input_service,
                    context=ContextAssemblyService(
                        token_counter=ByteBoundTokenCounter(),
                        artifact_store=artifacts,
                        unit_of_work=unit_of_work,
                    ),
                    executable_digest=FilesystemExecutableDigest(),
                    plan_builder=VerifiedPlanBuilder(),
                    schema_set_hash=ContentHash.parse(
                        CoreSchemaRegistry(setup.repo_root).schema_set_hash()
                    ),
                    policy_snapshot_hash=policy_snapshot_hash(setup.repo_root),
                    control_data_policy_hash=control_data_policy_hash(),
                )
            finally:
                broker.close()
        finally:
            connection.close()

    def initialize_task_plan(self, setup: TaskPlanSetup) -> None:
        """全Pathが通るまでDB/CASを初期化しない。"""
        # **新規要求の受付入口。** Drain 中は受け付けない（§3.10）。
        # Gate を通らない入口を増やすと、その入口は止まらない。
        validate_plan_paths(
            workspace=setup.workspace_root,
            artifact_root=setup.artifact_root,
            database=self._factory.database_path,
            policy=FilesystemPolicy.load(setup.repo_root),
            allow_test_filesystems=setup.allow_test_filesystems,
        )
        self.migrate(recorded_at=setup.now)
        self.intake_gate().admit("initialize-task-plan")

    def plan_task(self, setup: TaskPlanSetup, request: TaskPlanRequest) -> TaskPlanOutcome:
        """1回きりの Plan 生成。CLI はこれを呼ぶ。"""
        with self.task_plan(setup) as service:
            return service.plan(request)


@dataclass(frozen=True, slots=True)
class TaskPlanSetup:
    """Plan 生成の組立てに要る具象。**業務入力はここへ入れない。**"""

    repo_root: Path
    workspace_root: Path
    artifact_root: Path
    capability_id: str
    capability_scope: CapabilityScope
    now: str
    #: tmpfs 等のテスト用 Filesystem を許すか。**既定は許さない。**
    allow_test_filesystems: bool = False


@dataclass(frozen=True, slots=True)
class _FixedClock:
    """Clock Port。呼び出しごとに動かない値を返す。

    Plan 生成の途中で時刻が動くと、同じ実行の中で別の時刻が Event へ載る。
    入口で1回決めた値を使う。
    """

    stamp: str

    def now(self) -> str:
        return self.stamp


def policy_snapshot_hash(repo_root: Path) -> ContentHash:
    """`ExecutionPlan.policy_snapshot_hash`。正本 Policy File の生 Bytes を束縛する。

    File 名の昇順で並べてから Hash する（不変条件#6）。**存在しない File を
    無視しない。** 無視すると、Policy を消しただけで Hash が変わらなくなる。
    """
    entries = []
    for name in sorted(_POLICY_FILES):
        path = repo_root / "design-source" / "registries" / name
        entries.append(
            {
                "name": name,
                "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return hash_canonical(
        {"policy_files": entries, "hash_profile_version": HASH_PROFILE_VERSION},
        artifact_type="policy-snapshot",
        schema_major=1,
    )
