"""§1.11 Hash規約とDomain分離の試験。"""

from __future__ import annotations

import hashlib

import pytest

from harness.domain.canonical import canonicalize
from harness.domain.hashing import (
    HASH_PROFILE_VERSION,
    ContentHash,
    InvalidContentHashError,
    domain_separator,
    hash_bytes,
    hash_canonical,
)

pytestmark = pytest.mark.unit


def test_hash_profile_version_is_one() -> None:
    """§1.11「Hash Profile Version：hash_profile_version=1」。"""
    assert HASH_PROFILE_VERSION == 1


def test_domain_separator_format() -> None:
    assert domain_separator("plan-content", 1) == b"FDE-HARNESS/plan-content/1/"
    assert (
        domain_separator("execution-plan-authority", 1)
        == b"FDE-HARNESS/execution-plan-authority/1/"
    )


@pytest.mark.parametrize(
    "artifact_type",
    ["plan-content/x", "Plan-Content", "plan_content", "", "-plan", "plan-", "pl/an"],
)
def test_domain_separator_rejects_ambiguous_artifact_type(artifact_type: str) -> None:
    """`/` を含むartifact_typeを許すとDomainを詐称できるため構文を固定する。"""
    with pytest.raises(ValueError, match="artifact_type must match"):
        domain_separator(artifact_type, 1)


def test_domain_separator_rejects_non_positive_major() -> None:
    with pytest.raises(ValueError, match="schema_major"):
        domain_separator("plan-content", 0)


def test_hash_canonical_matches_specified_formula() -> None:
    """§1.11の計算式どおりであることを独立計算で照合する。

    SHA-256("FDE-HARNESS/plan-content/1/" || RFC8785-JCS(value))
    """
    value = {"b": 1, "a": "x"}
    expected = hashlib.sha256(b"FDE-HARNESS/plan-content/1/" + canonicalize(value)).hexdigest()
    result = hash_canonical(value, artifact_type="plan-content", schema_major=1)
    assert result.hexdigest == expected
    assert str(result) == f"sha256:{expected}"


def test_domain_separation_changes_hash_for_identical_content() -> None:
    """同一Canonical Bytesでも意味が違えばHashが異なる（分離の目的そのもの）。"""
    value = {"a": 1}
    plan = hash_canonical(value, artifact_type="plan-content", schema_major=1)
    authority = hash_canonical(value, artifact_type="execution-plan-authority", schema_major=1)
    assert plan != authority


def test_schema_major_changes_hash() -> None:
    value = {"a": 1}
    v1 = hash_canonical(value, artifact_type="plan-content", schema_major=1)
    v2 = hash_canonical(value, artifact_type="plan-content", schema_major=2)
    assert v1 != v2


def test_hash_is_insertion_order_independent() -> None:
    a = hash_canonical({"x": 1, "y": 2}, artifact_type="plan-content", schema_major=1)
    b = hash_canonical({"y": 2, "x": 1}, artifact_type="plan-content", schema_major=1)
    assert a == b


def test_hash_is_stable_across_calls() -> None:
    value = {"nested": [{"k": "v"}], "n": 1}
    first = hash_canonical(value, artifact_type="plan-content", schema_major=1)
    second = hash_canonical(value, artifact_type="plan-content", schema_major=1)
    assert first == second


def test_hash_bytes_has_no_domain_separation() -> None:
    """Artifact Binary Hashは保存Bytesそのもののhash（§1.11）。"""
    data = b"hello\n"
    assert hash_bytes(data).hexdigest == hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# ContentHash 値オブジェクト
# --------------------------------------------------------------------------


def test_content_hash_parse_roundtrip() -> None:
    text = "sha256:" + "a" * 64
    assert str(ContentHash.parse(text)) == text


@pytest.mark.parametrize(
    "text",
    [
        "a" * 64,  # prefixなし
        "sha256:" + "A" * 64,  # 大文字hex
        "sha256:" + "a" * 63,  # 桁不足
        "sha256:" + "a" * 65,  # 桁超過
        "sha256:" + "g" * 64,  # 非hex
        "sha1:" + "a" * 40,  # 別algorithm
        " sha256:" + "a" * 64,  # 前後空白
    ],
)
def test_content_hash_rejects_malformed(text: str) -> None:
    with pytest.raises(InvalidContentHashError):
        ContentHash.parse(text)


def test_content_hash_of_digest_requires_32_bytes() -> None:
    with pytest.raises(InvalidContentHashError, match="32 bytes"):
        ContentHash.of_digest(b"\x00" * 31)


def test_content_hash_is_frozen_and_hashable() -> None:
    value = ContentHash.parse("sha256:" + "0" * 64)
    assert {value, ContentHash.parse("sha256:" + "0" * 64)} == {value}
    with pytest.raises(AttributeError):
        value.hexdigest = "x" * 64  # type: ignore[misc]
