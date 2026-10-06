"""合成 Task から決定論的 `ExecutionPlan` までを1本にする Application Service。

## この Service が担う範囲

    Task File と宣言 File を Capability 経由で読む
      → 形式・文字コード・Path 境界を検査して分類
      → Context 候補を作り、重複除去・選択・予算判定を通す
      → 圧縮後の Token を同じ予算契約で再計測
      → 最終 payload へ束縛した Frozen Input から ExecutionPlan を Build

**Approval も Effect も起こさない。** Provider も呼ばない。CC-03 の範囲は
Plan の生成までである。呼ばないことは試験が Spy で実測する。

## なぜ Provider を持たないか

予算超過時に Provider 呼出しが起きないことを実測するのに、いちばん確かな
やり方は**依存を持たないこと**である。持っていなければ、呼ぶ経路が無い。
Spy で 0 回を数えるのは、その裏付けにすぎない。

## 圧縮について

任意の候補だけを出典付きMock圧縮へ渡し、圧縮後Tokenを再計測する。
必須Taskと保護情報は圧縮しない。候補の全Read証跡をPlanへ束縛する。

## 時刻の扱い

`now` は Clock Port が解決した値を受け取る。Plan Content へは入らない。
`TokenProfileSnapshot` の時刻は宣言側が凍結して持ち込む（`plan_declaration` の
docstring 参照）。Service がここで現在時刻から Snapshot を作らない。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from harness.domain.context_budget import ContextAssembly
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import HASH_PROFILE_VERSION, ContentHash, hash_canonical
from harness.domain.input_read import ReadDenial
from harness.domain.plan import ExecutionPlan, PlanAuthority, PlanBuildInput
from harness.domain.plan_build_payload import freeze_plan_request
from harness.domain.plan_declaration import PlanDeclaration, parse_plan_declaration
from harness.domain.task_input import TaskFormat, classify_task_bytes, decode_task_text
from harness.ports.executable_digest import ExecutableDigestPort, ExecutablePathMissing
from harness.ports.plan_builder import PlanBuilderPort
from harness.ports.safe_input_reader import ReadEvidence
from harness.ports.task_intake import (
    ContextAssemblyPort,
    InputEnumerationPort,
    InputReadOutcome,
    InputReadPort,
    InputReadRequest,
    VerifiedContextInput,
)

__all__ = [
    "TaskArtifactClassifier",
    "TaskPlanOutcome",
    "TaskPlanRequest",
    "TaskPlanService",
]


class TaskArtifactClassifier:
    """`InputArtifactClassifierPort` を Domain の形式判定へつなぐ。

    Port の形（`payload`＋`ReadEvidence`）と Domain の規則（Path と Bytes）を
    合わせるだけの薄い層である。判定規則をここへ書かない。
    """

    def classify(self, payload: bytes, evidence: ReadEvidence) -> str:
        return classify_task_bytes(evidence.requested_path, payload).value


@dataclass(frozen=True, slots=True)
class TaskPlanRequest:
    """1回の Plan 生成要求。

    採番 ID と時刻はここに集める。**どれも Plan Content へ入らない**
    （`PlanAuthority` 側だけが持つ／不変条件#4）。
    """

    stream_id: str
    capability_id: str
    task_relative_path: str
    declaration_relative_path: str
    task_read_decision_id: str
    declaration_read_decision_id: str
    bundle_id: str
    receipt_id: str
    run_id: str
    execution_plan_id: str
    issued_at: str
    expires_at: str
    now: str
    workspace_snapshot_id: str
    candidate_directories: tuple[str, ...] = ()
    compress_candidates: bool = False


@dataclass(frozen=True, slots=True)
class TaskPlanOutcome:
    """成功時の結果。拒否は例外か `denied` な Read Outcome で表す。"""

    task_read: InputReadOutcome
    declaration_read: InputReadOutcome
    task_format: TaskFormat
    declaration: PlanDeclaration
    assembly: ContextAssembly
    plan: ExecutionPlan
    frozen_request: bytes


class TaskPlanService:
    """Task Loader・Context・Plan を1本につなぐ。"""

    def __init__(
        self,
        *,
        input_read: InputReadPort,
        input_enumeration: InputEnumerationPort | None = None,
        context: ContextAssemblyPort,
        executable_digest: ExecutableDigestPort,
        plan_builder: PlanBuilderPort,
        schema_set_hash: ContentHash,
        policy_snapshot_hash: ContentHash,
        control_data_policy_hash: ContentHash,
    ) -> None:
        self._input_read = input_read
        self._input_enumeration = input_enumeration
        self._context = context
        self._executable_digest = executable_digest
        self._plan_builder = plan_builder
        self._schema_set_hash = schema_set_hash
        self._policy_snapshot_hash = policy_snapshot_hash
        self._control_data_policy_hash = control_data_policy_hash

    # -- 入口 ------------------------------------------------------------

    def plan(self, request: TaskPlanRequest) -> TaskPlanOutcome:
        declaration_read = self._read(
            request, request.declaration_relative_path, request.declaration_read_decision_id
        )
        declaration = self._parse_declaration(declaration_read)
        self._require_declared_executable(declaration)

        task_read = self._read(request, request.task_relative_path, request.task_read_decision_id)
        payload, evidence = self._require_payload(task_read, request.task_relative_path)
        task_format = classify_task_bytes(request.task_relative_path, payload)

        candidates = self._collect_candidates(request)
        assembly = self._assemble_context(request, declaration, payload, evidence, candidates)
        plan, frozen_request = self._build_plan(request, declaration, assembly, evidence)
        return TaskPlanOutcome(
            task_read=task_read,
            declaration_read=declaration_read,
            task_format=task_format,
            declaration=declaration,
            assembly=assembly,
            plan=plan,
            frozen_request=frozen_request,
        )

    def _collect_candidates(self, request: TaskPlanRequest) -> tuple[VerifiedContextInput, ...]:
        if not request.candidate_directories:
            return ()
        if self._input_enumeration is None or len(request.candidate_directories) > 32:
            raise HarnessError(
                ErrorCode.PATH_OUTSIDE_CAPABILITY, "candidate request exceeds bounds"
            )
        paths: set[str] = set()
        for directory in sorted(set(request.candidate_directories)):
            result = self._input_enumeration.enumerate(
                InputReadRequest(
                    stream_id=request.stream_id,
                    read_decision_id=request.run_id + ":enumerate:" + directory,
                    capability_id=request.capability_id,
                    relative_path=directory,
                )
            )
            if isinstance(result, ReadDenial):
                raise HarnessError(ErrorCode(result.reason.value), "candidate enumeration denied")
            paths.update(result)
            if len(paths) > 4096:
                raise HarnessError(ErrorCode.PATH_OUTSIDE_CAPABILITY, "too many input candidates")
        candidates = []
        total_bytes = 0
        for path in sorted(paths):
            if path in (request.task_relative_path, request.declaration_relative_path):
                continue
            outcome = self._read(request, path, request.run_id + ":candidate:" + path)
            payload, evidence = self._require_payload(outcome, path)
            total_bytes += len(payload)
            if total_bytes > 64 * 1024 * 1024:
                raise HarnessError(
                    ErrorCode.PATH_OUTSIDE_CAPABILITY, "candidate bytes exceed bounds"
                )
            candidates.append(
                VerifiedContextInput(
                    text=decode_task_text(payload, where="context candidate"),
                    evidence=evidence,
                    compressible=request.compress_candidates,
                )
            )
        return tuple(candidates)

    # -- 読取 ------------------------------------------------------------

    def _read(
        self, request: TaskPlanRequest, relative_path: str, read_decision_id: str
    ) -> InputReadOutcome:
        return self._input_read.read(
            InputReadRequest(
                stream_id=request.stream_id,
                read_decision_id=read_decision_id,
                capability_id=request.capability_id,
                relative_path=relative_path,
            )
        )

    @staticmethod
    def _require_payload(
        outcome: InputReadOutcome, relative_path: str
    ) -> tuple[bytes, ReadEvidence]:
        """拒否された読取から先へ進まない。

        `payload=None` を空 Bytes へ倒さない。倒せば「読めなかった」が
        「空のファイルだった」になり、そこから Plan が立ってしまう。
        """
        if outcome.denied or outcome.payload is None or outcome.read_evidence is None:
            raise HarnessError(
                outcome.error_code or ErrorCode.PATH_OUTSIDE_CAPABILITY,
                f"input read for {relative_path!r} was denied; no plan is built",
            )
        return outcome.payload, outcome.read_evidence

    def _parse_declaration(self, outcome: InputReadOutcome) -> PlanDeclaration:
        payload, evidence = self._require_payload(outcome, "<plan declaration>")
        text = decode_task_text(payload, where=f"plan declaration {evidence.requested_path!r}")
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                f"plan declaration is not valid JSON: {exc.msg} at line {exc.lineno}",
            ) from None
        if not isinstance(document, dict):
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                "plan declaration must be a JSON object",
            )
        return parse_plan_declaration(document)

    def _require_declared_executable(self, declaration: PlanDeclaration) -> None:
        """宣言された実行体 SHA-256 を実測と突き合わせる。

        §6 制約「Executable は絶対 Path ＋ SHA-256」。宣言だけで通すと、
        存在しない実行体や別物の実行体で Plan が立つ。
        """
        envelope = declaration.runtime_envelope
        try:
            measured = self._executable_digest.digest(envelope.executable_path)
        except ExecutablePathMissing as exc:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                f"declared executable cannot be measured: {exc}",
            ) from None
        if measured != envelope.executable_sha256:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "declared executable_sha256 does not match the measured digest",
            )

    # -- Context ---------------------------------------------------------

    def _assemble_context(
        self,
        request: TaskPlanRequest,
        declaration: PlanDeclaration,
        payload: bytes,
        evidence: ReadEvidence,
        candidates: tuple[VerifiedContextInput, ...] = (),
    ) -> ContextAssembly:
        """読み取った Task を Context 候補にして選択・予算判定を通す。

        Role をここで指定しない。`ContextAssemblyPort` の実装が
        `UNTRUSTED_ARTIFACT_DATA` に固定する。**呼出側が Role を選べる形に
        しない**（§3.6 手順2）。選べれば、入力由来の本文へ Control Role を
        与える経路ができる。Source 形式でも扱いは変わらない。
        """
        text = decode_task_text(payload, where=f"task input {evidence.requested_path!r}")
        return self._context.build_verified_input_bundle(
            text=text,
            evidence=evidence,
            policy=declaration.token_budget,
            profile=declaration.token_profile,
            bundle_id=request.bundle_id,
            receipt_id=request.receipt_id,
            now=request.now,
            candidates=candidates,
        )

    # -- Plan ------------------------------------------------------------

    def _build_plan(
        self,
        request: TaskPlanRequest,
        declaration: PlanDeclaration,
        assembly: ContextAssembly,
        evidence: ReadEvidence,
    ) -> tuple[ExecutionPlan, bytes]:
        bundle = assembly.bundle
        instruction_hash = declaration.intent.semantic_hash
        request_artifact_hash = evidence.content_hash
        invocation_hash = declaration.invocation.semantic_hash(
            context_bundle_hash=bundle.bundle_hash,
            message_role_manifest_hash=bundle.message_role_manifest_hash,
            control_data_policy_hash=self._control_data_policy_hash,
            token_profile_snapshot_hash=bundle.token_profile_snapshot_hash,
            instruction_hash=instruction_hash,
            request_artifact_hash=request_artifact_hash,
        )
        build_input = PlanBuildInput(
            intent_hash=instruction_hash,
            workspace_snapshot_hash=self._workspace_snapshot_hash(request, evidence),
            input_read_evidence_hash=bundle.input_read_evidence_hash,
            context_bundle_hash=bundle.bundle_hash,
            policy_snapshot_hash=self._policy_snapshot_hash,
            token_profile_hash=bundle.token_profile_snapshot_hash,
            schema_set_hash=self._schema_set_hash,
            capability_snapshot_hash=bundle.input_read_capability_set_hash,
            # MVP0-A は Entitlement を持たない。空 Hash を発明せず None を渡す。
            entitlement_snapshot_hash=None,
            normalized_actions=declaration.actions,
            runtime_envelope_spec_hash=declaration.runtime_envelope.semantic_hash,
            invocation_manifest_hash=invocation_hash,
            token_budget_policy_hash=bundle.token_budget_policy_hash,
            control_data_policy_hash=self._control_data_policy_hash,
            planner_algorithm_version=declaration.planner_algorithm_version,
        )
        authority = PlanAuthority(
            run_id=request.run_id,
            execution_plan_id=request.execution_plan_id,
            plan_version=1,
            issued_at=request.issued_at,
            expires_at=request.expires_at,
            planner_identity=declaration.planner_identity,
            authority_scope=declaration.authority_scope,
        )
        frozen = freeze_plan_request(build_input, authority)
        return self._plan_builder.build(frozen), frozen

    @staticmethod
    def _workspace_snapshot_hash(request: TaskPlanRequest, evidence: ReadEvidence) -> ContentHash:
        """実際に読んだものだけから Workspace Snapshot Hash を作る。

        列挙順に依存しないよう、Key を明示して並べる（不変条件#6）。
        読んでいない File を数に入れない。
        """
        projection: dict[str, Any] = {
            "capability_id": evidence.capability_id,
            "mount_id": evidence.mount_id,
            "read_resources": [
                {
                    "requested_path": evidence.requested_path,
                    "canonical_path": evidence.canonical_path,
                    "content_hash": str(evidence.content_hash),
                    "size_bytes": evidence.size_bytes,
                }
            ],
            "hash_profile_version": HASH_PROFILE_VERSION,
        }
        return hash_canonical(projection, artifact_type="workspace-snapshot", schema_major=1)
