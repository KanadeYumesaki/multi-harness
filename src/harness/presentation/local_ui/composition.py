"""ローカル UI が呼ぶ Application Service を組み立てる合成点。

## ここが唯一の組み立て場所である

UI は Application Service だけを呼ぶ（CLAUDE.md §2）。Repository へも SQLite へも
Domain の保存経路へも直接触れない。触れないことを保つために、**具象を知るのは
この Module だけ**にする。

## Masker を持たない

LLM Masker はこの環境に無い。だから **Span を 1 つも提案しない Masker** を挿す。
これは拒否の強さを下げない。Reject 分類（Secret／`NATIONAL_ID`／
`SPECIAL_CATEGORY_DATA`）は Scan#1 が Masker を呼ぶ**前**に落とすからである。
提案が無ければマスクは起きず、会話保存はもともとマスクせず拒否する（CMC-5-A）。

## 時刻と ID を Domain の中で引かない

`Clock` と `IdSource` を注入する。試験は固定値を渡し、実行時は標準ライブラリを
使う。Service の内側で `datetime` も `uuid` も呼ばない。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from harness.application.chat_context_service import ChatContextService
from harness.application.conversation_service import ConversationService
from harness.application.masking_policy_gate import MaskingPolicyGate
from harness.domain.artifact import ArtifactMetadata, ArtifactVerification
from harness.domain.context_budget import EstimateAssurance
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.errors import ErrorCode as _ErrorCode
from harness.domain.hashing import ContentHash, hash_bytes, hash_canonical
from harness.infrastructure import owner_value_recorder
from harness.infrastructure.artifact.filesystem_cas import FilesystemArtifactCas
from harness.infrastructure.artifact.store import ArtifactStore
from harness.infrastructure.crypto.local_authority import LocalAuthority
from harness.infrastructure.filesystem.local_workflow_workspace import LocalWorktree
from harness.infrastructure.filesystem.workspace_boundary import FilesystemPolicy
from harness.infrastructure.masking.mock_masker import StaticPhraseMasker
from harness.infrastructure.masking.pipeline import MaskingPipeline
from harness.infrastructure.masking.policy import MaskingPolicy
from harness.infrastructure.provider.mock_provider import DeterministicMockProvider
from harness.infrastructure.runtime_facade import HarnessRuntimeService
from harness.infrastructure.schema.registry import CoreSchemaRegistry
from harness.infrastructure.sqlite.artifact_manifest_repository import (
    SqliteArtifactManifestRepository,
)
from harness.infrastructure.sqlite.connection_factory import ConnectionFactory, ConnectionRole
from harness.infrastructure.sqlite.conversation_repository import (
    SqliteConversationMessageRepository,
    SqliteConversationRepository,
    SqliteConversationSnapshotRepository,
)
from harness.infrastructure.sqlite.migrations import migrate
from harness.infrastructure.sqlite.unit_of_work import SqliteUnitOfWork
from harness.infrastructure.tokenizer.deterministic_counter import (
    BYTE_BOUND_TOKENIZER_NAME,
    BYTE_BOUND_TOKENIZER_VERSION,
    COUNTING_ADAPTER_VERSION,
    ByteBoundTokenCounter,
)
from harness.infrastructure.workbench_runtime import (
    build_chatgpt_connection,
    build_workbench_service,
)
from harness.ports.artifact_store import ArtifactManifestRecord, ArtifactStorePort
from harness.ports.provider import ProviderPort
from harness.ports.token_counter import TokenCounterPort
from harness.ports.unit_of_work import UnitOfWorkPort
from harness.presentation.local_ui.workbench_gateway import WorkbenchGateway

__all__ = [
    "PRODUCER",
    "Clock",
    "IdSource",
    "LocalUiServices",
    "SystemClock",
    "TransactionalArtifactStore",
    "UuidSource",
    "WorkbenchSetup",
    "build_services",
    "output_schema_hash",
]

#: 保存 Record の `producer`。誰が書いたかを 1 つの語で表す。
PRODUCER = "harness-local-ui/1"

#: Mock Adapter が名乗る版。**Provider 固有値ではない。**
#:
#: `runtime_facade` の CLI 経路が使っている版と同じ語である。UI 側で別の版を
#: 名乗ると、同じ Adapter が 2 つの名前を持つことになる。
MOCK_ADAPTER_VERSION = "mock-adapter/1.0"


def _empty_capability_set_hash() -> ContentHash:
    """Chat の Message は Filesystem Input Read を経ていない。

    Capability は 0 件である。**「無い」を Hash として述べる。** 空集合の Hash は
    `application/context_assembly.py` が同じ入力から作るものと同じ形にする。
    """
    return hash_canonical(
        {"input_read_capability_ids": []},
        artifact_type="input-read-capability-set",
        schema_major=1,
    )


def _empty_read_evidence_hash() -> ContentHash:
    """同じ理由で Read Evidence も 0 件である。"""
    return hash_canonical(
        {"input_read_evidence": []},
        artifact_type="input-read-evidence",
        schema_major=1,
    )


@dataclass(frozen=True, slots=True)
class TransactionalArtifactStore:
    """Manifest 書込みへ Transaction を与える `ArtifactStorePort` の薄い層。

    `ConversationService` は CAS 保存を **自分の Transaction の外**で行う。
    落ちたときに参照されない Artifact が残る方が、参照先の無い Record が残る
    より安全だからである。その順序は変えない。Manifest 書込みが不変条件#15 で
    Transaction を要求するので、**その 1 回分だけ**をここで開く。

    読取りは Transaction を要らない（`transaction_guard` の規約）。素通しする。
    """

    inner: ArtifactStorePort
    unit_of_work: UnitOfWorkPort

    def put(
        self,
        data: bytes,
        metadata: ArtifactMetadata,
        *,
        artifact_id: str,
        stored_at: str,
    ) -> ArtifactManifestRecord:
        with self.unit_of_work.begin_immediate():
            record = self.inner.put(data, metadata, artifact_id=artifact_id, stored_at=stored_at)
        # Commit を抜けてから返す。返した時点で Manifest は確定している。
        return record

    def get(self, content_hash: ContentHash) -> bytes:
        return self.inner.get(content_hash)

    def verify(self, content_hash: ContentHash) -> ArtifactVerification:
        return self.inner.verify(content_hash)


class Clock(Protocol):
    def now(self) -> str:
        """RFC3339 UTC の現在時刻。"""
        ...


class IdSource(Protocol):
    def new_id(self) -> str:
        """新しい識別子。**UI 側で採番規則を決めない。**"""
        ...


@dataclass(frozen=True, slots=True)
class SystemClock:
    def now(self) -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class UuidSource:
    def new_id(self) -> str:
        return str(uuid.uuid4())


@dataclass(frozen=True, slots=True)
class LocalUiServices:
    """UI が使う Application Service と、その周辺の読み取り専用の値。"""

    repo_root: Path
    database_path: Path
    artifact_root: Path
    conversations: ConversationService
    context: ChatContextService
    token_counter: TokenCounterPort
    design_sha256: ContentHash
    clock: Clock
    id_source: IdSource
    max_body_bytes: int
    connection: sqlite3.Connection
    #: Provider Adapter。**Port として持つ。** UI は具象を知らない。
    provider: ProviderPort
    #: Core Schema Catalog。`output_schema_hash` はここからだけ引く。
    #:
    #: **Catalog に無い Schema は指せない**（Owner Decision `MTM-3-A`）。
    schema_catalog: CoreSchemaRegistry
    #: Workbench Preview。**起動時に Workspace を指定しなければ `None` になる。**
    #:
    #: `None` のあいだ `/api/workbench` は「未設定」とだけ答える。画面から任意の
    #: 絶対 Path を渡して有効化する口は作らない。
    operator_auth_session: str | None = None
    workbench: WorkbenchGateway | None = None
    #: Workbench を閉じるための後始末。`close()` から呼ぶ。
    workbench_close: Any = None
    conversations_producer: str = PRODUCER
    #: `ByteBoundTokenCounter` が名乗る保証水準。UI 側で強く言い換えない。
    counter_assurance: EstimateAssurance = EstimateAssurance.CONSERVATIVE
    empty_capability_set_hash: ContentHash = field(default_factory=_empty_capability_set_hash)
    empty_read_evidence_hash: ContentHash = field(default_factory=_empty_read_evidence_hash)

    @property
    def tokenizer_identity(self) -> dict[str, str]:
        """Profile が名乗るべき計数器の同一性。**この 3 値は正本側から採る。**"""
        return {
            "tokenizer_name": BYTE_BOUND_TOKENIZER_NAME,
            "tokenizer_version": BYTE_BOUND_TOKENIZER_VERSION,
            "counting_adapter_version": COUNTING_ADAPTER_VERSION,
        }

    def operation_inspection(self) -> dict[str, Any]:
        with HarnessRuntimeService(self.database_path).operator_resume(
            artifact_root=self.artifact_root, repo_root=self.repo_root
        ) as service:
            return {
                "write_enabled": self.operator_auth_session is not None,
                "resume": service.inspect(),
                "reconcile": service.inspect("reconcile"),
            }

    def operation_decision(self, operation: str, review_hash: str) -> dict[str, Any]:
        if self.operator_auth_session is None:
            raise HarnessError(
                ErrorCode.APPROVAL_REQUIRED, "operator actions are not enabled at startup"
            )
        with HarnessRuntimeService(self.database_path).operator_resume(
            artifact_root=self.artifact_root, repo_root=self.repo_root
        ) as service:
            if operation == "resume":
                return service.resume(
                    review_hash=review_hash,
                    auth_session=self.operator_auth_session,
                    reason="maintenance-complete",
                )
            if operation == "reconcile":
                return service.reconcile(
                    review_hash=review_hash,
                    auth_session=self.operator_auth_session,
                    reason="reconcile-quiescent-source",
                )
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "unknown maintenance operation"
            )

    def record_owner_values(self, document: dict[str, Any]) -> dict[str, Any]:
        """Owner の値を Package へ記録する。**記録器だけが書く。**

        検査は `owner_value_recorder.validate` が行う。落ちたら何も書かない。
        UI は Package へ直接触れない。
        """
        return owner_value_recorder.save_values(
            self.repo_root / owner_value_recorder.PACKAGE_RELATIVE,
            document,
            owner_value_recorder.failure_ids(self.repo_root),
        )

    def close(self) -> None:
        """**Worker を静めてから接続を閉じる。** 順序を逆にすると Process ごと落ちる。

        止められなかった場合は接続を閉じない。Handle を残す方が、使用中の接続を
        引き抜いて落ちるより安全である。
        """
        if self.workbench is not None and not self.workbench.shutdown():
            raise HarnessError(
                _ErrorCode.EFFECT_UNKNOWN,
                "a CLI invocation is still running; connections were left open on purpose",
            )
        if self.workbench_close is not None:
            self.workbench_close()
        self.connection.close()


def output_schema_hash(
    catalog: CoreSchemaRegistry, *, schema_name: str | None, schema_version: str | None
) -> ContentHash | None:
    """Catalog に実在する Schema 名と版から `output_schema_hash` を引く。

    ## 発明しない（Owner Decision `MTM-3-A`）

    Catalog に無い名前や版は **拒否する。** 既定の Schema を作らない。仮の Hash も
    作らない。Schema を使わない経路では `None` を返す。

    Hash は Catalog が既に持っている `content_hash` である。**ここで新しい Hash 規則を
    作らない。**
    """
    if schema_name is None and schema_version is None:
        # Schema を使わない経路。`InvocationManifest` は任意としている。
        return None
    if not schema_name or not schema_version:
        raise HarnessError(
            ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
            "output schema needs both a name and a version, or neither",
        )
    for ref in catalog.refs:
        if ref.schema_name == schema_name and ref.schema_version == schema_version:
            return ref.content_hash
    raise HarnessError(
        ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
        f"schema {schema_name}@{schema_version} is not in the core schema catalog",
    )


def design_hash(repo_root: Path) -> ContentHash:
    """いまの設計書の Hash。Snapshot はこの値へ束縛される。"""
    snapshot = json.loads((repo_root / "registry-snapshot.json").read_text(encoding="utf-8"))
    design = repo_root / f"design-v{snapshot['design_version']}-runtime-go.md"
    return hash_bytes(design.read_bytes())


@dataclass(frozen=True, slots=True)
class WorkbenchSetup:
    """Workbench を有効にするために起動時だけ受け取る値。

    ブラウザーからは 1 Byte も渡せない。**CLI の引数だけが入口である。**
    """

    workspace: Path
    runtime_profile: Path | None
    workspace_label: str
    auth_session: str
    runner: Any = None
    #: 能力検証の差し替え口。**試験専用で、`cli.py` は決して渡さない。**
    #:
    #: `None` のまま組むと `CliRuntimeProfiles` が実測 Probe を使う。承認や
    #: 安全制限を飛ばす Flag ではなく、境界の測定方法だけを差し替える。
    #: `tests/integration/workbench/test_boundary_gate.py` が「CLI から渡す経路が
    #: 無いこと」を検査している。
    boundary_probe: Any = None


def build_services(
    *,
    repo_root: Path,
    operator_auth_session: str | None = None,
    database_path: Path,
    artifact_root: Path,
    clock: Clock | None = None,
    id_source: IdSource | None = None,
    workbench: WorkbenchSetup | None = None,
) -> LocalUiServices:
    """保存先を用意し、Service を組む。**Migration はここで通す。**"""
    if operator_auth_session is not None:
        LocalAuthority().subject(operator_auth_session)
    resolved_clock = clock or SystemClock()
    resolved_ids = id_source or UuidSource()

    database_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_root.mkdir(parents=True, exist_ok=True)

    factory = ConnectionFactory(database_path)
    migrate(factory, recorded_at=resolved_clock.now())
    # `ThreadingHTTPServer` は Request ごとに別 Thread を作る。共有接続を
    # 素のまま使うと `check_same_thread` で落ちる。`server.py` が Request を
    # 1 件ずつへ直列化したうえで、この宣言と組で使う。
    connection = factory.connect(ConnectionRole.RUNTIME, cross_thread=True)

    policy = MaskingPolicy.load(repo_root)
    # Span を 1 つも提案しない Masker。Reject 分類は Scan#1 が先に落とす。
    gate = MaskingPolicyGate(
        MaskingPipeline(policy, StaticPhraseMasker(phrases={}), repo_root=repo_root)
    )

    unit_of_work = SqliteUnitOfWork(factory, connection)
    artifacts = TransactionalArtifactStore(
        inner=ArtifactStore(
            FilesystemArtifactCas(artifact_root),
            SqliteArtifactManifestRepository(connection),
        ),
        unit_of_work=unit_of_work,
    )
    # **Catalog は 1 つだけ組む。** 2 つ作ると、名乗る版が食い違いうる。
    schema_catalog = CoreSchemaRegistry.bundled()
    conversations = ConversationService(
        unit_of_work=unit_of_work,
        conversations=SqliteConversationRepository(connection),
        messages=SqliteConversationMessageRepository(connection),
        artifacts=artifacts,
        masking_gate=gate,
        snapshots=SqliteConversationSnapshotRepository(connection),
        schema_catalog=schema_catalog,
        design_sha256=design_hash(repo_root),
    )
    counter = ByteBoundTokenCounter()
    context = ChatContextService(
        conversations=SqliteConversationRepository(connection),
        messages=SqliteConversationMessageRepository(connection),
        artifacts=artifacts,
        token_counter=counter,
    )
    gateway: WorkbenchGateway | None = None
    closers: list[Any] = []
    if workbench is not None:
        gateway, closers = _build_workbench(
            repo_root=repo_root,
            database_path=database_path,
            artifact_root=artifact_root,
            factory=factory,
            setup=workbench,
            clock=resolved_clock,
            ids=resolved_ids,
        )

    return LocalUiServices(
        operator_auth_session=operator_auth_session,
        repo_root=repo_root,
        database_path=database_path,
        artifact_root=artifact_root,
        conversations=conversations,
        context=context,
        token_counter=counter,
        design_sha256=design_hash(repo_root),
        clock=resolved_clock,
        id_source=resolved_ids,
        # 本文長の上限は Masking Policy の正本から採る。UI が数を決めない。
        max_body_bytes=policy.max_maskable_bytes,
        connection=connection,
        # Mock Adapter は外部 I/O を持たない。**ここが唯一の組み立て場所である。**
        provider=DeterministicMockProvider(adapter_version=MOCK_ADAPTER_VERSION),
        schema_catalog=schema_catalog,
        workbench=gateway,
        workbench_close=(lambda: [closer() for closer in closers]) if closers else None,
    )


def _build_workbench(
    *,
    repo_root: Path,
    database_path: Path,
    artifact_root: Path,
    factory: ConnectionFactory,
    setup: WorkbenchSetup,
    clock: Clock,
    ids: IdSource,
) -> tuple[WorkbenchGateway, list[Any]]:
    """Workbench を 2 本の Connection で組む。**Worker は自分の接続を持つ。**"""
    policy = FilesystemPolicy.load(repo_root)
    worktree = LocalWorktree(setup.workspace, policy)
    identity = _harness_runtime_identity(repo_root)
    closers: list[Any] = []
    try:
        chatgpt_connection, chatgpt_generation, chatgpt_closers = build_chatgpt_connection(
            factory=factory, ids=ids
        )
        closers.extend(chatgpt_closers)
        request_side = build_workbench_service(
            repo_root=repo_root,
            database_path=database_path,
            artifact_root=artifact_root,
            workspace=setup.workspace,
            runtime_profile=setup.runtime_profile,
            workspace_label=setup.workspace_label,
            clock=clock,
            ids=ids,
            runtime_identity=identity,
            factory=factory,
            worktree=worktree,
            runner=setup.runner,
            boundary_probe=setup.boundary_probe,
            chatgpt=chatgpt_generation,
        )
        closers.append(request_side.close)
        worker_side = build_workbench_service(
            repo_root=repo_root,
            database_path=database_path,
            artifact_root=artifact_root,
            workspace=setup.workspace,
            runtime_profile=setup.runtime_profile,
            workspace_label=setup.workspace_label,
            clock=clock,
            ids=ids,
            runtime_identity=identity,
            factory=factory,
            worktree=worktree,
            runner=setup.runner,
            boundary_probe=setup.boundary_probe,
            chatgpt=chatgpt_generation,
        )
        closers.append(worker_side.close)
        closers.append(worktree.close)
    except BaseException:
        for closer in reversed(closers):
            closer()
        worktree.close()
        raise
    gateway = WorkbenchGateway(
        request_service=request_side.service,
        worker_service=worker_side.service,
        auth_session=setup.auth_session,
        chatgpt_connection=chatgpt_connection,
    )
    return gateway, closers


def _harness_runtime_identity(repo_root: Path) -> str:
    """Harness 自身の同一性。**起動時に 1 度だけ確定する。**

    読み込み済みの Python Code は実行中に差し替わらない。CLI 実行体の同一性は
    毎回 `CliRuntimeProfiles.verify` が計算し直す。両者を混同しない。
    """
    from harness.infrastructure.local_workflow_runtime import current_runtime_identity

    try:
        return current_runtime_identity(repo_root)
    except OSError as error:
        raise HarnessError(
            _ErrorCode.RUNTIME_SPEC_MISMATCH, "harness runtime identity is unavailable"
        ) from error
