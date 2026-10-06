"""MVP0-Aの決定的Context Token Budget Manager（spec §3.6、§1.11、§1.12）。

## 語彙の正本

本Moduleの型名は `design-source/registries/schemas.yaml` と
`schemas/core/<name>/1.0.0.schema.json` に登録された名前だけを使う。

    ContextFragment / ContextBundle / ContextSelectionReceipt
    TokenBudgetPolicy / TokenProfileSnapshot

「Context Snapshot」と呼ばれる概念の正本名は `ContextBundle` であり、
その再現性Hashが `bundle_hash` である。Registryに無い名前
（`Conversation`、`Message`、`ContextSnapshot`）の型を新設しない（不変条件#18）。

## 3つのHashを混同しない

`plan.py` と同じ分離を守る。

* `bundle_hash`
    Context Snapshotの意味的再現性。採用Fragment、Profile／Policy Hash、束縛Hash群。
* `decision_hash`
    選択判断の再現性。Candidate、Selected、Excluded＋理由、Algorithm Version。
    **`receipt_id`／`bundle_id` を含めない**（Owner Decision CP-4-A）。同じ判断は
    Receipt が違っても同じ値になる。個体の識別はRecordの`content_hash`が担う。
* Recordの`content_hash`
    保存Recordの監査Hash。`record_id`・`created_at`・`producer`を含むRecord射影全体。

Record用`content_hash`へ`bundle_hash`をそのまま入れない。両者は目的が違う。

## Error Codeの使い分け（TASK-DESIGN-B-02）

`CONTEXT_BUDGET_EXCEEDED` と `RUNTIME_SPEC_MISMATCH` を混ぜない。
どちらも Classification は `VALIDATION_ERROR` だが、Alertを受けたときに
原因を切り分けられるかが変わる。

`CONTEXT_BUDGET_EXCEEDED`（Token予算が足りない）

* 必須Fragmentが予算へ収まらない（§3.6 手順8）
* 固定Overheadが利用可能入力Tokenを食い潰した
* Policyの合計TokenがProvider Context Limitを超える
* 予約出力TokenがProvider Maximum Output Limitを超える

`RUNTIME_SPEC_MISMATCH`（宣言と実体が食い違う。予算の話ではない）

* `TokenProfileSnapshot` が `estimate_assurance=UNKNOWN`
* `TokenProfileSnapshot` のExpiry超過
* 計数器のTokenizer名／Version／Counting Adapter VersionがSnapshotと不一致
* Bundle不変条件の違反（Mandatory欠落、重複除去でMandatoryが落ちる）

いずれも自動再試行しない。§1.4.2 の写像により
`ACTION_FAILED` → `FAILED_PERMANENT` へ落ちる（`VALIDATION_ERROR` は
§1.7の条件付き再試行対象に含まれない）。

## 決定性（不変条件#4／#5／#6）

`bundle_hash` の入力にランダムID、採番ID、時刻、PID、列挙順を含めない。
`set`／`frozenset`を反復せず、整列はCode Point昇順を明示する。

## 本文を持たない

Fragmentは本文を保持せず `fragment_content_hash` だけを持つ
（`ContextFragment`制約「Content本文はArtifact参照」、不変条件#7）。
本文からFragmentを組み立てる境界はApplication層にある。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import HASH_PROFILE_VERSION, ContentHash, hash_canonical
from harness.domain.timestamps import canonical_timestamp, timestamp_seconds

__all__ = [
    "CONTEXT_SELECTION_ALGORITHM_VERSION",
    "FINAL_PAYLOAD_ARTIFACT_TYPE",
    "ContextAssembly",
    "ContextBundle",
    "ContextFragment",
    "ContextSelection",
    "ContextSelectionReceipt",
    "EstimateAssurance",
    "ExcludedFragment",
    "ExclusionReason",
    "MessageRole",
    "TokenBudgetPolicy",
    "TokenOverheads",
    "TokenProfileSnapshot",
    "build_context_bundle",
    "final_payload_hash",
    "final_payload_projection",
    "select_context",
]

# 選択アルゴリズムのVersion。spec §3.6の手順とTie-breakerを変えたら上げる。
# `ContextSelectionReceipt`制約「Algorithm VersionとTie-breakerを記録」。
CONTEXT_SELECTION_ALGORITHM_VERSION: Final[str] = "context-selection/2"

# 同順位のTie-breaker。Locale非依存であることを明示する（§3.7.1）。
_TIE_BREAKER: Final[str] = "mandatory-first,priority-desc,fragment_id:unicode-code-point-ascending"


class MessageRole(Enum):
    """`ContextFragment.message_role` の正本enum。

    値は `schemas/core/ContextFragment/1.0.0.schema.json` の列挙と一致する。
    Roleの序列はspecに定義が無いため、本enumを優先度として使わない。
    """

    SYSTEM_CONTROL = "SYSTEM_CONTROL"
    DEVELOPER_CONTROL = "DEVELOPER_CONTROL"
    USER_TASK = "USER_TASK"
    TOOL_DEFINITION = "TOOL_DEFINITION"
    VERIFIED_REFERENCE_DATA = "VERIFIED_REFERENCE_DATA"
    UNTRUSTED_ARTIFACT_DATA = "UNTRUSTED_ARTIFACT_DATA"
    UNTRUSTED_PROVIDER_DATA = "UNTRUSTED_PROVIDER_DATA"


class EstimateAssurance(Enum):
    """§1.12 Estimate Assurance。"""

    EXACT = "EXACT"
    CONSERVATIVE = "CONSERVATIVE"
    UNKNOWN = "UNKNOWN"


class ExclusionReason(Enum):
    """`ContextSelectionReceipt`制約「各Excluded FragmentにReason Code必須」。

    規範Fixture断片は `{"fragment_id":"f2","reason":"BUDGET"}`。
    MVP0-Aが生成する理由はBudgetとContent Hash完全一致の2つだけである
    （§3.6 手順5「MVP0-Aでは類似度判定を実装しない」）。
    """

    BUDGET = "BUDGET"
    DUPLICATE = "DUPLICATE"


# §3.6 手順2「Untrusted入力のControl Role昇格を拒否する」の対象Role。
_UNTRUSTED_ROLES: Final[frozenset[MessageRole]] = frozenset(
    {MessageRole.UNTRUSTED_ARTIFACT_DATA, MessageRole.UNTRUSTED_PROVIDER_DATA}
)

# `ContextFragment`制約「`SYSTEM_CONTROL`／`DEVELOPER_CONTROL`は
# 検証済み`control_authority`必須」。
_CONTROL_ROLES: Final[frozenset[MessageRole]] = frozenset(
    {MessageRole.SYSTEM_CONTROL, MessageRole.DEVELOPER_CONTROL}
)


@dataclass(frozen=True, slots=True)
class TokenOverheads:
    """§1.12がSnapshotへ要求するOverhead群。

    §1.12は「Overhead算定不能の場合は実行停止する」と定める。既定値を持たせると
    「算定していない」と「算定した結果0」が同じ見た目になるため、**全項目を必須**とする。
    Mock Providerのように実測0が正しい場合も、0を明示的に宣言させる。
    """

    system_message_overhead: int
    developer_message_overhead: int
    tool_definition_overhead: int
    per_message_overhead: int
    structured_output_overhead: int
    streaming_frame_overhead: int
    retry_fallback_reservation: int

    def __post_init__(self) -> None:
        for name in (
            "system_message_overhead",
            "developer_message_overhead",
            "tool_definition_overhead",
            "per_message_overhead",
            "structured_output_overhead",
            "streaming_frame_overhead",
            "retry_fallback_reservation",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")

    @property
    def fixed_overhead_tokens(self) -> int:
        """Fragment数に依存しない固定消費。予算から先に差し引く。"""
        return (
            self.system_message_overhead
            + self.developer_message_overhead
            + self.tool_definition_overhead
            + self.structured_output_overhead
            + self.streaming_frame_overhead
            + self.retry_fallback_reservation
        )

    def projection(self) -> dict[str, Any]:
        return {
            "system_message_overhead": self.system_message_overhead,
            "developer_message_overhead": self.developer_message_overhead,
            "tool_definition_overhead": self.tool_definition_overhead,
            "per_message_overhead": self.per_message_overhead,
            "structured_output_overhead": self.structured_output_overhead,
            "streaming_frame_overhead": self.streaming_frame_overhead,
            "retry_fallback_reservation": self.retry_fallback_reservation,
        }


@dataclass(frozen=True, slots=True)
class TokenProfileSnapshot:
    """§1.12 Token Profile Snapshot。

    Provider／Model／Tokenizer／計数Adapterの同一性と、Overhead算定結果を
    1つのHashへ畳み込む。`snapshot_hash`は`bundle_hash`の入力であるため、
    どれが変わってもContext Snapshotは別物になる。
    """

    snapshot_id: str
    provider: str
    model: str
    tokenizer_name: str
    tokenizer_version: str
    counting_adapter_version: str
    context_limit: int
    maximum_output_limit: int
    estimate_assurance: EstimateAssurance
    overheads: TokenOverheads
    retrieved_at: str
    expires_at: str
    vocabulary_hash: ContentHash | None = None

    def __post_init__(self) -> None:
        for name in (
            "snapshot_id",
            "provider",
            "model",
            "tokenizer_name",
            "tokenizer_version",
            "counting_adapter_version",
        ):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if self.context_limit < 1 or self.maximum_output_limit < 1:
            raise ValueError("context_limit and maximum_output_limit must be >= 1")
        canonical_timestamp(self.retrieved_at)
        canonical_timestamp(self.expires_at)
        if timestamp_seconds(self.expires_at) <= timestamp_seconds(self.retrieved_at):
            raise ValueError("expires_at must be later than retrieved_at")

    @property
    def snapshot_hash(self) -> ContentHash:
        return hash_canonical(
            self.projection(), artifact_type="token-profile-snapshot", schema_major=1
        )

    def projection(self) -> dict[str, Any]:
        """Hash入力。§1.11「意味のあるSnapshot時刻はContentへ含める」に従う。"""
        return {
            "snapshot_id": self.snapshot_id,
            "provider": self.provider,
            "model": self.model,
            "tokenizer_name": self.tokenizer_name,
            "tokenizer_version": self.tokenizer_version,
            "counting_adapter_version": self.counting_adapter_version,
            "vocabulary_hash": None if self.vocabulary_hash is None else str(self.vocabulary_hash),
            "context_limit": self.context_limit,
            "maximum_output_limit": self.maximum_output_limit,
            "estimate_assurance": self.estimate_assurance.value,
            "overheads": self.overheads.projection(),
            "retrieved_at": self.retrieved_at,
            "expires_at": self.expires_at,
            "hash_profile_version": HASH_PROFILE_VERSION,
        }

    def require_usable(self, *, now: str) -> None:
        """§1.12「`UNKNOWN`、Version不明、Overhead算定不能の場合は実行停止する」。

        併せて「Expiry超過時はFail-Closedとする」を適用する。`now`はClock Portが
        解決した値を受け取り、Domainが時刻を直接読まない（CLAUDE.md §2）。
        """
        if self.estimate_assurance is EstimateAssurance.UNKNOWN:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "token profile snapshot with estimate_assurance=UNKNOWN cannot be used",
            )
        if timestamp_seconds(canonical_timestamp(now)) >= timestamp_seconds(self.expires_at):
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "token profile snapshot has expired; re-acquire before planning",
            )

    def require_counter_identity(
        self, *, tokenizer_name: str, tokenizer_version: str, counting_adapter_version: str
    ) -> None:
        """§1.12「ModelとTokenizer不一致の場合は実行停止する」。

        計数器が名乗るTokenizerと計数Adapter Versionが、Snapshotの宣言と
        一致しなければ、Snapshotへ束縛したToken数の意味が失われる。推測せず停止する。
        """
        actual = (tokenizer_name, tokenizer_version, counting_adapter_version)
        expected = (self.tokenizer_name, self.tokenizer_version, self.counting_adapter_version)
        if actual != expected:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "token counter identity does not match the token profile snapshot",
            )


@dataclass(frozen=True, slots=True)
class TokenBudgetPolicy:
    """`TokenBudgetPolicy` Schema。制約「各値は0以上の整数」「合計がContext Limit以下」。

    入力上限・予約出力・予約Tool・安全余裕・残量を別々の値として保持する。
    `available_input_tokens`はPolicyだけで決まる残量であり、Provider固有の
    固定Overheadは`TokenProfileSnapshot`側にある。両者を混ぜない。
    """

    total_tokens: int
    reserved_output_tokens: int
    reserved_tool_tokens: int
    safety_margin_tokens: int = 0
    compression_max_depth: int = 1
    # `TokenBudgetPolicy`制約「`overflow_policy=FAIL_CLOSED`を初期値」。
    # MVP0-Aで定義された値はこれだけであり、他の値を推測で受け付けない。
    overflow_policy: str = "FAIL_CLOSED"
    policy_id: str = "default"

    def __post_init__(self) -> None:
        values = (
            self.total_tokens,
            self.reserved_output_tokens,
            self.reserved_tool_tokens,
            self.safety_margin_tokens,
        )
        if min(values) < 0:
            raise ValueError("token budget values must not be negative")
        if not 0 <= self.compression_max_depth <= 2:
            # §3.6 圧縮規則「圧縮深度は初期値1、最大2」。Schemaのminimum/maximumと一致。
            raise ValueError("compression_max_depth must be between 0 and 2")
        if self.overflow_policy != "FAIL_CLOSED":
            raise ValueError("overflow_policy must be FAIL_CLOSED in MVP0-A")
        if not self.policy_id:
            raise ValueError("policy_id must not be empty")
        if self.available_input_tokens < 0:
            raise ValueError("reserved tokens exceed total token budget")

    @property
    def available_input_tokens(self) -> int:
        return (
            self.total_tokens
            - self.reserved_output_tokens
            - self.reserved_tool_tokens
            - self.safety_margin_tokens
        )

    @property
    def policy_hash(self) -> ContentHash:
        return hash_canonical(
            self.projection(), artifact_type="token-budget-policy", schema_major=1
        )

    def projection(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "total_tokens": self.total_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "reserved_tool_tokens": self.reserved_tool_tokens,
            "safety_margin_tokens": self.safety_margin_tokens,
            "compression_max_depth": self.compression_max_depth,
            "overflow_policy": self.overflow_policy,
            "hash_profile_version": HASH_PROFILE_VERSION,
        }

    def require_within(self, profile: TokenProfileSnapshot) -> None:
        """`TokenBudgetPolicy`制約「合計がProvider Context Limit以下」。

        いずれもToken予算がProviderの許容量を超えている状態であり、
        `CONTEXT_BUDGET_EXCEEDED`（§1.7.1、Classification `VALIDATION_ERROR`）で停止する。
        Runtime SpecやTokenizerの不一致ではないため`RUNTIME_SPEC_MISMATCH`を使わない。
        """
        if self.total_tokens > profile.context_limit:
            raise HarnessError(
                ErrorCode.CONTEXT_BUDGET_EXCEEDED,
                "token budget total exceeds the provider context limit",
            )
        if self.reserved_output_tokens > profile.maximum_output_limit:
            raise HarnessError(
                ErrorCode.CONTEXT_BUDGET_EXCEEDED,
                "reserved output tokens exceed the provider maximum output limit",
            )
        if self.available_input_tokens < profile.overheads.fixed_overhead_tokens:
            raise HarnessError(
                ErrorCode.CONTEXT_BUDGET_EXCEEDED,
                "fixed provider overhead exceeds the available input token budget",
            )


@dataclass(frozen=True, slots=True)
class ContextFragment:
    """`ContextFragment` Schema。本文は持たず`fragment_content_hash`だけを持つ。

    `priority`は呼出側が与える明示値である。spec §3.6の9段階優先順位と
    `message_role`の7値enumとの写像はRegistryにもspecにも定義が無いため、
    Domainで推測して写像を作らない。

    `input_read_capability_id`と`classification_scan_evidence_hash`は
    §3.6 手順1の検査証跡である。Domainは値の有無だけを保持し、
    「証跡が必要か」の判定はApplication層が行う（Trust Zoneを知るのは向こう）。
    """

    fragment_id: str
    fragment_content_hash: ContentHash
    token_count: int
    message_role: MessageRole
    control_authority: bool
    instruction_eligible: bool
    mandatory: bool = False
    priority: int = 0
    source_artifact_id: str = ""
    deduplication_key: ContentHash | None = None
    classification_scan_evidence_hash: ContentHash | None = None
    input_read_capability_id: str | None = None

    def __post_init__(self) -> None:
        if not self.fragment_id:
            raise ValueError("fragment_id must not be empty")
        if self.token_count < 0:
            raise ValueError("token_count must not be negative")

    @property
    def deduplication_hash(self) -> ContentHash:
        """§3.6 手順4「Content Hashで完全重複を除去する」の比較キー。"""
        return self.deduplication_key or self.fragment_content_hash

    def role_projection(self) -> dict[str, Any]:
        """Message Role Manifestの1件分。"""
        return {
            "fragment_id": self.fragment_id,
            "message_role": self.message_role.value,
            "control_authority": self.control_authority,
            "instruction_eligible": self.instruction_eligible,
        }

    def bundle_projection(self) -> dict[str, Any]:
        """`bundle_hash`の入力となる1件分の射影。

        Read証跡のHashも含める。同じ本文でも、別のCapabilityで読まれたものは
        別のContextである（§3.6 手順1、§1.16）。
        """
        return {
            "fragment_id": self.fragment_id,
            "fragment_content_hash": str(self.fragment_content_hash),
            "message_role": self.message_role.value,
            "token_count": self.token_count,
            "mandatory": self.mandatory,
            "source_artifact_id": self.source_artifact_id,
            "input_read_capability_id": self.input_read_capability_id,
            "classification_scan_evidence_hash": (
                None
                if self.classification_scan_evidence_hash is None
                else str(self.classification_scan_evidence_hash)
            ),
        }


@dataclass(frozen=True, slots=True)
class ExcludedFragment:
    """除外1件。理由Codeを必須とする（Reason無し除外は拒否例）。"""

    fragment_id: str
    reason: ExclusionReason

    def projection(self) -> dict[str, Any]:
        return {"fragment_id": self.fragment_id, "reason": self.reason.value}


@dataclass(frozen=True, slots=True)
class ContextSelection:
    """選択結果。CandidateはSelectedとExcludedで完全に説明される。"""

    available_input_tokens: int
    selected_tokens: int
    selected_fragments: tuple[ContextFragment, ...]
    excluded_fragments: tuple[ExcludedFragment, ...]
    candidate_fragment_ids: tuple[str, ...]
    mandatory_fragment_ids: tuple[str, ...]

    @property
    def selected_fragment_ids(self) -> tuple[str, ...]:
        return tuple(fragment.fragment_id for fragment in self.selected_fragments)

    @property
    def excluded_fragment_ids(self) -> tuple[str, ...]:
        return tuple(item.fragment_id for item in self.excluded_fragments)

    @property
    def deduplicated_fragment_ids(self) -> tuple[str, ...]:
        return tuple(
            item.fragment_id
            for item in self.excluded_fragments
            if item.reason is ExclusionReason.DUPLICATE
        )


@dataclass(frozen=True, slots=True)
class ContextBundle:
    """`ContextBundle` Schema。`bundle_hash`がContext Snapshotの再現性Hashである。

    制約「Ordered IDsはSelected IDsと同集合」「Mandatory Fragmentを全て含む」を
    構築時に検査する。生成後は`frozen=True`で変更できない。
    """

    bundle_id: str
    selection_receipt_id: str
    ordered_fragment_ids: tuple[str, ...]
    total_token_count: int
    message_role_manifest_hash: ContentHash
    token_profile_snapshot_hash: ContentHash
    token_budget_policy_hash: ContentHash
    input_read_capability_set_hash: ContentHash
    input_read_evidence_hash: ContentHash
    compression_artifact_ids: tuple[str, ...]
    algorithm_version: str
    bundle_hash: ContentHash
    content_projection: tuple[tuple[str, Any], ...]

    def recompute_bundle_hash(self) -> ContentHash:
        """保存済みBundleからHashを再計算する。改ざん・取り違えの検出に使う。"""
        return hash_canonical(
            dict(self.content_projection), artifact_type="context-bundle", schema_major=1
        )

    def assert_integrity(self) -> None:
        if self.recompute_bundle_hash() != self.bundle_hash:
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT,
                "stored context bundle does not reproduce its bundle_hash",
            )

    def canonical_content(self) -> dict[str, Any]:
        """CASへ保存するCanonical JSONの元となる辞書。"""
        return dict(self.content_projection)

    def to_record(self, *, record_id: str, created_at: str, producer: str) -> dict[str, Any]:
        """`ContextBundle/1.0.0.schema.json` へ適合する保存Recordを返す。

        `content_hash`はRecordの監査Hashであり、`bundle_hash`とは別に計算する
        （`plan.py`の`to_record`と同じ分離）。同じBundleでも別Recordとして
        保存されたものは別の`content_hash`を持つ。

        なおJSON Schemaは`additionalProperties:false`であり、
        `input_read_capability_set_hash`／`input_read_evidence_hash`／
        `selection_receipt_id`／`compression_artifact_ids`のFieldを持たない。
        これらはSchema本文（`spec/20-schemas/ContextBundle.md`）が必須と定めるが
        機械可読Schemaに無い。Recordへ出さず、`bundle_hash`へ束縛して保持する。
        Schema側の是正は別Taskとする。
        """
        record = {
            "schema_name": "ContextBundle",
            "schema_version": "1.0.0",
            "record_id": record_id,
            "created_at": canonical_timestamp(created_at),
            "producer": producer,
            "bundle_id": self.bundle_id,
            "bundle_hash": str(self.bundle_hash),
            "fragment_ids": list(self.ordered_fragment_ids),
            "total_token_count": self.total_token_count,
            "message_role_manifest_hash": str(self.message_role_manifest_hash),
        }
        record["content_hash"] = str(
            hash_canonical(record, artifact_type="context-bundle-record", schema_major=1)
        )
        return record


@dataclass(frozen=True, slots=True)
class ContextSelectionReceipt:
    """`ContextSelectionReceipt` Schema。選択・除外・拒否の根拠を保持する。

    `rejected_input_resources`は§1.16.2のRead拒否記録であり、Bytes本文を保存しない。
    """

    receipt_id: str
    bundle_id: str
    candidate_fragment_ids: tuple[str, ...]
    selected_fragment_ids: tuple[str, ...]
    excluded_fragments: tuple[ExcludedFragment, ...]
    rejected_input_resources: tuple[tuple[tuple[str, Any], ...], ...]
    estimated_token_total: int
    token_profile_snapshot_hash: ContentHash
    budget_policy_hash: ContentHash
    input_read_capability_set_hash: ContentHash
    input_read_evidence_hash: ContentHash
    compression_artifact_ids: tuple[str, ...]
    algorithm_version: str
    tie_breaker: str
    decision_hash: ContentHash

    @property
    def excluded_fragment_ids(self) -> tuple[str, ...]:
        return tuple(item.fragment_id for item in self.excluded_fragments)

    @property
    def deduplication_result(self) -> tuple[str, ...]:
        return tuple(
            item.fragment_id
            for item in self.excluded_fragments
            if item.reason is ExclusionReason.DUPLICATE
        )

    def to_record(self, *, record_id: str, created_at: str, producer: str) -> dict[str, Any]:
        """`ContextSelectionReceipt/1.0.0.schema.json` へ適合する保存Recordを返す。

        `content_hash`はRecordの監査Hashであり、`decision_hash`とは別に計算する。
        """
        record: dict[str, Any] = {
            "schema_name": "ContextSelectionReceipt",
            "schema_version": "1.0.0",
            "record_id": record_id,
            "created_at": canonical_timestamp(created_at),
            "producer": producer,
            "receipt_id": self.receipt_id,
            "bundle_id": self.bundle_id,
            "selected_fragment_ids": list(self.selected_fragment_ids),
            "excluded_fragment_ids": list(self.excluded_fragment_ids),
            "rejected_input_resources": [dict(item) for item in self.rejected_input_resources],
            "compressed_fragment_ids": list(self.compression_artifact_ids),
        }
        record["content_hash"] = str(
            hash_canonical(record, artifact_type="context-selection-receipt-record", schema_major=1)
        )
        return record


@dataclass(frozen=True, slots=True)
class ContextAssembly:
    """Bundleと、その選択根拠Receiptの組。"""

    bundle: ContextBundle
    receipt: ContextSelectionReceipt
    selection: ContextSelection


def select_context(
    fragments: Sequence[ContextFragment],
    policy: TokenBudgetPolicy,
    profile: TokenProfileSnapshot | None = None,
) -> ContextSelection:
    """spec §3.6に従い、外部状態なしで決定的にContextを選択する。

    手順は§3.6「選択アルゴリズム」に対応する。

    2. Control／Data Role検証（Untrusted入力のControl Role昇格を拒否）
    4. Content Hash完全一致の重複除去（Mandatoryを必ず優先する）
    6. 必須Fragmentを先に確保
    7. 残BudgetへPriority順に追加
    8. 収まらない必須Fragmentがあれば停止

    `profile`を渡した場合、固定Overheadを予算から差し引き、
    Per-message OverheadをFragmentごとの実効コストへ加算する（§1.12）。
    """
    _require_no_role_escalation(fragments)
    _require_unique_fragment_ids(fragments)

    fixed_overhead = 0 if profile is None else profile.overheads.fixed_overhead_tokens
    per_message = 0 if profile is None else profile.overheads.per_message_overhead
    available = policy.available_input_tokens - fixed_overhead
    if available < 0:
        raise HarnessError(
            ErrorCode.CONTEXT_BUDGET_EXCEEDED,
            "fixed provider overhead exceeds the available input token budget",
        )

    retained, duplicates = _deduplicate(fragments)
    mandatory = tuple(
        sorted((item for item in retained if item.mandatory), key=lambda item: item.fragment_id)
    )
    optional = tuple(
        sorted(
            (item for item in retained if not item.mandatory),
            key=lambda item: (-item.priority, item.fragment_id),
        )
    )

    selected: list[ContextFragment] = []
    selected_tokens = 0
    for fragment in mandatory:
        selected_tokens += fragment.token_count + per_message
        if selected_tokens > available:
            # §3.6 手順8「収まらない必須Fragmentがある場合は
            # `CONTEXT_BUDGET_EXCEEDED`で停止する」。
            #
            # Step 3-b で当該Codeが errors.yaml と §1.7.1 表へ登録されたため、
            # 代替として使っていた `RUNTIME_SPEC_MISMATCH` から差し替えた
            # （Owner Decision B-1b / TASK-DESIGN-B-02）。
            raise HarnessError(
                ErrorCode.CONTEXT_BUDGET_EXCEEDED, "mandatory context exceeds the token budget"
            )
        selected.append(fragment)

    excluded: list[ExcludedFragment] = list(duplicates)
    for fragment in optional:
        if selected_tokens + fragment.token_count + per_message <= available:
            selected.append(fragment)
            selected_tokens += fragment.token_count + per_message
        else:
            excluded.append(ExcludedFragment(fragment.fragment_id, ExclusionReason.BUDGET))

    return ContextSelection(
        available_input_tokens=available,
        selected_tokens=selected_tokens,
        selected_fragments=tuple(selected),
        excluded_fragments=tuple(excluded),
        candidate_fragment_ids=tuple(sorted(item.fragment_id for item in fragments)),
        mandatory_fragment_ids=tuple(
            sorted(item.fragment_id for item in fragments if item.mandatory)
        ),
    )


def build_context_bundle(
    fragments: Sequence[ContextFragment],
    policy: TokenBudgetPolicy,
    profile: TokenProfileSnapshot,
    *,
    bundle_id: str,
    receipt_id: str,
    input_read_capability_set_hash: ContentHash,
    input_read_evidence_hash: ContentHash,
    rejected_input_resources: Sequence[dict[str, Any]] = (),
    algorithm_version: str = CONTEXT_SELECTION_ALGORITHM_VERSION,
    compression_artifact_ids: tuple[str, ...] = (),
) -> ContextAssembly:
    """選択結果からContext Snapshot（`ContextBundle`）とReceiptを構築する。

    §1.11「Planは1回凍結した入力から2回Buildし、Hash一致を検証する」と同じ扱いで、
    同一入力から2回射影を作りHashが一致することを確認する。不一致は停止する。
    別`PYTHONHASHSEED`での三重Buildは試験側が担う（不変条件#5／#6）。
    """
    if not bundle_id or not receipt_id:
        raise ValueError("bundle_id and receipt_id must not be empty")
    if not algorithm_version:
        raise ValueError("algorithm_version must not be empty")

    policy.require_within(profile)
    selection = select_context(fragments, policy, profile)

    first = _bundle_projection(
        selection,
        policy,
        profile,
        algorithm_version=algorithm_version,
        input_read_capability_set_hash=input_read_capability_set_hash,
        input_read_evidence_hash=input_read_evidence_hash,
        compression_artifact_ids=compression_artifact_ids,
    )
    second = _bundle_projection(
        selection,
        policy,
        profile,
        algorithm_version=algorithm_version,
        input_read_capability_set_hash=input_read_capability_set_hash,
        input_read_evidence_hash=input_read_evidence_hash,
        compression_artifact_ids=compression_artifact_ids,
    )
    first_hash = hash_canonical(first, artifact_type="context-bundle", schema_major=1)
    second_hash = hash_canonical(second, artifact_type="context-bundle", schema_major=1)
    if first != second or first_hash != second_hash:
        raise HarnessError(
            ErrorCode.PLAN_NONDETERMINISTIC,
            "identical context selection produced different bundle content",
        )

    ordered_ids = selection.selected_fragment_ids
    _require_bundle_constraints(selection, ordered_ids)

    bundle = ContextBundle(
        bundle_id=bundle_id,
        selection_receipt_id=receipt_id,
        ordered_fragment_ids=ordered_ids,
        total_token_count=selection.selected_tokens,
        message_role_manifest_hash=_message_role_manifest_hash(selection.selected_fragments),
        token_profile_snapshot_hash=profile.snapshot_hash,
        token_budget_policy_hash=policy.policy_hash,
        input_read_capability_set_hash=input_read_capability_set_hash,
        input_read_evidence_hash=input_read_evidence_hash,
        compression_artifact_ids=compression_artifact_ids,
        algorithm_version=algorithm_version,
        bundle_hash=first_hash,
        content_projection=tuple(sorted(first.items())),
    )

    rejected = tuple(tuple(sorted(item.items())) for item in rejected_input_resources)
    # `decision_hash` は **選択判断の再現性** を表す（Owner Decision CP-4-A）。
    # `receipt_id`／`bundle_id` を入力に取らない。取ると同じ判断でも Receipt ごとに
    # 値が変わり、「同じ入力から同じ判断が出たか」を Hash で照合できなくなる。
    # Receipt 個体の識別は Record の `content_hash` が担う。
    receipt_projection = {
        "bundle_hash": str(bundle.bundle_hash),
        "candidate_fragment_ids": list(selection.candidate_fragment_ids),
        "selected_fragment_ids": list(selection.selected_fragment_ids),
        "excluded_fragments": [item.projection() for item in selection.excluded_fragments],
        "rejected_input_resources": [dict(item) for item in rejected],
        "estimated_token_total": selection.selected_tokens,
        "token_profile_snapshot_hash": str(profile.snapshot_hash),
        "budget_policy_hash": str(policy.policy_hash),
        "input_read_capability_set_hash": str(input_read_capability_set_hash),
        "input_read_evidence_hash": str(input_read_evidence_hash),
        "compression_artifact_ids": list(compression_artifact_ids),
        "algorithm_version": algorithm_version,
        "tie_breaker": _TIE_BREAKER,
        "hash_profile_version": HASH_PROFILE_VERSION,
    }
    receipt = ContextSelectionReceipt(
        receipt_id=receipt_id,
        bundle_id=bundle_id,
        candidate_fragment_ids=selection.candidate_fragment_ids,
        selected_fragment_ids=selection.selected_fragment_ids,
        excluded_fragments=selection.excluded_fragments,
        rejected_input_resources=rejected,
        estimated_token_total=selection.selected_tokens,
        token_profile_snapshot_hash=profile.snapshot_hash,
        budget_policy_hash=policy.policy_hash,
        input_read_capability_set_hash=input_read_capability_set_hash,
        input_read_evidence_hash=input_read_evidence_hash,
        compression_artifact_ids=compression_artifact_ids,
        algorithm_version=algorithm_version,
        tie_breaker=_TIE_BREAKER,
        decision_hash=hash_canonical(
            receipt_projection, artifact_type="context-selection-receipt", schema_major=1
        ),
    )
    return ContextAssembly(bundle=bundle, receipt=receipt, selection=selection)


def _bundle_projection(
    selection: ContextSelection,
    policy: TokenBudgetPolicy,
    profile: TokenProfileSnapshot,
    *,
    algorithm_version: str,
    input_read_capability_set_hash: ContentHash,
    input_read_evidence_hash: ContentHash,
    compression_artifact_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """`bundle_hash`の入力。ランダムID・時刻・PID・列挙順を含めない（不変条件#4）。"""
    return {
        "ordered_fragment_ids": list(selection.selected_fragment_ids),
        "fragments": [fragment.bundle_projection() for fragment in selection.selected_fragments],
        "total_token_count": selection.selected_tokens,
        "message_role_manifest_hash": str(
            _message_role_manifest_hash(selection.selected_fragments)
        ),
        "token_profile_snapshot_hash": str(profile.snapshot_hash),
        "token_budget_policy_hash": str(policy.policy_hash),
        "input_read_capability_set_hash": str(input_read_capability_set_hash),
        "input_read_evidence_hash": str(input_read_evidence_hash),
        "compression_artifact_ids": list(compression_artifact_ids),
        "algorithm_version": algorithm_version,
        "tie_breaker": _TIE_BREAKER,
        "hash_profile_version": HASH_PROFILE_VERSION,
    }


#: 最終送信 payload の Hash が名乗る Domain Separator。
#:
#: `hash_canonical` は種別ごとに分離子を要る。**正規化の仕方は変えない。**
#: 何を入力に取るかだけが、この Hash 固有である。
FINAL_PAYLOAD_ARTIFACT_TYPE: Final[str] = "final-send-payload"


def final_payload_projection(fragments: Sequence[ContextFragment]) -> dict[str, Any]:
    """Provider へ実際に渡すものだけを射影する。

    **渡さないものを入れない。** `fragment_id`・`source_artifact_id`・
    `input_read_capability_id`・`classification_scan_evidence_hash`・
    `token_count`・`mandatory` は素性と会計であって payload ではない。
    Snapshot ID も Policy ID も Bundle ID も、時刻も乱数も PID も入れない
    （不変条件#4）。

    引数は **送信順**である。順序そのものが payload の一部なので、ここでは
    並べ替えない。呼出側が `send_order` で渡す。
    """
    return {
        "messages": [
            {
                "message_role": fragment.message_role.value,
                "content_hash": str(fragment.fragment_content_hash),
            }
            for fragment in fragments
        ],
        "hash_profile_version": HASH_PROFILE_VERSION,
    }


def final_payload_hash(fragments: Sequence[ContextFragment]) -> ContentHash:
    """最終送信 payload の Hash。**送信順の Fragment を受け取る。**

    同じ本文・同じ role・同じ順序なら、Snapshot ID や Policy ID が違っても
    同じ値になる。本文・role・順序のどれかが変われば値が変わる。
    """
    return hash_canonical(
        final_payload_projection(fragments),
        artifact_type=FINAL_PAYLOAD_ARTIFACT_TYPE,
        schema_major=1,
    )


def _message_role_manifest_hash(fragments: Sequence[ContextFragment]) -> ContentHash:
    """採用Fragmentの Control／Data Role Manifest Hash。

    順序を含める。同じFragment集合でも順序が変われば別のManifestである。
    """
    manifest = [fragment.role_projection() for fragment in fragments]
    return hash_canonical(manifest, artifact_type="message-role-manifest", schema_major=1)


def _require_no_role_escalation(fragments: Sequence[ContextFragment]) -> None:
    """§3.6 手順2。Untrusted入力のControl Role昇格を拒否する。

    `AT-CONTROL-DATA-001/ARTIFACT_INSTRUCTION`（`untrusted_artifact_attempts_control_role`）
    が要求する`CONTROL_DATA_ROLE_ESCALATION`はここで発生する。
    """
    for fragment in fragments:
        if fragment.message_role in _UNTRUSTED_ROLES and (
            fragment.control_authority or fragment.instruction_eligible
        ):
            raise HarnessError(
                ErrorCode.CONTROL_DATA_ROLE_ESCALATION,
                "untrusted content cannot be promoted to a control or instruction role",
            )
        if fragment.message_role in _CONTROL_ROLES and not fragment.control_authority:
            raise HarnessError(
                ErrorCode.CONTROL_DATA_ROLE_ESCALATION,
                "control role requires a verified control_authority",
            )


def _require_unique_fragment_ids(fragments: Sequence[ContextFragment]) -> None:
    """同一Fragment IDの二重投入を拒否する。

    IDが重複すると、採用ID列とCandidate列の対応が一意でなくなり、
    Receiptの「CandidateはSelected＋Excludedで完全に説明」が成立しない。
    """
    seen: dict[str, int] = {}
    for fragment in fragments:
        seen[fragment.fragment_id] = seen.get(fragment.fragment_id, 0) + 1
    duplicated = sorted(key for key, count in seen.items() if count > 1)
    if duplicated:
        raise HarnessError(
            ErrorCode.PLAN_NONDETERMINISTIC,
            f"duplicate fragment_id in candidates: {', '.join(duplicated)}",
        )


def _require_bundle_constraints(selection: ContextSelection, ordered_ids: tuple[str, ...]) -> None:
    """`ContextBundle`制約と`ContextSelectionReceipt`制約を構築時に検査する。"""
    if len(set(ordered_ids)) != len(ordered_ids):
        raise HarnessError(
            ErrorCode.PLAN_NONDETERMINISTIC, "ordered_fragment_ids contains a duplicate"
        )
    # `ContextFragment`制約「Mandatory FragmentはSelectionから除外不可」。
    # Candidate側のMandatory全件を見る。採用済みだけを見ると常に真になり検査にならない。
    present = set(ordered_ids)
    missing = [item for item in selection.mandatory_fragment_ids if item not in present]
    if missing:
        raise HarnessError(
            ErrorCode.RUNTIME_SPEC_MISMATCH,
            f"mandatory fragment excluded from the context bundle: {', '.join(missing)}",
        )
    accounted = sorted([*selection.selected_fragment_ids, *selection.excluded_fragment_ids])
    if accounted != sorted(selection.candidate_fragment_ids):
        raise HarnessError(
            ErrorCode.PLAN_NONDETERMINISTIC,
            "candidates are not fully explained by selected and excluded fragments",
        )


def _deduplicate(
    fragments: Sequence[ContextFragment],
) -> tuple[tuple[ContextFragment, ...], tuple[ExcludedFragment, ...]]:
    """§3.6 手順4／5。Content Hash完全一致だけを重複として除去する。

    **Mandatoryを必ず優先する。** 整列キーの先頭に`not mandatory`を置くことで、
    同一Content Hashを持つMandatoryとOptionalが並んだ場合にMandatoryが残る。
    以前はPriorityだけで順位を決めていたため、高Priorityの任意Fragmentが
    Mandatoryを押しのけて残り、Mandatoryが`DUPLICATE`として消えていた。

    順位付けの後段にも、Mandatoryを落とす結果になっていないかのFail-Closed検査を置く。
    整列キーだけが正しさの根拠だと、キーを触った次の変更で同じ事故が再発する。

    `set`を反復せず、比較順を明示する（不変条件#6）。
    """
    seen: dict[str, ContextFragment] = {}
    duplicates: list[ExcludedFragment] = []
    ranked = sorted(
        fragments, key=lambda item: (not item.mandatory, -item.priority, item.fragment_id)
    )
    for fragment in ranked:
        key = str(fragment.deduplication_hash)
        kept = seen.get(key)
        if kept is not None:
            if fragment.mandatory and not kept.mandatory:
                raise HarnessError(
                    ErrorCode.RUNTIME_SPEC_MISMATCH,
                    f"mandatory fragment {fragment.fragment_id} would be dropped in favour of "
                    f"optional duplicate {kept.fragment_id}",
                )
            duplicates.append(ExcludedFragment(fragment.fragment_id, ExclusionReason.DUPLICATE))
            continue
        seen[key] = fragment
    retained = tuple(sorted(seen.values(), key=lambda item: item.fragment_id))
    return retained, tuple(sorted(duplicates, key=lambda item: item.fragment_id))
