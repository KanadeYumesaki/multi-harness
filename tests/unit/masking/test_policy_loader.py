"""`masking-policy.yaml` 読込みの試験（不変条件#18）。

「Registryを削ると制約が消える」経路が無いことを確かめる。
既定値で補う実装は、この試験群が落ちる。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from harness.domain.errors import ErrorCode, HarnessError
from harness.infrastructure.masking.policy import MaskingPolicy
from harness.ports.masker import MaskerIsolationReport

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE = REPO_ROOT / "design-source" / "registries" / "masking-policy.yaml"


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(SOURCE.read_text(encoding="utf-8"))
    return data


def write_policy(directory: Path, document: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "masking-policy.yaml").write_text(
        yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
    )
    return directory


# ---------------------------------------------------------------------------
# 正本の読込み
# ---------------------------------------------------------------------------


def test_real_registry_loads() -> None:
    policy = MaskingPolicy.load(REPO_ROOT)
    assert policy.policy_version == 4
    assert policy.never_pass_unmasked is True
    assert policy.auto_downgrade_after_masking is False


def test_every_maskable_category_has_a_mask_token() -> None:
    """Tokenの無いカテゴリがあると、そのSpanで置換が失敗する。"""
    policy = MaskingPolicy.load(REPO_ROOT)
    assert set(policy.mask_tokens) == set(policy.maskable_categories)
    assert all(token for token in policy.mask_tokens.values())


def test_mask_tokens_are_distinct() -> None:
    """Tokenが重複すると、マスク後の本文からカテゴリを復元できない。"""
    policy = MaskingPolicy.load(REPO_ROOT)
    tokens = list(policy.mask_tokens.values())
    assert len(set(tokens)) == len(tokens)


def test_national_id_is_a_reject_category_in_mvp0a() -> None:
    """マイナンバー等の特定個人情報。マスクせず拒否する。"""
    policy = MaskingPolicy.load(REPO_ROOT)
    assert "NATIONAL_ID" in policy.reject_categories
    assert "NATIONAL_ID" not in policy.maskable_categories


def test_known_categories_exclude_reject_categories() -> None:
    policy = MaskingPolicy.load(REPO_ROOT)
    assert not (policy.known_categories & set(policy.reject_categories))


def test_span_constraints_come_from_the_registry(document: dict[str, Any]) -> None:
    policy = MaskingPolicy.load(REPO_ROOT)
    section = document["span_constraints"]
    assert policy.span_constraints.max_spans == section["max_spans"]
    assert policy.span_constraints.max_mask_ratio == section["max_mask_ratio"]
    assert policy.span_constraints.risky_classes == frozenset(section["grapheme_risky_classes"])


# ---------------------------------------------------------------------------
# 欠落は既定値で埋めない
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("span_constraints", "max_mask_ratio"),
        ("span_constraints", "max_spans"),
        ("span_constraints", "min_span_length"),
        ("span_constraints", "grapheme_risky_classes"),
        ("normalization", "engine_artifact_sha256"),
        ("normalization", "profile_id"),
        ("normalization", "runtime_unicodedata_min_version"),
    ],
)
def test_missing_key_is_rejected(
    tmp_path: Path, document: dict[str, Any], section: str, key: str
) -> None:
    """`ratio_unset: REJECT`。欠落を既定値で補う実装なら、この試験が落ちる。"""
    broken = copy.deepcopy(document)
    del broken[section][key]
    write_policy(tmp_path, broken)
    with pytest.raises(HarnessError) as error:
        MaskingPolicy.load(REPO_ROOT, registries=tmp_path)
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
    assert key in str(error.value)


@pytest.mark.parametrize(
    "key",
    ["max_maskable_bytes", "masker_timeout_seconds", "categories", "reject_categories"],
)
def test_missing_root_key_is_rejected(tmp_path: Path, document: dict[str, Any], key: str) -> None:
    broken = copy.deepcopy(document)
    del broken[key]
    write_policy(tmp_path, broken)
    with pytest.raises(HarnessError):
        MaskingPolicy.load(REPO_ROOT, registries=tmp_path)


# ---------------------------------------------------------------------------
# Contract照合
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("span_constraints", "offset_basis", "UTF16_CODE_UNIT"),
        ("span_constraints", "interval", "CLOSED"),
        ("span_constraints", "ratio_basis", "ALL_UNION_CODE_POINTS"),
        ("span_constraints", "deterministic_scan1_excluded", False),
        ("span_constraints", "grapheme_algorithm", "UAX29_FULL"),
        ("span_constraints", "ratio_empty_text", "ALLOW"),
        ("normalization", "form", "NFKC"),
        ("normalization", "engine", "CUSTOM_NFC"),
        ("normalization", "artifact_bit_order", "CODEPOINT_ASCENDING_MSB0"),
        ("normalization", "engine_artifact_hash_required", False),
    ],
)
def test_registry_declaring_an_unimplemented_method_stops_the_load(
    tmp_path: Path, document: dict[str, Any], section: str, key: str, value: object
) -> None:
    """Registryだけ書き換えても実装は追随しない。不一致は停止させる。

    ここを黙って通すと、Registryは新方式を宣言しているのに実行は
    旧方式のまま、という状態が成立する。
    """
    broken = copy.deepcopy(document)
    broken[section][key] = value
    write_policy(tmp_path, broken)
    with pytest.raises(HarnessError) as error:
        MaskingPolicy.load(REPO_ROOT, registries=tmp_path)
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH


def test_policy_version_bump_requires_reviewing_the_implementation(
    tmp_path: Path, document: dict[str, Any]
) -> None:
    broken = copy.deepcopy(document)
    broken["masking_policy_version"] = 5
    write_policy(tmp_path, broken)
    with pytest.raises(HarnessError, match="masking_policy_version"):
        MaskingPolicy.load(REPO_ROOT, registries=tmp_path)


def test_category_in_both_reject_and_maskable_is_rejected(
    tmp_path: Path, document: dict[str, Any]
) -> None:
    """順序次第でSecretがマスクされて通過し得る配置を許さない。"""
    broken = copy.deepcopy(document)
    broken["categories"].append({"id": "NATIONAL_ID", "mask_token": "[MASKED:ID]"})
    write_policy(tmp_path, broken)
    with pytest.raises(HarnessError, match="both reject and maskable"):
        MaskingPolicy.load(REPO_ROOT, registries=tmp_path)


# ---------------------------------------------------------------------------
# 隔離要件
# ---------------------------------------------------------------------------


def test_all_isolation_requirements_are_demanded_by_the_registry() -> None:
    policy = MaskingPolicy.load(REPO_ROOT)
    assert policy.isolation.network_egress_denied
    assert policy.isolation.telemetry_disabled
    assert policy.isolation.prompt_logging_disabled
    assert policy.isolation.core_dump_disabled
    assert policy.isolation.temp_files_disallowed
    assert policy.isolation.memory_lock_required


def test_unmet_lists_only_what_the_registry_requires(
    tmp_path: Path, document: dict[str, Any]
) -> None:
    """要求していない項目は判定しない。判定表を実装側に固定しない。"""
    relaxed = copy.deepcopy(document)
    relaxed["masker_isolation"]["memory_lock"] = "OPTIONAL"
    write_policy(tmp_path, relaxed)
    policy = MaskingPolicy.load(REPO_ROOT, registries=tmp_path)

    report = MaskerIsolationReport(
        network_egress_denied=True,
        telemetry_disabled=True,
        prompt_logging_disabled=True,
        core_dump_disabled=True,
        temp_files_disallowed=True,
        memory_locked=False,
    )
    assert policy.isolation.unmet(report) == ()

    strict = MaskingPolicy.load(REPO_ROOT)
    assert strict.isolation.unmet(report) == ("memory_locked",)
