"""§3.7.1／§3.7.2 Plan Content の未確定入力を、**人の宣言**として受け取る。

## なぜ「宣言」にするか

`PlanBuildInput` は `intent_hash`／`runtime_envelope_spec_hash`／
`invocation_manifest_hash` を要求する。ところが CC-03 着手時に走査したところ、
この3つを生む Production は1件も無かった（`domain/plan.py` が入力として名前を
挙げているだけ）。

ここを Plan 組立て側が埋めると、**Harness が Provider・Model・認証・課金・実行体の
未入力値を自分で埋める**ことになる。それは指示書 §2 が明示的に禁じている。
だから値は人が宣言し、Harness は「宣言を検査して Hash を計算する」役に徹する。
足りない宣言は既定値で補わず停止する（不変条件#9）。

## Semantic Hash から外すもの

§3.7.1「`RuntimeEnvelopeSpec` と `InvocationManifest` の Semantic Hash は、
Record ID、生成時刻、表示用 Metadata を除いた内容 Projection から計算する」。
`InvocationManifest` 制約も「Snapshot Record ID は Semantic Hash へ含めない」と
定める。

したがって宣言型は **`record_id`／`created_at`／`invocation_id` のような採番 ID と
時刻を最初から持たない**。持たなければ Plan Content へ混入しようがない
（不変条件#4）。採番と時刻は `PlanAuthority` 側だけが持つ。

## 列挙値の出所

`os_boundary`／`invocation_mode`／`billing_mode`／`expected_effect`／`risk_level` の
語彙は `schemas/core/*/1.0.0.schema.json` の `enum` と一致する。ここで新しい値を
作らない。MVP0-A が組み立てられるのは `invocation_mode=MOCK` だけである
（§7 条件表：MOCK は Auth／Account／Tenant／Billing を明示値 `NONE`、
External 条件 Field は未設定）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from harness.domain.context_budget import (
    EstimateAssurance,
    TokenBudgetPolicy,
    TokenOverheads,
    TokenProfileSnapshot,
)
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import HASH_PROFILE_VERSION, ContentHash, hash_canonical
from harness.domain.plan import PlanAction

__all__ = [
    "IntentDeclaration",
    "InvocationDeclaration",
    "PlanDeclaration",
    "RuntimeEnvelopeDeclaration",
    "parse_plan_declaration",
]

# `schemas/core/ActionIntent/1.0.0.schema.json` の `risk_level` enum。
_RISK_LEVELS: Final[frozenset[str]] = frozenset({"LOW", "MEDIUM", "HIGH", "CRITICAL"})
# `schemas/core/RuntimeEnvelopeSpec/1.0.0.schema.json` の `os_boundary` enum。
_OS_BOUNDARIES: Final[frozenset[str]] = frozenset({"LINUX"})
# `schemas/core/InvocationManifest/1.0.0.schema.json` の3つの enum。
_INVOCATION_MODES: Final[frozenset[str]] = frozenset({"MOCK", "LOCAL", "EXTERNAL"})
_BILLING_MODES: Final[frozenset[str]] = frozenset({"FREE", "PAID", "UNKNOWN"})
_EXPECTED_EFFECTS: Final[frozenset[str]] = frozenset(
    {"NONE", "REMOTE_INVOCATION", "WORKSPACE_WRITE", "EXTERNAL_EFFECT"}
)

#: MVP0-A で組み立ててよい Invocation Mode。外部通信を持つ Mode は作らない。
MVP0A_INVOCATION_MODE: Final[str] = "MOCK"


def _reject(message: str) -> HarnessError:
    return HarnessError(ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, message)


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise _reject(f"{field} must be a non-empty string")
    return value


def _require_choice(value: object, field: str, allowed: frozenset[str]) -> str:
    text = _require_text(value, field)
    if text not in allowed:
        # 語彙外を既定値へ倒さない。倒すと宣言していない Mode で Plan が立つ。
        raise _reject(f"{field}={text!r} is not one of {sorted(allowed)}")
    return text


def _require_positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise _reject(f"{field} must be an integer >= 1")
    return value


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field)


def _require_keys(document: Mapping[str, Any], *, required: frozenset[str], where: str) -> None:
    """未知 Key と欠落 Key を両方拒否する。

    未知 Key を黙って捨てると、綴り違いの宣言が「宣言しなかった」と同じ扱いに
    なる。宣言が効いていないことに気付けない。
    """
    keys = frozenset(document)
    missing = sorted(required - keys)
    if missing:
        raise _reject(f"{where}: missing keys {missing}")


def _reject_unknown(document: Mapping[str, Any], *, known: frozenset[str], where: str) -> None:
    unknown = sorted(frozenset(document) - known)
    if unknown:
        raise _reject(f"{where}: unknown keys {unknown}")


@dataclass(frozen=True, slots=True)
class IntentDeclaration:
    """`ActionIntent` のうち Semantic Hash へ載る宣言部分。

    `action_id`／`record_id`／`created_at` は持たない（§3.7.1）。
    """

    action_type: str
    data_classification: str
    trust_level: str
    risk_level: str
    maximum_attempts: int
    timeout_seconds: int

    def __post_init__(self) -> None:
        _require_text(self.action_type, "intent.action_type")
        _require_text(self.data_classification, "intent.data_classification")
        _require_text(self.trust_level, "intent.trust_level")
        _require_choice(self.risk_level, "intent.risk_level", _RISK_LEVELS)
        _require_positive_int(self.maximum_attempts, "intent.maximum_attempts")
        _require_positive_int(self.timeout_seconds, "intent.timeout_seconds")

    def projection(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "data_classification": self.data_classification,
            "trust_level": self.trust_level,
            "risk_level": self.risk_level,
            "maximum_attempts": self.maximum_attempts,
            "timeout_seconds": self.timeout_seconds,
            "hash_profile_version": HASH_PROFILE_VERSION,
        }

    @property
    def semantic_hash(self) -> ContentHash:
        return hash_canonical(self.projection(), artifact_type="action-intent", schema_major=1)


@dataclass(frozen=True, slots=True)
class RuntimeEnvelopeDeclaration:
    """`RuntimeEnvelopeSpec` のうち Semantic Hash へ載る宣言部分。

    `executable_sha256` は**宣言値**である。実物との一致は Application 層が
    `ExecutableDigestPort` で実測して突き合わせる。宣言だけで通さない。
    """

    runtime_spec_version: str
    runtime_type: str
    launcher_version: str
    executable_path: str
    executable_sha256: ContentHash
    argv: tuple[str, ...]
    working_directory: str
    workspace_id: str
    os_boundary: str
    provider: str
    adapter: str
    auth_route: str
    model: str | None = None
    model_digest: str | None = None
    output_schema_hash: ContentHash | None = None
    environment_allowlist_hash: ContentHash | None = None

    def __post_init__(self) -> None:
        _require_text(self.runtime_spec_version, "runtime_envelope.runtime_spec_version")
        _require_text(self.runtime_type, "runtime_envelope.runtime_type")
        _require_text(self.launcher_version, "runtime_envelope.launcher_version")
        _require_text(self.working_directory, "runtime_envelope.working_directory")
        _require_text(self.workspace_id, "runtime_envelope.workspace_id")
        _require_text(self.provider, "runtime_envelope.provider")
        _require_text(self.adapter, "runtime_envelope.adapter")
        _require_text(self.auth_route, "runtime_envelope.auth_route")
        _require_choice(self.os_boundary, "runtime_envelope.os_boundary", _OS_BOUNDARIES)
        path = _require_text(self.executable_path, "runtime_envelope.executable_path")
        if not path.startswith("/"):
            # §6 制約「Executable は絶対 Path ＋ SHA-256」。
            raise _reject("runtime_envelope.executable_path must be absolute")
        if not self.argv:
            raise _reject("runtime_envelope.argv must not be empty")
        for index, item in enumerate(self.argv):
            _require_text(item, f"runtime_envelope.argv[{index}]")

    def projection(self) -> dict[str, Any]:
        return {
            "runtime_spec_version": self.runtime_spec_version,
            "runtime_type": self.runtime_type,
            "launcher_version": self.launcher_version,
            "executable_path": self.executable_path,
            "executable_sha256": str(self.executable_sha256),
            # `argv` は宣言順を保つ。並べ替えると起動意味が変わる。
            "argv": list(self.argv),
            "working_directory": self.working_directory,
            "workspace_id": self.workspace_id,
            "os_boundary": self.os_boundary,
            "provider": self.provider,
            "adapter": self.adapter,
            "auth_route": self.auth_route,
            "model": self.model,
            "model_digest": self.model_digest,
            "output_schema_hash": _optional_hash(self.output_schema_hash),
            "environment_allowlist_hash": _optional_hash(self.environment_allowlist_hash),
            "hash_profile_version": HASH_PROFILE_VERSION,
        }

    @property
    def semantic_hash(self) -> ContentHash:
        return hash_canonical(
            self.projection(), artifact_type="runtime-envelope-spec", schema_major=1
        )


@dataclass(frozen=True, slots=True)
class InvocationDeclaration:
    """`InvocationManifest` のうち**人が宣言する**部分。

    `context_bundle_hash`／`message_role_manifest_hash`／
    `token_profile_snapshot_hash`／`control_data_policy_hash` は宣言しない。
    それらは組み上がった Context から**導出**する値であり、人が書き写すと
    実物とずれる。`semantic_hash()` が引数で受け取って束縛する。
    """

    invocation_mode: str
    provider: str
    billing_mode: str
    expected_effect: str
    model: str | None = None
    provider_operation: str | None = None
    output_schema_hash: ContentHash | None = None

    def __post_init__(self) -> None:
        mode = _require_choice(
            self.invocation_mode, "invocation.invocation_mode", _INVOCATION_MODES
        )
        _require_text(self.provider, "invocation.provider")
        billing = _require_choice(self.billing_mode, "invocation.billing_mode", _BILLING_MODES)
        effect = _require_choice(
            self.expected_effect, "invocation.expected_effect", _EXPECTED_EFFECTS
        )
        if mode != MVP0A_INVOCATION_MODE:
            # 外部通信を持つ Mode を組み立てない。Scope 外の経路を作らない。
            raise _reject(f"invocation.invocation_mode must be {MVP0A_INVOCATION_MODE} in MVP0-A")
        if billing == "UNKNOWN":
            # §7 制約「`billing_mode=UNKNOWN` では Runtime GO 不可」。
            raise _reject("invocation.billing_mode must not be UNKNOWN")
        if effect != "NONE":
            # §7 制約「`expected_effect=NONE` は Mock／Local Read-only の初期値」。
            # CC-03 は Effect を起こさない。起こす宣言をここで受理しない。
            raise _reject("invocation.expected_effect must be NONE while no effect is planned")

    def semantic_hash(
        self,
        *,
        context_bundle_hash: ContentHash,
        message_role_manifest_hash: ContentHash,
        control_data_policy_hash: ContentHash,
        token_profile_snapshot_hash: ContentHash,
        instruction_hash: ContentHash,
        request_artifact_hash: ContentHash,
    ) -> ContentHash:
        """宣言部分と導出部分をまとめて Semantic Hash にする。

        導出部分を含めるのは §7 制約「Exact Provider／Model … と Snapshot Hash を
        Plan Content へ含める」に従うためである。Context が変われば Manifest Hash も
        変わり、Plan Content Hash も変わる。
        """
        projection = {
            "invocation_mode": self.invocation_mode,
            "provider": self.provider,
            "model": self.model,
            "billing_mode": self.billing_mode,
            "provider_operation": self.provider_operation,
            "expected_effect": self.expected_effect,
            "output_schema_hash": _optional_hash(self.output_schema_hash),
            "context_bundle_hash": str(context_bundle_hash),
            "message_role_manifest_hash": str(message_role_manifest_hash),
            "control_data_policy_hash": str(control_data_policy_hash),
            "token_profile_snapshot_hash": str(token_profile_snapshot_hash),
            "instruction_hash": str(instruction_hash),
            "request_artifact_hash": str(request_artifact_hash),
            "hash_profile_version": HASH_PROFILE_VERSION,
        }
        return hash_canonical(projection, artifact_type="invocation-manifest", schema_major=1)


@dataclass(frozen=True, slots=True)
class PlanDeclaration:
    """1回の Plan 生成で人が宣言する全体。

    `token_profile` と `token_budget` も宣言に含める。`TokenProfileSnapshot` の
    `retrieved_at`／`expires_at` は §1.11「意味のある Snapshot 時刻は Content へ
    含める」により `snapshot_hash` の入力であり、そこから
    `token_profile_hash` を通って Plan Content Hash へ届く。

    ここを Harness が現在時刻から作ると、**同じ Task と同じ設定でも実行のたびに
    Plan Content Hash が変わる**。凍結した入力から2回 Build する意味が消える
    （不変条件#5）。だから Snapshot は人が凍結して渡す。
    """

    planner_algorithm_version: str
    planner_identity: str
    authority_scope: str
    intent: IntentDeclaration
    runtime_envelope: RuntimeEnvelopeDeclaration
    invocation: InvocationDeclaration
    token_profile: TokenProfileSnapshot
    token_budget: TokenBudgetPolicy
    actions: tuple[PlanAction, ...]

    def __post_init__(self) -> None:
        _require_text(self.planner_algorithm_version, "planner_algorithm_version")
        _require_text(self.planner_identity, "planner_identity")
        _require_text(self.authority_scope, "authority_scope")
        if not self.actions:
            raise _reject("actions must not be empty")


def _optional_hash(value: ContentHash | None) -> str | None:
    return None if value is None else str(value)


def _parse_hash(value: object, field: str) -> ContentHash:
    text = _require_text(value, field)
    try:
        return ContentHash.parse(text)
    except ValueError as exc:
        raise _reject(f"{field}: {exc}") from None


def _parse_optional_hash(value: object, field: str) -> ContentHash | None:
    return None if value is None else _parse_hash(value, field)


def _parse_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _reject(f"{field} must be an object")
    for key in value:
        if not isinstance(key, str):
            raise _reject(f"{field} keys must be strings")
    return value


_OVERHEAD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "system_message_overhead",
        "developer_message_overhead",
        "tool_definition_overhead",
        "per_message_overhead",
        "structured_output_overhead",
        "streaming_frame_overhead",
        "retry_fallback_reservation",
    }
)

_PROFILE_REQUIRED: Final[frozenset[str]] = frozenset(
    {
        "snapshot_id",
        "provider",
        "model",
        "tokenizer_name",
        "tokenizer_version",
        "counting_adapter_version",
        "context_limit",
        "maximum_output_limit",
        "estimate_assurance",
        "overheads",
        "retrieved_at",
        "expires_at",
    }
)

_BUDGET_REQUIRED: Final[frozenset[str]] = frozenset(
    {"policy_id", "total_tokens", "reserved_output_tokens", "reserved_tool_tokens"}
)
_BUDGET_OPTIONAL: Final[frozenset[str]] = frozenset(
    {"safety_margin_tokens", "compression_max_depth", "overflow_policy"}
)


def _require_non_negative_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _reject(f"{field} must be an integer >= 0")
    return value


def _parse_overheads(value: object) -> TokenOverheads:
    document = _parse_mapping(value, "token_profile.overheads")
    _reject_unknown(document, known=_OVERHEAD_KEYS, where="token_profile.overheads")
    _require_keys(document, required=_OVERHEAD_KEYS, where="token_profile.overheads")
    return TokenOverheads(
        system_message_overhead=_require_non_negative_int(
            document["system_message_overhead"], "token_profile.overheads.system_message_overhead"
        ),
        developer_message_overhead=_require_non_negative_int(
            document["developer_message_overhead"],
            "token_profile.overheads.developer_message_overhead",
        ),
        tool_definition_overhead=_require_non_negative_int(
            document["tool_definition_overhead"],
            "token_profile.overheads.tool_definition_overhead",
        ),
        per_message_overhead=_require_non_negative_int(
            document["per_message_overhead"], "token_profile.overheads.per_message_overhead"
        ),
        structured_output_overhead=_require_non_negative_int(
            document["structured_output_overhead"],
            "token_profile.overheads.structured_output_overhead",
        ),
        streaming_frame_overhead=_require_non_negative_int(
            document["streaming_frame_overhead"],
            "token_profile.overheads.streaming_frame_overhead",
        ),
        retry_fallback_reservation=_require_non_negative_int(
            document["retry_fallback_reservation"],
            "token_profile.overheads.retry_fallback_reservation",
        ),
    )


def _parse_token_profile(value: object) -> TokenProfileSnapshot:
    """凍結された `TokenProfileSnapshot` を読む。**Harness が時刻を入れない。**"""
    document = _parse_mapping(value, "token_profile")
    optional = frozenset({"vocabulary_hash"})
    _reject_unknown(document, known=_PROFILE_REQUIRED | optional, where="token_profile")
    _require_keys(document, required=_PROFILE_REQUIRED, where="token_profile")
    assurance_text = _require_text(
        document["estimate_assurance"], "token_profile.estimate_assurance"
    )
    try:
        assurance = EstimateAssurance(assurance_text)
    except ValueError:
        raise _reject(
            f"token_profile.estimate_assurance={assurance_text!r} is not a known assurance"
        ) from None
    try:
        return TokenProfileSnapshot(
            snapshot_id=_require_text(document["snapshot_id"], "token_profile.snapshot_id"),
            provider=_require_text(document["provider"], "token_profile.provider"),
            model=_require_text(document["model"], "token_profile.model"),
            tokenizer_name=_require_text(
                document["tokenizer_name"], "token_profile.tokenizer_name"
            ),
            tokenizer_version=_require_text(
                document["tokenizer_version"], "token_profile.tokenizer_version"
            ),
            counting_adapter_version=_require_text(
                document["counting_adapter_version"], "token_profile.counting_adapter_version"
            ),
            context_limit=_require_positive_int(
                document["context_limit"], "token_profile.context_limit"
            ),
            maximum_output_limit=_require_positive_int(
                document["maximum_output_limit"], "token_profile.maximum_output_limit"
            ),
            estimate_assurance=assurance,
            overheads=_parse_overheads(document["overheads"]),
            retrieved_at=_require_text(document["retrieved_at"], "token_profile.retrieved_at"),
            expires_at=_require_text(document["expires_at"], "token_profile.expires_at"),
            vocabulary_hash=_parse_optional_hash(
                document.get("vocabulary_hash"), "token_profile.vocabulary_hash"
            ),
        )
    except ValueError as exc:
        # Domain の不変条件違反（時刻の前後関係など）を握り潰さない。
        raise _reject(f"token_profile: {exc}") from None


def _parse_token_budget(value: object) -> TokenBudgetPolicy:
    document = _parse_mapping(value, "token_budget")
    _reject_unknown(document, known=_BUDGET_REQUIRED | _BUDGET_OPTIONAL, where="token_budget")
    _require_keys(document, required=_BUDGET_REQUIRED, where="token_budget")
    try:
        return TokenBudgetPolicy(
            policy_id=_require_text(document["policy_id"], "token_budget.policy_id"),
            total_tokens=_require_non_negative_int(
                document["total_tokens"], "token_budget.total_tokens"
            ),
            reserved_output_tokens=_require_non_negative_int(
                document["reserved_output_tokens"], "token_budget.reserved_output_tokens"
            ),
            reserved_tool_tokens=_require_non_negative_int(
                document["reserved_tool_tokens"], "token_budget.reserved_tool_tokens"
            ),
            safety_margin_tokens=_require_non_negative_int(
                document.get("safety_margin_tokens", 0), "token_budget.safety_margin_tokens"
            ),
            compression_max_depth=_require_non_negative_int(
                document.get("compression_max_depth", 1), "token_budget.compression_max_depth"
            ),
            overflow_policy=_require_text(
                document.get("overflow_policy", "FAIL_CLOSED"), "token_budget.overflow_policy"
            ),
        )
    except ValueError as exc:
        raise _reject(f"token_budget: {exc}") from None


def _parse_actions(value: object) -> tuple[PlanAction, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise _reject("actions must be an array")
    parsed: list[PlanAction] = []
    for index, item in enumerate(value):
        where = f"actions[{index}]"
        document = _parse_mapping(item, where)
        _reject_unknown(
            document,
            known=frozenset({"action_type", "normalized_inputs", "normalized_outputs"}),
            where=where,
        )
        _require_keys(
            document,
            required=frozenset({"action_type", "normalized_inputs", "normalized_outputs"}),
            where=where,
        )
        parsed.append(
            PlanAction(
                action_type=_require_text(document["action_type"], f"{where}.action_type"),
                normalized_inputs=dict(
                    _parse_mapping(document["normalized_inputs"], f"{where}.normalized_inputs")
                ),
                normalized_outputs=dict(
                    _parse_mapping(document["normalized_outputs"], f"{where}.normalized_outputs")
                ),
            )
        )
    if not parsed:
        raise _reject("actions must not be empty")
    return tuple(parsed)


def parse_plan_declaration(document: Mapping[str, Any]) -> PlanDeclaration:
    """宣言 Document を検査して値オブジェクトへ変換する。

    欠落・未知 Key・型違い・語彙外はすべて `SCHEMA_CONDITIONAL_VIOLATION` で
    停止する。**既定値で補わない。** 補えば、宣言していない Runtime で Plan が
    立ち、その Plan が承認されうる。
    """
    _reject_unknown(
        document,
        known=frozenset(
            {
                "planner_algorithm_version",
                "planner_identity",
                "authority_scope",
                "intent",
                "runtime_envelope",
                "invocation",
                "token_profile",
                "token_budget",
                "actions",
            }
        ),
        where="<root>",
    )
    _require_keys(
        document,
        required=frozenset(
            {
                "planner_algorithm_version",
                "planner_identity",
                "authority_scope",
                "intent",
                "runtime_envelope",
                "invocation",
                "token_profile",
                "token_budget",
                "actions",
            }
        ),
        where="<root>",
    )

    intent_document = _parse_mapping(document["intent"], "intent")
    intent_keys = frozenset(
        {
            "action_type",
            "data_classification",
            "trust_level",
            "risk_level",
            "maximum_attempts",
            "timeout_seconds",
        }
    )
    _reject_unknown(intent_document, known=intent_keys, where="intent")
    _require_keys(intent_document, required=intent_keys, where="intent")

    envelope_document = _parse_mapping(document["runtime_envelope"], "runtime_envelope")
    envelope_required = frozenset(
        {
            "runtime_spec_version",
            "runtime_type",
            "launcher_version",
            "executable_path",
            "executable_sha256",
            "argv",
            "working_directory",
            "workspace_id",
            "os_boundary",
            "provider",
            "adapter",
            "auth_route",
        }
    )
    envelope_optional = frozenset(
        {"model", "model_digest", "output_schema_hash", "environment_allowlist_hash"}
    )
    _reject_unknown(
        envelope_document, known=envelope_required | envelope_optional, where="runtime_envelope"
    )
    _require_keys(envelope_document, required=envelope_required, where="runtime_envelope")

    argv_value = envelope_document["argv"]
    if isinstance(argv_value, str) or not isinstance(argv_value, Sequence):
        # 文字列の argv は Shell 文字列である。不変条件#8 でそもそも起動できない。
        raise _reject("runtime_envelope.argv must be an array of strings, not a shell string")

    invocation_document = _parse_mapping(document["invocation"], "invocation")
    invocation_required = frozenset(
        {"invocation_mode", "provider", "billing_mode", "expected_effect"}
    )
    invocation_optional = frozenset({"model", "provider_operation", "output_schema_hash"})
    _reject_unknown(
        invocation_document, known=invocation_required | invocation_optional, where="invocation"
    )
    _require_keys(invocation_document, required=invocation_required, where="invocation")

    return PlanDeclaration(
        planner_algorithm_version=_require_text(
            document["planner_algorithm_version"], "planner_algorithm_version"
        ),
        planner_identity=_require_text(document["planner_identity"], "planner_identity"),
        authority_scope=_require_text(document["authority_scope"], "authority_scope"),
        intent=IntentDeclaration(
            action_type=_require_text(intent_document["action_type"], "intent.action_type"),
            data_classification=_require_text(
                intent_document["data_classification"], "intent.data_classification"
            ),
            trust_level=_require_text(intent_document["trust_level"], "intent.trust_level"),
            risk_level=_require_choice(
                intent_document["risk_level"], "intent.risk_level", _RISK_LEVELS
            ),
            maximum_attempts=_require_positive_int(
                intent_document["maximum_attempts"], "intent.maximum_attempts"
            ),
            timeout_seconds=_require_positive_int(
                intent_document["timeout_seconds"], "intent.timeout_seconds"
            ),
        ),
        runtime_envelope=RuntimeEnvelopeDeclaration(
            runtime_spec_version=_require_text(
                envelope_document["runtime_spec_version"], "runtime_envelope.runtime_spec_version"
            ),
            runtime_type=_require_text(
                envelope_document["runtime_type"], "runtime_envelope.runtime_type"
            ),
            launcher_version=_require_text(
                envelope_document["launcher_version"], "runtime_envelope.launcher_version"
            ),
            executable_path=_require_text(
                envelope_document["executable_path"], "runtime_envelope.executable_path"
            ),
            executable_sha256=_parse_hash(
                envelope_document["executable_sha256"], "runtime_envelope.executable_sha256"
            ),
            argv=tuple(
                _require_text(item, f"runtime_envelope.argv[{index}]")
                for index, item in enumerate(argv_value)
            ),
            working_directory=_require_text(
                envelope_document["working_directory"], "runtime_envelope.working_directory"
            ),
            workspace_id=_require_text(
                envelope_document["workspace_id"], "runtime_envelope.workspace_id"
            ),
            os_boundary=_require_choice(
                envelope_document["os_boundary"], "runtime_envelope.os_boundary", _OS_BOUNDARIES
            ),
            provider=_require_text(envelope_document["provider"], "runtime_envelope.provider"),
            adapter=_require_text(envelope_document["adapter"], "runtime_envelope.adapter"),
            auth_route=_require_text(
                envelope_document["auth_route"], "runtime_envelope.auth_route"
            ),
            model=_optional_text(envelope_document.get("model"), "runtime_envelope.model"),
            model_digest=_optional_text(
                envelope_document.get("model_digest"), "runtime_envelope.model_digest"
            ),
            output_schema_hash=_parse_optional_hash(
                envelope_document.get("output_schema_hash"), "runtime_envelope.output_schema_hash"
            ),
            environment_allowlist_hash=_parse_optional_hash(
                envelope_document.get("environment_allowlist_hash"),
                "runtime_envelope.environment_allowlist_hash",
            ),
        ),
        invocation=InvocationDeclaration(
            invocation_mode=_require_text(
                invocation_document["invocation_mode"], "invocation.invocation_mode"
            ),
            provider=_require_text(invocation_document["provider"], "invocation.provider"),
            billing_mode=_require_text(
                invocation_document["billing_mode"], "invocation.billing_mode"
            ),
            expected_effect=_require_text(
                invocation_document["expected_effect"], "invocation.expected_effect"
            ),
            model=_optional_text(invocation_document.get("model"), "invocation.model"),
            provider_operation=_optional_text(
                invocation_document.get("provider_operation"), "invocation.provider_operation"
            ),
            output_schema_hash=_parse_optional_hash(
                invocation_document.get("output_schema_hash"), "invocation.output_schema_hash"
            ),
        ),
        token_profile=_parse_token_profile(document["token_profile"]),
        token_budget=_parse_token_budget(document["token_budget"]),
        actions=_parse_actions(document["actions"]),
    )
