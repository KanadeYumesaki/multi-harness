"""Composition for the offline workflow; actual time comes from the local clock."""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from harness.application.approval_consume_coordinator import ApprovalConsumeCoordinator
from harness.application.intake_gate import IntakeGate
from harness.application.local_workflow import LocalWorkflowService
from harness.application.storage_fence_guard import StorageFenceGuard
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_canonical
from harness.domain.schema_set import compute_schema_set_hash
from harness.infrastructure.artifact.filesystem_cas import FilesystemArtifactCas
from harness.infrastructure.artifact.store import ArtifactStore
from harness.infrastructure.crypto.local_authority import LocalAuthority
from harness.infrastructure.filesystem.executable_digest import FilesystemExecutableDigest
from harness.infrastructure.filesystem.local_workflow_workspace import LocalWorktree
from harness.infrastructure.filesystem.plan_paths import validate_plan_paths
from harness.infrastructure.filesystem.workspace_boundary import FilesystemPolicy
from harness.infrastructure.planning.verified_builder import VerifiedPlanBuilder
from harness.infrastructure.provider.mock_provider import DeterministicMockProvider
from harness.infrastructure.schema.registry import CoreSchemaRegistry
from harness.infrastructure.sqlite.action_attempt_repository import SqliteActionAttemptRepository
from harness.infrastructure.sqlite.approval_consume_repository import SqliteConsumeRepository
from harness.infrastructure.sqlite.approval_grant_repository import SqliteApprovalGrantRepository
from harness.infrastructure.sqlite.artifact_manifest_repository import (
    SqliteArtifactManifestRepository,
)
from harness.infrastructure.sqlite.connection_factory import ConnectionFactory
from harness.infrastructure.sqlite.effect_journal_repository import SqliteEffectJournalRepository
from harness.infrastructure.sqlite.event_ledger_repository import SqliteEventLedgerRepository
from harness.infrastructure.sqlite.lease_repository import SqliteLeaseRepository
from harness.infrastructure.sqlite.local_workflow_repository import SqliteWorkflowRepository
from harness.infrastructure.sqlite.migrations import migrate
from harness.infrastructure.sqlite.operation_control import SqliteOperationControl
from harness.infrastructure.sqlite.unit_of_work import SqliteUnitOfWork


class SystemClock:
    def now(self) -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def current_runtime_identity(repo_root: Path) -> str:
    entries = {
        str(path.relative_to(repo_root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for parent in (repo_root / "src/harness", repo_root / "design-source/registries")
        for path in sorted(parent.rglob("*"))
        if path.is_file() and path.suffix in (".py", ".yaml")
    }
    entries["executable"] = hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
    return str(hash_canonical(entries, artifact_type="local-runtime-identity", schema_major=1))


@contextmanager
def local_workflow(
    *, database: Path, artifact_root: Path, workspace: Path, repo_root: Path
) -> Iterator[LocalWorkflowService]:
    policy = FilesystemPolicy.load(repo_root)
    validate_plan_paths(
        workspace=workspace, artifact_root=artifact_root, database=database, policy=policy
    )
    root = Path(os.path.abspath(workspace))
    if any(Path(os.path.abspath(path)).is_relative_to(root) for path in (database, artifact_root)):
        raise HarnessError(
            ErrorCode.PATH_OUTSIDE_CAPABILITY, "DB and artifacts must be outside the worktree"
        )
    worktree = LocalWorktree(workspace, policy)
    try:
        clock = SystemClock()
        factory = ConnectionFactory(database)
        migrate(factory, recorded_at=clock.now())
        connection = factory.connect()
        try:
            artifact_root.mkdir(parents=True, exist_ok=True)
            artifacts = ArtifactStore(
                FilesystemArtifactCas(artifact_root), SqliteArtifactManifestRepository(connection)
            )
            uow = SqliteUnitOfWork(factory, connection)
            leases = SqliteLeaseRepository(connection)
            journals = SqliteEffectJournalRepository(connection)
            schemas = CoreSchemaRegistry(repo_root)
            approval_consumer = ApprovalConsumeCoordinator(
                clock=clock,
                grants=SqliteApprovalGrantRepository(connection),
                store=SqliteConsumeRepository(connection),
                artifacts=artifacts,
                ledger=SqliteEventLedgerRepository(connection),
                uow=uow,
                schema_set_hash=compute_schema_set_hash(
                    (schemas.active_ref("ApprovalConsumeResult"),)
                ),
                validate=lambda name, record: schemas.validate_or_raise(
                    name, str(record["schema_version"]), record
                ),
            )
            yield LocalWorkflowService(
                operation_gate=IntakeGate(SqliteOperationControl(database)),
                runs=SqliteWorkflowRepository(connection),
                artifacts=artifacts,
                ledger=SqliteEventLedgerRepository(connection),
                uow=uow,
                clock=clock,
                workspace=worktree,
                authority=LocalAuthority(),
                grants=SqliteApprovalGrantRepository(connection),
                approval_consumer=approval_consumer,
                leases=leases,
                journals=journals,
                attempts=SqliteActionAttemptRepository(connection),
                builder=VerifiedPlanBuilder(),
                provider=DeterministicMockProvider("mock-adapter/1.0"),
                guard_factory=lambda lease: StorageFenceGuard(
                    leases,
                    journals,
                    uow,
                    clock,
                    lease.lease_id,
                    lease.resource_key,
                    lease.fencing_token,
                ),
                runtime_identity=lambda: current_runtime_identity(repo_root),
                executable_digest=FilesystemExecutableDigest().digest,
                validate_record=lambda name, record: schemas.validate_or_raise(
                    name, str(record["schema_version"]), record
                ),
            )
        finally:
            connection.close()
    finally:
        worktree.close()
