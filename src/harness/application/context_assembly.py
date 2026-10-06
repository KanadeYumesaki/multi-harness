"""Context Assembly Service（spec §3.6、§1.12、§1.2 Trust Zone）。

本文（`str`）を扱う境界はここだけである。Domain層のFragmentは
`fragment_content_hash`しか持たず、`ContextBundle`と`ContextSelectionReceipt`にも
本文は入らない（不変条件#7、`ContextFragment`制約「Content本文はArtifact参照」）。

## §3.6 手順1を型で強制する

    「InputReadCapability、File Identity、Classification／Secret Scan Evidenceを
      検証し、合格した入力だけをFragment候補にする」

初版はこれらを任意Fieldにしていたため、Scan未実施・Capability無しの本文をそのまま
Context候補にできた。ここを塞ぐため、候補は必ず出自（:class:`FragmentOrigin`）を
宣言する。

* `CONTROL_PLANE`（Trust Zone Z0）
    Harness自身が生成した本文。Read証跡は不要。
    許可Roleは`SYSTEM_CONTROL`／`DEVELOPER_CONTROL`／`TOOL_DEFINITION`だけ。
* `VERIFIED_INPUT`（Z5を検証済み）
    Capability ID＋Scan Evidence Hash＋Artifact IDを全て要求する。
    許可Roleは上記以外の4 Role。

Control PlaneのRoleをVerified Inputが名乗ること、およびその逆を拒否する。
証跡欠落は`PATH_OUTSIDE_CAPABILITY`（Classification `POLICY_DENIED`）で停止する。

CLAUDE.md §2に従い、`ports/`と`domain/`だけへ依存する。`sqlite3`、`os`、
Provider SDKをimportしない。時刻はClock Portが解決した値を引数で受け取る。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Final

from harness.domain.artifact import ArtifactMetadata
from harness.domain.canonical import canonicalize
from harness.domain.context_budget import (
    CONTEXT_SELECTION_ALGORITHM_VERSION,
    ContextAssembly,
    ContextBundle,
    ContextFragment,
    EstimateAssurance,
    ExclusionReason,
    MessageRole,
    TokenBudgetPolicy,
    TokenProfileSnapshot,
    build_context_bundle,
    select_context,
)
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash, hash_bytes, hash_canonical
from harness.ports.artifact_store import ArtifactManifestRecord, ArtifactStorePort
from harness.ports.safe_input_reader import ReadEvidence
from harness.ports.task_intake import VerifiedContextInput
from harness.ports.token_counter import TokenCounterPort
from harness.ports.unit_of_work import UnitOfWorkPort

__all__ = [
    "ContextAssemblyService",
    "FragmentOrigin",
    "FragmentSource",
    "control_data_policy_hash",
]

_BUNDLE_MEDIA_TYPE: Final[str] = "application/json"


class FragmentOrigin(Enum):
    """候補本文の出自。§1.2のTrust Zoneに対応する。"""

    # Z0 Control Plane。Harness自身が組み立てたPolicy／Instruction／Tool定義。
    CONTROL_PLANE = "CONTROL_PLANE"
    # Z5 Artifact Input を §1.16 の SafeInputReader 経由で読み、Scanを通したもの。
    VERIFIED_INPUT = "VERIFIED_INPUT"


# Control Planeだけが名乗れるRole。Untrusted入力がここへ来ることを拒否する。
_CONTROL_PLANE_ROLES: Final[frozenset[MessageRole]] = frozenset(
    {MessageRole.SYSTEM_CONTROL, MessageRole.DEVELOPER_CONTROL, MessageRole.TOOL_DEFINITION}
)

# §1.12 Estimate Assurance の強さ。計数器がSnapshotの主張より弱い保証しか
# 出せない場合は、推測して通さず停止する（不変条件#9）。
_ASSURANCE_RANK: Final[dict[EstimateAssurance, int]] = {
    EstimateAssurance.UNKNOWN: 0,
    EstimateAssurance.CONSERVATIVE: 1,
    EstimateAssurance.EXACT: 2,
}


@dataclass(frozen=True, slots=True)
class FragmentSource:
    """Fragment候補1件。本文を保持するのはApplication層のこの型だけ。

    `priority`はspec §3.6の優先順位を呼出側が数値へ写した明示値である。
    §3.6の9段階と`message_role`の7値enumとの写像はRegistryにもspecにも
    定義が無いため、Serviceが推測で写像を作らない。
    """

    fragment_id: str
    text: str
    message_role: MessageRole
    origin: FragmentOrigin
    control_authority: bool = False
    instruction_eligible: bool = False
    mandatory: bool = False
    priority: int = 0
    source_artifact_id: str = ""
    classification_scan_evidence_hash: ContentHash | None = None
    input_read_capability_id: str | None = None
    compressible: bool = False
    compression_depth: int = 0

    def __post_init__(self) -> None:
        if not self.fragment_id:
            raise ValueError("fragment_id must not be empty")

    def require_admissible(self) -> None:
        """§3.6 手順1。証跡が揃っていない候補を拒否する。"""
        if self.origin is FragmentOrigin.CONTROL_PLANE:
            if self.message_role not in _CONTROL_PLANE_ROLES:
                raise HarnessError(
                    ErrorCode.PATH_OUTSIDE_CAPABILITY,
                    f"control plane origin cannot carry message role {self.message_role.value}",
                )
            return

        if self.message_role in _CONTROL_PLANE_ROLES:
            raise HarnessError(
                ErrorCode.CONTROL_DATA_ROLE_ESCALATION,
                f"input-originated content cannot claim control plane role "
                f"{self.message_role.value}",
            )
        missing = [
            name
            for name, value in (
                ("input_read_capability_id", self.input_read_capability_id),
                ("classification_scan_evidence_hash", self.classification_scan_evidence_hash),
                ("source_artifact_id", self.source_artifact_id or None),
            )
            if not value
        ]
        if missing:
            raise HarnessError(
                ErrorCode.PATH_OUTSIDE_CAPABILITY,
                f"context candidate {self.fragment_id} lacks read evidence: {', '.join(missing)}",
            )


@dataclass(frozen=True, slots=True)
class ContextAssemblyService:
    """決定論的Context Snapshotの構築と保存。"""

    token_counter: TokenCounterPort
    artifact_store: ArtifactStorePort
    unit_of_work: UnitOfWorkPort | None = None

    def build(
        self,
        sources: Sequence[FragmentSource],
        policy: TokenBudgetPolicy,
        profile: TokenProfileSnapshot,
        *,
        bundle_id: str,
        receipt_id: str,
        now: str,
        rejected_input_resources: Sequence[dict[str, Any]] = (),
        algorithm_version: str = CONTEXT_SELECTION_ALGORITHM_VERSION,
    ) -> ContextAssembly:
        """候補からContext Snapshotを組み立てる。

        同一の`sources`・`policy`・`profile`からは常に同一の`bundle_hash`になる。
        `now`はSnapshotのExpiry判定にだけ使い、Bundleの内容へ入れない
        （不変条件#4「Snapshotへ時刻を含めない」）。
        """
        profile.require_usable(now=now)
        for source in sources:
            source.require_admissible()
        fragments = tuple(self._to_fragment(source, profile) for source in sources)
        selection = select_context(fragments, policy, profile)
        over_budget = {
            item.fragment_id
            for item in selection.excluded_fragments
            if item.reason is ExclusionReason.BUDGET
        }
        compressed: list[str] = []
        revised = list(fragments)
        for index, source in enumerate(sources):
            if source.fragment_id not in over_budget:
                continue
            summary = self._compress(source, maximum_depth=policy.compression_max_depth)
            if summary is None:
                continue
            text, document = summary
            candidate = self._to_fragment(replace(source, text=text), profile)
            if candidate.token_count >= fragments[index].token_count:
                continue
            artifact = canonicalize(document)
            compression_id = str(hash_bytes(artifact))
            if self.unit_of_work is None:
                raise HarnessError(
                    ErrorCode.RUNTIME_SPEC_MISMATCH, "compression requires durable artifact storage"
                )
            with self.unit_of_work.begin_immediate():
                for payload in (source.text.encode("utf-8"), text.encode("utf-8"), artifact):
                    self._persist_bytes(payload, now=now)
            revised[index] = replace(
                candidate, source_artifact_id=str(hash_bytes(text.encode("utf-8")))
            )
            compressed.append(compression_id)
        return build_context_bundle(
            tuple(revised),
            policy,
            profile,
            bundle_id=bundle_id,
            receipt_id=receipt_id,
            input_read_capability_set_hash=_capability_set_hash(sources),
            input_read_evidence_hash=_read_evidence_hash(sources),
            rejected_input_resources=rejected_input_resources,
            algorithm_version="context-selection/3" if compressed else algorithm_version,
            compression_artifact_ids=tuple(sorted(compressed)),
        )

    def build_verified_input_bundle(
        self,
        *,
        text: str,
        evidence: ReadEvidence,
        policy: TokenBudgetPolicy,
        profile: TokenProfileSnapshot,
        bundle_id: str,
        receipt_id: str,
        now: str,
        candidates: tuple[VerifiedContextInput, ...] = (),
        rejected_input_resources: tuple[dict[str, str], ...] = (),
    ) -> ContextAssembly:
        """検証済み入力1件から Context Snapshot を組む（`ContextAssemblyPort`）。

        Role は `UNTRUSTED_ARTIFACT_DATA` に**固定する**。呼出側へ選ばせない。
        選ばせれば、入力由来の本文が `SYSTEM_CONTROL` を名乗る経路ができる
        （§3.6 手順2、`CONTROL_DATA_ROLE_ESCALATION`）。拡張子や分類結果で
        Role を変えることもしない。

        `mandatory=True` にするのは、Task 本文を落として Plan を立てないため
        である。落とすくらいなら `CONTEXT_BUDGET_EXCEEDED` で止める。
        """
        source = FragmentSource(
            fragment_id=f"task:{evidence.content_hash}",
            text=text,
            message_role=MessageRole.UNTRUSTED_ARTIFACT_DATA,
            origin=FragmentOrigin.VERIFIED_INPUT,
            control_authority=False,
            instruction_eligible=False,
            mandatory=True,
            priority=0,
            source_artifact_id=str(evidence.content_hash),
            classification_scan_evidence_hash=hash_canonical(
                {
                    "capability_id": evidence.capability_id,
                    "canonical_path": evidence.canonical_path,
                    "content_hash": str(evidence.content_hash),
                    "size_bytes": evidence.size_bytes,
                },
                artifact_type="classification-scan-evidence",
                schema_major=1,
            ),
            input_read_capability_id=evidence.capability_id,
        )
        sources = [source]
        for item in sorted(candidates, key=lambda item: item.evidence.canonical_path):
            candidate_source = replace(
                source,
                fragment_id="reference:" + item.evidence.canonical_path,
                text=item.text,
                mandatory=item.mandatory,
                source_artifact_id=str(item.evidence.content_hash),
                input_read_capability_id=item.evidence.capability_id,
                classification_scan_evidence_hash=hash_canonical(
                    {
                        "path": item.evidence.canonical_path,
                        "content_hash": str(item.evidence.content_hash),
                        "size_bytes": item.evidence.size_bytes,
                    },
                    artifact_type="classification-scan-evidence",
                    schema_major=1,
                ),
                compressible=item.compressible,
                compression_depth=item.compression_depth,
            )
            sources.append(candidate_source)
        assembly = self.build(
            sources,
            policy,
            profile,
            bundle_id=bundle_id,
            receipt_id=receipt_id,
            now=now,
            rejected_input_resources=rejected_input_resources,
        )
        if self.unit_of_work is not None:
            with self.unit_of_work.begin_immediate():
                for stored_source in sources:
                    self._persist_bytes(stored_source.text.encode("utf-8"), now=now)
                self._persist_bytes(self.bundle_bytes(assembly.bundle), now=now)
                self._persist_bytes(
                    canonicalize(
                        assembly.receipt.to_record(
                            record_id=receipt_id, created_at=now, producer="context-assembly/3"
                        )
                    ),
                    now=now,
                )
        return assembly

    def _persist_bytes(self, payload: bytes, *, now: str) -> None:
        self.artifact_store.put(
            payload,
            ArtifactMetadata(
                media_type="application/octet-stream",
                size_bytes=len(payload),
                data_classification="SYNTHETIC",
                trust_level="UNTRUSTED_INPUT",
            ),
            artifact_id=str(hash_bytes(payload)),
            stored_at=now,
        )

    @staticmethod
    def _compress(
        source: FragmentSource, *, maximum_depth: int
    ) -> tuple[str, dict[str, Any]] | None:
        # Mock is lossless with respect to distinct lines. Never infer facts or drop
        # security/approval constraints to make an otherwise rejected plan fit.
        if (
            not source.compressible
            or source.mandatory
            or source.control_authority
            or source.instruction_eligible
            or source.origin is not FragmentOrigin.VERIFIED_INPUT
            or not source.source_artifact_id
            or source.compression_depth < 0
            or source.compression_depth >= min(maximum_depth, 2)
        ):
            return None
        protected = (
            "security",
            "policy",
            "approval",
            "secret",
            "credential",
            "must",
            "prohibit",
            "forbid",
            "禁止",
            "承認",
            "秘密",
            "機密",
            "制約",
            "ポリシー",
        )
        if any(word in (source.fragment_id + " " + source.text).casefold() for word in protected):
            return None
        lines: dict[str, tuple[int, int]] = {}
        offset = 0
        for line in source.text.splitlines(keepends=True):
            if line not in lines:
                lines[line] = (offset, offset + len(line))
            offset += len(line)
        text = "".join(lines)
        if text == source.text:
            return None
        return text, {
            "algorithm": "mock-distinct-lines/1",
            "compression_depth": source.compression_depth + 1,
            "source_artifact_id": source.source_artifact_id,
            "source_content_hash": str(hash_bytes(source.text.encode("utf-8"))),
            "summary_content_hash": str(hash_bytes(text.encode("utf-8"))),
            "source_spans": [{"start": start, "end": end} for start, end in lines.values()],
            "span_unit": "unicode-code-point",
            "message_role": source.message_role.value,
        }

    def persist_bundle(
        self,
        bundle: ContextBundle,
        *,
        artifact_id: str,
        stored_at: str,
        data_classification: str,
        trust_level: str,
    ) -> ArtifactManifestRecord:
        """Bundleの Canonical BytesをCASへ保存する。

        `ContextBundle`のRelational Storeは`migrations.py`に存在しない。
        Bytesは既存のArtifact CAS（不変条件#13の順序を守る具象実装）へ置き、
        ManifestだけがSQLite側に載る（ADR-001）。
        """
        payload = self.bundle_bytes(bundle)
        metadata = ArtifactMetadata(
            media_type=_BUNDLE_MEDIA_TYPE,
            size_bytes=len(payload),
            data_classification=data_classification,
            trust_level=trust_level,
        )
        return self.artifact_store.put(
            payload, metadata, artifact_id=artifact_id, stored_at=stored_at
        )

    def load_and_verify_bundle(
        self, manifest: ArtifactManifestRecord, *, expected: ContextBundle
    ) -> None:
        """保存済みBytesを読み戻し、`bundle_hash`を再計算して照合する。

        Task完了条件「Snapshot Hashを保存・再計算できるようにする」の検証点。
        不一致は推測で通さず`ARTIFACT_CONTENT_CONFLICT`で停止する。
        """
        self.artifact_store.verify(manifest.content_hash).raise_if_repair_required()
        stored = self.artifact_store.get(manifest.content_hash)
        if hash_bytes(stored) != manifest.content_hash:
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT,
                "stored context bundle bytes do not match the manifest content hash",
            )
        if stored != self.bundle_bytes(expected):
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT,
                "stored context bundle bytes do not match the expected bundle",
            )
        expected.assert_integrity()

    @staticmethod
    def bundle_bytes(bundle: ContextBundle) -> bytes:
        """§1.11 Canonical Bytes。保存とHash再計算の唯一の表現。"""
        return canonicalize(bundle.canonical_content())

    def _to_fragment(
        self, source: FragmentSource, profile: TokenProfileSnapshot
    ) -> ContextFragment:
        counted = self.token_counter.count(source.text, profile=profile)
        profile.require_counter_identity(
            tokenizer_name=counted.tokenizer_name,
            tokenizer_version=counted.tokenizer_version,
            counting_adapter_version=counted.counting_adapter_version,
        )
        self._require_assurance(counted.estimate_assurance, profile.estimate_assurance)
        # 本文はHashだけを残して捨てる。以降どの層にも`text`は渡らない。
        return ContextFragment(
            fragment_id=source.fragment_id,
            fragment_content_hash=hash_bytes(source.text.encode("utf-8")),
            token_count=counted.tokens,
            message_role=source.message_role,
            control_authority=source.control_authority,
            instruction_eligible=source.instruction_eligible,
            mandatory=source.mandatory,
            priority=source.priority,
            source_artifact_id=source.source_artifact_id,
            classification_scan_evidence_hash=source.classification_scan_evidence_hash,
            input_read_capability_id=source.input_read_capability_id,
        )

    @staticmethod
    def _require_assurance(counted: EstimateAssurance, declared: EstimateAssurance) -> None:
        if counted is EstimateAssurance.UNKNOWN:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "token counter reported estimate_assurance=UNKNOWN",
            )
        if _ASSURANCE_RANK[counted] < _ASSURANCE_RANK[declared]:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "token counter assurance is weaker than the token profile snapshot declares",
            )


def control_data_policy_hash() -> ContentHash:
    """`ExecutionPlan.control_data_policy_hash`。

    Plan Content は「いまどの Control／Data 分離規則の下で組んだ Context か」を
    束縛する必要がある（§3.7.1）。その規則の実体は次の2つである。

    * `MessageRole` の語彙（正本は `schemas/core/ContextFragment/1.0.0.schema.json`）
    * そのうち Control Plane だけが名乗れる Role の集合（§3.6 手順2）

    どちらも Code Point 昇順で整列してから Hash する（不変条件#6）。**ここで
    新しい Policy を発明しない。** 規則が変われば Hash が動き、同じ Task でも
    別の Plan Content になる。それが狙いである。
    """
    return hash_canonical(
        {
            "message_roles": sorted(role.value for role in MessageRole),
            "control_plane_roles": sorted(role.value for role in _CONTROL_PLANE_ROLES),
            "algorithm_version": CONTEXT_SELECTION_ALGORITHM_VERSION,
        },
        artifact_type="control-data-policy",
        schema_major=1,
    )


def _capability_set_hash(sources: Sequence[FragmentSource]) -> ContentHash:
    """`input_read_capability_set_hash`。使用したCapability IDの集合を束縛する。

    Code Point昇順で整列した重複無しのListを入力にする（不変条件#6）。
    """
    identifiers = sorted(
        {source.input_read_capability_id for source in sources if source.input_read_capability_id}
    )
    return hash_canonical(
        {"input_read_capability_ids": identifiers},
        artifact_type="input-read-capability-set",
        schema_major=1,
    )


def _read_evidence_hash(sources: Sequence[FragmentSource]) -> ContentHash:
    """`input_read_evidence_hash`。どの候補をどの証跡で通したかを束縛する。

    同じ本文でも、別のCapabilityやScan Evidenceで通ったものは別のContextである。
    """
    evidence = sorted(
        (
            {
                "fragment_id": source.fragment_id,
                "origin": source.origin.value,
                "input_read_capability_id": source.input_read_capability_id,
                "classification_scan_evidence_hash": (
                    None
                    if source.classification_scan_evidence_hash is None
                    else str(source.classification_scan_evidence_hash)
                ),
                "source_artifact_id": source.source_artifact_id,
            }
            for source in sources
        ),
        key=lambda item: str(item["fragment_id"]),
    )
    return hash_canonical(
        {"input_read_evidence": evidence},
        artifact_type="input-read-evidence",
        schema_major=1,
    )
