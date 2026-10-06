"""`design-source/registries/masking-policy.yaml` の読込み（ADR-007 §6）。

REJECT境界・Mask Token・Span制約・正規化Profileの正本はRegistryであり、
本Moduleへ値を手入力しない（不変条件#18）。

## 既定値を持たせない

`ratio_unset: REJECT` が示すとおり、項目が欠けている状態は
「既定値で続行」ではなく停止である。欠落へ既定値を当てる実装は、
Registryを削っただけで制約が消える経路を作る。`_require()` は
欠落を必ず例外にする。

## Contract文字列の照合

`grapheme_algorithm` や `artifact_bit_order` のような項目は、
**実装がその方式であることの表明**である。Registry側だけを別方式へ
書き換えても実装は追随しないため、一致しなければ
`RUNTIME_SPEC_MISMATCH` で停止する。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from harness.domain.canonical import canonicalize
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_bytes
from harness.domain.masking import RISKY_CLASS_NAMES, SpanConstraints
from harness.infrastructure.masking.pictographic import ExtendedPictographicTable
from harness.ports.masker import MaskerIsolationReport

__all__ = [
    "GraphemePolicy",
    "MaskerIsolationPolicy",
    "MaskingPolicy",
    "NormalizationPolicy",
]

# 本実装が実現している方式。Registryがこれ以外を指定したら停止する。
_IMPLEMENTED_POLICY_VERSION: Final[int] = 4
_IMPLEMENTED_OFFSET_BASIS: Final[str] = "UNICODE_SCALAR_VALUE"
_IMPLEMENTED_INTERVAL: Final[str] = "HALF_OPEN"
_IMPLEMENTED_RATIO_BASIS: Final[str] = "LLM_ADDITIONAL_UNION_CODE_POINTS"
_IMPLEMENTED_RATIO_DENOMINATOR: Final[str] = "NORMALIZED_CODEPOINT_COUNT"
# V2でExtended_PictographicをUnicode実データの表へ切り替えた。
# V1（範囲近似）とは判定が違うため、Registryが古い版を指していたら止める。
_IMPLEMENTED_GRAPHEME_ALGORITHM: Final[str] = "CONSERVATIVE_RISKY_CODEPOINT_GUARD_V2"
_IMPLEMENTED_NORMALIZATION_ENGINE: Final[str] = "RUNTIME_UNICODEDATA_WITH_UCD14_ASSIGNED_GUARD"
_IMPLEMENTED_ENGINE_CONTRACT: Final[str] = "UAX15_NFC_RUNTIME_WITH_UCD14_GUARD_V1"
_IMPLEMENTED_ARTIFACT_FORMAT: Final[str] = "UCD14_ASSIGNED_BITMAP_V1"
_IMPLEMENTED_ARTIFACT_BIT_ORDER: Final[str] = "CODEPOINT_ASCENDING_LSB0"
_IMPLEMENTED_NORMALIZATION_FORM: Final[str] = "NFC"


def _require(mapping: dict[str, Any], key: str, context: str) -> Any:
    """欠落を既定値で埋めない。"""
    if key not in mapping:
        raise HarnessError(
            ErrorCode.RUNTIME_SPEC_MISMATCH,
            f"masking-policy.yaml is missing {context}.{key}",
        )
    return mapping[key]


def _require_equals(actual: object, expected: object, context: str) -> None:
    if actual != expected:
        raise HarnessError(
            ErrorCode.RUNTIME_SPEC_MISMATCH,
            f"{context}: registry declares {actual!r} but this implementation "
            f"provides {expected!r}",
        )


def _pictographic_table(document: dict[str, Any], repo_root: Path) -> ExtendedPictographicTable:
    """Grapheme判定表を組み立てる。Hash照合はTable側が行う。"""
    section = _require(document, "grapheme", "<root>")
    return ExtendedPictographicTable(
        repo_root / str(_require(section, "pictographic_artifact_path", "grapheme")),
        expected_sha256=str(_require(section, "pictographic_artifact_sha256", "grapheme")),
    )


@dataclass(frozen=True, slots=True)
class GraphemePolicy:
    """Grapheme判定に使うVendor表の素性。"""

    pictographic_artifact_path: str
    pictographic_artifact_sha256: str
    pictographic_source_url: str
    pictographic_source_sha256: str


@dataclass(frozen=True, slots=True)
class NormalizationPolicy:
    profile_id: str
    artifact_path: str
    artifact_sha256: str
    artifact_size_bytes: int
    runtime_minimum_version: str
    ucd_source_url: str
    ucd_source_sha256: str


@dataclass(frozen=True, slots=True)
class MaskerIsolationPolicy:
    """§ADR-007 §7。Raw PII を渡す**前**に成立していなければならない条件。

    `enforcement: PRE_LAUNCH_VERIFY` であるため、Maskerを起動してから
    確認するのでは遅い。1度でも起動すればRaw PIIが渡り得る。
    """

    network_egress_denied: bool
    telemetry_disabled: bool
    prompt_logging_disabled: bool
    core_dump_disabled: bool
    temp_files_disallowed: bool
    memory_lock_required: bool

    def unmet(self, report: MaskerIsolationReport) -> tuple[str, ...]:
        """Registryが要求している項目のうち、成立していないものを返す。

        要求していない項目は判定しない。実装側に必須項目を固定すると、
        Registryを変えても挙動が追随しない。
        """
        return tuple(
            name
            for name, required, satisfied in (
                (
                    "network_egress_denied",
                    self.network_egress_denied,
                    report.network_egress_denied,
                ),
                (
                    "telemetry_disabled",
                    self.telemetry_disabled,
                    report.telemetry_disabled,
                ),
                (
                    "prompt_logging_disabled",
                    self.prompt_logging_disabled,
                    report.prompt_logging_disabled,
                ),
                (
                    "core_dump_disabled",
                    self.core_dump_disabled,
                    report.core_dump_disabled,
                ),
                (
                    "temp_files_disallowed",
                    self.temp_files_disallowed,
                    report.temp_files_disallowed,
                ),
                ("memory_locked", self.memory_lock_required, report.memory_locked),
            )
            if required and not satisfied
        )


@dataclass(frozen=True, slots=True)
class MaskingPolicy:
    snapshot_hash: str
    policy_version: int
    reject_categories: tuple[str, ...]
    maskable_categories: tuple[str, ...]
    mask_tokens: dict[str, str]
    span_constraints: SpanConstraints
    normalization: NormalizationPolicy
    grapheme: GraphemePolicy
    isolation: MaskerIsolationPolicy
    max_maskable_bytes: int
    masker_timeout_seconds: int
    never_pass_unmasked: bool
    auto_downgrade_after_masking: bool

    @property
    def known_categories(self) -> frozenset[str]:
        """LLMがSpanへ付けてよいカテゴリ。REJECT側は含めない。

        REJECTカテゴリのSpanが提案された場合は`MASKING_CATEGORY_UNKNOWN`で
        止まる。REJECTカテゴリは検出時点で入力ごと拒否されており、
        Maskerへ到達していないためである。
        """
        return frozenset(self.maskable_categories)

    # ------------------------------------------------------------------

    @classmethod
    def load(cls, repo_root: Path, registries: Path | None = None) -> MaskingPolicy:
        directory = registries or (repo_root / "design-source" / "registries")
        document: dict[str, Any] = yaml.safe_load(
            (directory / "masking-policy.yaml").read_text(encoding="utf-8")
        )

        version = _require(document, "masking_policy_version", "<root>")
        _require_equals(version, _IMPLEMENTED_POLICY_VERSION, "masking_policy_version")

        reject = tuple(str(row["id"]) for row in _require(document, "reject_categories", "<root>"))
        categories = _require(document, "categories", "<root>")
        maskable = tuple(str(row["id"]) for row in categories)
        tokens = {str(row["id"]): str(row["mask_token"]) for row in categories}

        overlap = set(reject) & set(maskable)
        if overlap:
            # 同じカテゴリがRejectとMaskableの両方にあると、順序次第で
            # Secretがマスクされて通過し得る。
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                f"categories appear in both reject and maskable lists: {sorted(overlap)}",
            )
        if len(tokens) != len(maskable):
            raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "duplicate maskable category id")

        return cls(
            snapshot_hash=str(hash_bytes(canonicalize(document))),
            policy_version=int(version),
            reject_categories=reject,
            maskable_categories=maskable,
            mask_tokens=tokens,
            span_constraints=cls._span_constraints(document, repo_root),
            normalization=cls._normalization(document),
            grapheme=cls._grapheme(document),
            isolation=cls._isolation(document),
            max_maskable_bytes=int(_require(document, "max_maskable_bytes", "<root>")),
            masker_timeout_seconds=int(_require(document, "masker_timeout_seconds", "<root>")),
            never_pass_unmasked=bool(
                _require(
                    _require(document, "failure_handling", "<root>"),
                    "never_pass_unmasked",
                    "failure_handling",
                )
            ),
            auto_downgrade_after_masking=bool(
                _require(
                    _require(document, "classification", "<root>"),
                    "auto_downgrade_after_masking",
                    "classification",
                )
            ),
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _span_constraints(document: dict[str, Any], repo_root: Path) -> SpanConstraints:
        section = _require(document, "span_constraints", "<root>")
        context = "span_constraints"

        _require_equals(
            _require(section, "offset_basis", context),
            _IMPLEMENTED_OFFSET_BASIS,
            f"{context}.offset_basis",
        )
        _require_equals(
            _require(section, "interval", context),
            _IMPLEMENTED_INTERVAL,
            f"{context}.interval",
        )
        _require_equals(
            _require(section, "ratio_basis", context),
            _IMPLEMENTED_RATIO_BASIS,
            f"{context}.ratio_basis",
        )
        _require_equals(
            _require(section, "ratio_denominator", context),
            _IMPLEMENTED_RATIO_DENOMINATOR,
            f"{context}.ratio_denominator",
        )
        _require_equals(
            _require(section, "grapheme_algorithm", context),
            _IMPLEMENTED_GRAPHEME_ALGORITHM,
            f"{context}.grapheme_algorithm",
        )
        # 分子からScan#1候補を除く前提でDomain側を実装している。
        # falseへ変えると決定論側の判断をCircuit Breakerが却下し得る。
        _require_equals(
            _require(section, "deterministic_scan1_excluded", context),
            True,
            f"{context}.deterministic_scan1_excluded",
        )
        # 空文字へのSpanは分母0となる。REJECT以外の指定は実装が持たない。
        _require_equals(
            _require(section, "ratio_empty_text", context),
            "REJECT",
            f"{context}.ratio_empty_text",
        )
        _require_equals(
            _require(section, "ratio_unset", context),
            "REJECT",
            f"{context}.ratio_unset",
        )

        risky_classes = frozenset(
            str(name) for name in _require(section, "grapheme_risky_classes", context)
        )
        # Registryの危険クラス名と、実装が返しうる名前が一致すること。
        #
        # 判定は `_risky_class(...) in risky_classes` である。食い違うと
        # 例外にならず、**そのクラスの拒否が静かに消える**。Registryへ
        # 追加して実装を足し忘れた場合も、実装のTypoでも、症状は同じ
        # 「何も起きない」になる。緩む方向へ黙って倒れるため読込み時に止める。
        unknown = sorted(risky_classes - RISKY_CLASS_NAMES)
        if unknown:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                f"{context}.grapheme_risky_classes declares classes this implementation "
                f"cannot detect: {unknown}",
            )
        unimplemented = sorted(RISKY_CLASS_NAMES - risky_classes)
        if unimplemented:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                f"{context}.grapheme_risky_classes omits classes this implementation "
                f"detects: {unimplemented}",
            )

        return SpanConstraints(
            max_spans=int(_require(section, "max_spans", context)),
            max_mask_ratio=float(_require(section, "max_mask_ratio", context)),
            max_single_llm_addition_ratio=float(
                _require(section, "max_single_llm_addition_ratio", context)
            ),
            min_span_length=int(_require(section, "min_span_length", context)),
            allow_overlap=bool(_require(section, "allow_overlap", context)),
            allow_nesting=bool(_require(section, "allow_nesting", context)),
            require_sorted_by_start=bool(_require(section, "require_sorted_by_start", context)),
            risky_classes=risky_classes,
            # 判定表はRegistryが指すArtifactから作る。Hash照合はTable側が行う。
            pictographic=_pictographic_table(document, repo_root),
        )

    @staticmethod
    def _grapheme(document: dict[str, Any]) -> GraphemePolicy:
        section = _require(document, "grapheme", "<root>")
        context = "grapheme"
        _require_equals(
            _require(section, "pictographic_artifact_format", context),
            "UCD14_EXTENDED_PICTOGRAPHIC_BITMAP_V1",
            f"{context}.pictographic_artifact_format",
        )
        _require_equals(
            _require(section, "pictographic_artifact_bit_order", context),
            _IMPLEMENTED_ARTIFACT_BIT_ORDER,
            f"{context}.pictographic_artifact_bit_order",
        )
        return GraphemePolicy(
            pictographic_artifact_path=str(
                _require(section, "pictographic_artifact_path", context)
            ),
            pictographic_artifact_sha256=str(
                _require(section, "pictographic_artifact_sha256", context)
            ),
            pictographic_source_url=str(_require(section, "pictographic_source_url", context)),
            pictographic_source_sha256=str(
                _require(section, "pictographic_source_sha256", context)
            ),
        )

    @staticmethod
    def _normalization(document: dict[str, Any]) -> NormalizationPolicy:
        section = _require(document, "normalization", "<root>")
        context = "normalization"

        _require_equals(
            _require(section, "form", context),
            _IMPLEMENTED_NORMALIZATION_FORM,
            f"{context}.form",
        )
        _require_equals(
            _require(section, "engine", context),
            _IMPLEMENTED_NORMALIZATION_ENGINE,
            f"{context}.engine",
        )
        _require_equals(
            _require(section, "engine_contract", context),
            _IMPLEMENTED_ENGINE_CONTRACT,
            f"{context}.engine_contract",
        )
        _require_equals(
            _require(section, "artifact_format", context),
            _IMPLEMENTED_ARTIFACT_FORMAT,
            f"{context}.artifact_format",
        )
        _require_equals(
            _require(section, "artifact_bit_order", context),
            _IMPLEMENTED_ARTIFACT_BIT_ORDER,
            f"{context}.artifact_bit_order",
        )
        _require_equals(
            _require(section, "index_unit", context),
            _IMPLEMENTED_OFFSET_BASIS,
            f"{context}.index_unit",
        )
        # 期待Hashを持たないまま「Hash必須」と宣言した状態を通さない。
        _require_equals(
            _require(section, "engine_artifact_hash_required", context),
            True,
            f"{context}.engine_artifact_hash_required",
        )

        return NormalizationPolicy(
            profile_id=str(_require(section, "profile_id", context)),
            artifact_path=str(_require(section, "engine_artifact_path", context)),
            artifact_sha256=str(_require(section, "engine_artifact_sha256", context)),
            artifact_size_bytes=int(_require(section, "artifact_size_bytes", context)),
            runtime_minimum_version=str(
                _require(section, "runtime_unicodedata_min_version", context)
            ),
            ucd_source_url=str(_require(section, "ucd_source_url", context)),
            ucd_source_sha256=str(_require(section, "ucd_source_sha256", context)),
        )

    @staticmethod
    def _isolation(document: dict[str, Any]) -> MaskerIsolationPolicy:
        section = _require(document, "masker_isolation", "<root>")
        context = "masker_isolation"
        return MaskerIsolationPolicy(
            network_egress_denied=_require(section, "network_egress", context) == "DENY",
            telemetry_disabled=_require(section, "telemetry", context) == "DISABLED",
            prompt_logging_disabled=_require(section, "prompt_logging", context) == "DISABLED",
            core_dump_disabled=_require(section, "core_dump", context) == "DISABLED",
            temp_files_disallowed=_require(section, "temp_files", context) == "DISALLOWED",
            memory_lock_required=_require(section, "memory_lock", context) == "REQUIRED",
        )
