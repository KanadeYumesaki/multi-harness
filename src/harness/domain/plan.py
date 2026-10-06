"""MVP0-Aの決定的なExecution Plan構築。

Plan Contentは意味的に同じ入力から必ず同じHashを導出し、個別RunのAuthority
情報は別のHashへ分離する。Domain層のため、Clock、UUID、Filesystem、DB、Provider
SDKへ依存しない。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import HASH_PROFILE_VERSION, ContentHash, hash_canonical
from harness.domain.timestamps import canonical_timestamp, timestamp_seconds

__all__ = [
    "ExecutionPlan",
    "PlanAction",
    "PlanAuthority",
    "PlanBuildInput",
    "build_execution_plan",
]


@dataclass(frozen=True, slots=True)
class PlanAction:
    """ランダムIDを含まないAction Graph Node。

    ``semantic_key`` はAction Type、正規化済み入出力、依存Action Keyだけから計算し、
    発行順やAction IDには依存しない。
    """

    action_type: str
    normalized_inputs: Mapping[str, Any]
    normalized_outputs: Mapping[str, Any]
    dependency_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.action_type:
            raise ValueError("action_type must not be empty")
        if any(not key.startswith("action:sha256:") for key in self.dependency_keys):
            raise ValueError("dependency_keys must contain semantic action keys")

    @property
    def semantic_key(self) -> str:
        content = {
            "action_type": self.action_type,
            "inputs": dict(self.normalized_inputs),
            "outputs": dict(self.normalized_outputs),
            "dependencies": sorted(self.dependency_keys),
        }
        return f"action:{hash_canonical(content, artifact_type='semantic-action', schema_major=1)}"

    def projection(self) -> dict[str, Any]:
        return {
            "semantic_action_key": self.semantic_key,
            "action_type": self.action_type,
            "normalized_inputs": dict(self.normalized_inputs),
            "normalized_outputs": dict(self.normalized_outputs),
            "dependency_keys": sorted(self.dependency_keys),
        }


@dataclass(frozen=True, slots=True)
class PlanBuildInput:
    """外部状態を凍結後にPlannerへ渡す唯一の入力。"""

    intent_hash: ContentHash
    workspace_snapshot_hash: ContentHash
    input_read_evidence_hash: ContentHash
    context_bundle_hash: ContentHash
    policy_snapshot_hash: ContentHash
    token_profile_hash: ContentHash
    schema_set_hash: ContentHash
    capability_snapshot_hash: ContentHash | None
    entitlement_snapshot_hash: ContentHash | None
    normalized_actions: tuple[PlanAction, ...]
    runtime_envelope_spec_hash: ContentHash
    invocation_manifest_hash: ContentHash
    token_budget_policy_hash: ContentHash
    control_data_policy_hash: ContentHash
    planner_algorithm_version: str

    def __post_init__(self) -> None:
        if not self.normalized_actions:
            raise ValueError("normalized_actions must not be empty")
        if not self.planner_algorithm_version:
            raise ValueError("planner_algorithm_version must not be empty")


@dataclass(frozen=True, slots=True)
class PlanAuthority:
    """個別Runへ束縛する非決定的Authority Envelope。"""

    run_id: str
    execution_plan_id: str
    plan_version: int
    issued_at: str
    expires_at: str
    planner_identity: str
    authority_scope: str

    def __post_init__(self) -> None:
        if not all(
            (
                self.run_id,
                self.execution_plan_id,
                self.planner_identity,
                self.authority_scope,
            )
        ):
            raise ValueError("authority identity fields must not be empty")
        if self.plan_version < 1:
            raise ValueError("plan_version must be >= 1")
        canonical_timestamp(self.issued_at)
        canonical_timestamp(self.expires_at)
        if timestamp_seconds(self.expires_at) <= timestamp_seconds(self.issued_at):
            raise ValueError("expires_at must be later than issued_at")

    def projection(self, plan_content_hash: ContentHash) -> dict[str, Any]:
        return {
            "plan_content_hash": str(plan_content_hash),
            "run_id": self.run_id,
            "execution_plan_id": self.execution_plan_id,
            "plan_version": self.plan_version,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "planner_identity": self.planner_identity,
            "authority_scope": self.authority_scope,
            "hash_profile_version": HASH_PROFILE_VERSION,
        }


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """承認前に完全解決されたPlanとその二層Hash。"""

    authority: PlanAuthority
    action_graph: Mapping[str, Any]
    plan_content_projection: Mapping[str, Any]
    plan_content_hash: ContentHash
    execution_plan_hash: ContentHash
    content_hash: ContentHash

    @property
    def run_id(self) -> str:
        return self.authority.run_id

    def assert_integrity(self) -> None:
        """保存済みPlanのContent／Authority Hashを再計算して照合する。"""
        expected_content = hash_canonical(
            dict(self.plan_content_projection), artifact_type="plan-content", schema_major=1
        )
        if expected_content != self.plan_content_hash:
            raise HarnessError(
                ErrorCode.PLAN_NONDETERMINISTIC, "stored plan content hash does not match"
            )
        expected_authority = hash_canonical(
            self.authority.projection(self.plan_content_hash),
            artifact_type="execution-plan-authority",
            schema_major=1,
        )
        if expected_authority != self.execution_plan_hash:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH, "stored execution plan authority does not match"
            )

    def to_record(self, *, record_id: str, created_at: str, producer: str) -> dict[str, Any]:
        """Core Schemaへ渡せる保存Recordを返す。

        ``content_hash`` はRecordの監査用Hashであり、Plan Content HashやAuthority
        Hashを置き換えない。
        """
        input_projection = self.plan_content_projection
        return {
            "schema_name": "ExecutionPlan",
            "schema_version": "1.0.0",
            "record_id": record_id,
            "run_id": self.authority.run_id,
            "created_at": created_at,
            "producer": producer,
            "content_hash": str(self.content_hash),
            "execution_plan_id": self.authority.execution_plan_id,
            "plan_version": self.authority.plan_version,
            "plan_content_hash": str(self.plan_content_hash),
            "execution_plan_hash": str(self.execution_plan_hash),
            "planner_identity": self.authority.planner_identity,
            "planner_algorithm_version": input_projection["planner_algorithm_version"],
            "authority_scope": self.authority.authority_scope,
            "hash_profile_version": HASH_PROFILE_VERSION,
            "issued_at": self.authority.issued_at,
            "expires_at": self.authority.expires_at,
            "schema_set_hash": input_projection["schema_set_hash"],
        }


def build_execution_plan(build_input: PlanBuildInput, authority: PlanAuthority) -> ExecutionPlan:
    """同一Frozen Inputを二度組み立て、非決定性をFail-Closedで検出する。"""
    first_projection, first_graph = _content_projection(build_input)
    second_projection, second_graph = _content_projection(build_input)
    first_hash = hash_canonical(first_projection, artifact_type="plan-content", schema_major=1)
    second_hash = hash_canonical(second_projection, artifact_type="plan-content", schema_major=1)
    if (
        first_hash != second_hash
        or first_projection != second_projection
        or first_graph != second_graph
    ):
        raise HarnessError(
            ErrorCode.PLAN_NONDETERMINISTIC,
            "identical frozen PlanBuildInput produced different plan content",
        )

    authority_projection = authority.projection(first_hash)
    execution_hash = hash_canonical(
        authority_projection,
        artifact_type="execution-plan-authority",
        schema_major=1,
    )
    record_projection = {
        "plan_content_projection": first_projection,
        "authority": authority_projection,
        "execution_plan_hash": str(execution_hash),
    }
    return ExecutionPlan(
        authority=authority,
        action_graph=MappingProxyType(first_graph),
        plan_content_projection=MappingProxyType(first_projection),
        plan_content_hash=first_hash,
        execution_plan_hash=execution_hash,
        content_hash=hash_canonical(
            record_projection, artifact_type="execution-plan-record", schema_major=1
        ),
    )


def _content_projection(build_input: PlanBuildInput) -> tuple[dict[str, Any], dict[str, Any]]:
    ordered_actions = _topological_actions(build_input.normalized_actions)
    graph = {
        "nodes": [action.semantic_key for action in ordered_actions],
        "edges": [
            {"from": dependency, "to": action.semantic_key}
            for action in ordered_actions
            for dependency in sorted(action.dependency_keys)
        ],
    }
    projection = {
        "intent_hash": str(build_input.intent_hash),
        "workspace_snapshot_hash": str(build_input.workspace_snapshot_hash),
        "input_read_evidence_hash": str(build_input.input_read_evidence_hash),
        "context_bundle_hash": str(build_input.context_bundle_hash),
        "policy_snapshot_hash": str(build_input.policy_snapshot_hash),
        "token_profile_hash": str(build_input.token_profile_hash),
        "schema_set_hash": str(build_input.schema_set_hash),
        "capability_snapshot_hash": _optional_hash(build_input.capability_snapshot_hash),
        "entitlement_snapshot_hash": _optional_hash(build_input.entitlement_snapshot_hash),
        "token_budget_policy_hash": str(build_input.token_budget_policy_hash),
        "control_data_policy_hash": str(build_input.control_data_policy_hash),
        "runtime_envelope_spec_hash": str(build_input.runtime_envelope_spec_hash),
        "invocation_manifest_hash": str(build_input.invocation_manifest_hash),
        "planner_algorithm_version": build_input.planner_algorithm_version,
        "action_graph": graph,
        "hash_profile_version": HASH_PROFILE_VERSION,
    }
    return projection, graph


def _topological_actions(actions: tuple[PlanAction, ...]) -> tuple[PlanAction, ...]:
    by_key = {action.semantic_key: action for action in actions}
    if len(by_key) != len(actions):
        raise HarnessError(ErrorCode.PLAN_NONDETERMINISTIC, "duplicate semantic action key")
    for action in actions:
        unknown = sorted(set(action.dependency_keys).difference(by_key))
        if unknown:
            raise HarnessError(
                ErrorCode.PLAN_NONDETERMINISTIC,
                "action graph contains an unknown dependency key",
            )

    visiting: set[str] = set()
    visited: set[str] = set()
    ordered: list[PlanAction] = []

    def visit(key: str) -> None:
        if key in visited:
            return
        if key in visiting:
            raise HarnessError(ErrorCode.PLAN_NONDETERMINISTIC, "action graph contains a cycle")
        visiting.add(key)
        action = by_key[key]
        for dependency in sorted(action.dependency_keys):
            visit(dependency)
        visiting.remove(key)
        visited.add(key)
        ordered.append(action)

    for key in sorted(by_key):
        visit(key)
    return tuple(ordered)


def _optional_hash(value: ContentHash | None) -> str | None:
    return None if value is None else str(value)
