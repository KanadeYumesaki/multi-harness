"""Masker出力検証の試験。信用境界の外から来たJSONを扱う。

受入Case `MASKER_OUTPUT_MALFORMED` / `NORMALIZATION_PROFILE_MISMATCH`
（ADR-007 §11）に対応する。
"""

from __future__ import annotations

import json

import pytest

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.masking import Span
from harness.infrastructure.masking.masker_output import parse_masker_output

pytestmark = pytest.mark.unit

HASH = "sha256:" + "a" * 64
PROFILE = "NFC_CODEPOINT_V3"
ARTIFACT = "sha256:" + "e" * 64

EXPECTED = {
    "expected_hash": HASH,
    "expected_profile": PROFILE,
    "expected_profile_artifact_hash": ARTIFACT,
}


def _payload(
    spans: list[dict[str, object]],
    *,
    hash_value: str = HASH,
    profile: str = PROFILE,
    artifact: str = ARTIFACT,
) -> str:
    return json.dumps(
        {
            "source_normalized_hash": hash_value,
            "normalization_profile": profile,
            "normalization_profile_artifact_hash": artifact,
            "spans": spans,
        }
    )


# ---------------------------------------------------------------------------
# ADR-007 §2 の4項目契約
# ---------------------------------------------------------------------------


def test_spec_conformant_output_is_accepted() -> None:
    """ADR-007 §2 どおりの4項目が通る。

    初版はProfile系2項目を「未知のKey」として拒否しており、仕様準拠Maskerが
    弾かれていた（レビュー BLOCKER 2）。この試験がその再発を止める。
    """
    raw = _payload([{"start": 3, "end": 9, "category": "EMAIL"}])
    proposal = parse_masker_output(raw, **EXPECTED)
    assert proposal.spans == (Span(3, 9, "EMAIL"),)
    assert proposal.normalization_profile == PROFILE
    assert proposal.normalization_profile_artifact_hash == ARTIFACT


def test_empty_span_list_is_valid() -> None:
    """「マスク不要」という提案は正当な応答である。"""
    proposal = parse_masker_output(_payload([]), **EXPECTED)
    assert proposal.spans == ()


@pytest.mark.parametrize(
    "missing_key",
    [
        "source_normalized_hash",
        "normalization_profile",
        "normalization_profile_artifact_hash",
        "spans",
    ],
)
def test_any_missing_root_key_is_rejected(missing_key: str) -> None:
    """4項目に過不足があれば拒否する。Profile系を省略した応答も通さない。"""
    document = json.loads(_payload([]))
    del document[missing_key]
    with pytest.raises(HarnessError) as error:
        parse_masker_output(json.dumps(document), **EXPECTED)
    assert error.value.code is ErrorCode.MASKER_OUTPUT_MALFORMED
    assert missing_key in str(error.value)


# ---------------------------------------------------------------------------
# Echo Back の照合
# ---------------------------------------------------------------------------


def test_hash_echo_mismatch_is_rejected() -> None:
    """`on_hash_mismatch: REJECT`。別の本文への応答を適用しない。"""
    raw = _payload([], hash_value="sha256:" + "b" * 64)
    with pytest.raises(HarnessError) as error:
        parse_masker_output(raw, **EXPECTED)
    assert error.value.code is ErrorCode.MASKER_OUTPUT_MALFORMED


def test_profile_echo_mismatch_is_a_profile_error_not_malformed() -> None:
    """別のProfileで座標を数えた応答を弾く。

    Codeを`MASKER_OUTPUT_MALFORMED`と分けるのは、運用上の対処が違うためである。
    壊れた出力はMasker実装の不具合、Profile不一致は配備の不整合である。
    """
    raw = _payload([], profile="NFKC_CODEPOINT_V1")
    with pytest.raises(HarnessError) as error:
        parse_masker_output(raw, **EXPECTED)
    assert error.value.code is ErrorCode.MASKING_NORMALIZATION_PROFILE_MISMATCH


def test_profile_artifact_echo_mismatch_is_rejected() -> None:
    """Profile名が同じでもArtifactが違えば割当済み集合が違う。"""
    raw = _payload([], artifact="sha256:" + "f" * 64)
    with pytest.raises(HarnessError) as error:
        parse_masker_output(raw, **EXPECTED)
    assert error.value.code is ErrorCode.MASKING_NORMALIZATION_PROFILE_MISMATCH


# ---------------------------------------------------------------------------
# 構造・型
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        "[]",
        '"string"',
        "42",
        "null",
        '{"source_normalized_hash": "x", "normalization_profile": "p",'
        ' "normalization_profile_artifact_hash": "a", "spans": [], "extra": 1}',
        '{"source_normalized_hash": 1, "normalization_profile": "p",'
        ' "normalization_profile_artifact_hash": "a", "spans": []}',
        '{"source_normalized_hash": "x", "normalization_profile": "p",'
        ' "normalization_profile_artifact_hash": "a", "spans": {}}',
    ],
    ids=[
        "empty",
        "not_json",
        "root_array",
        "root_string",
        "root_number",
        "root_null",
        "unknown_key",
        "hash_not_string",
        "spans_not_array",
    ],
)
def test_malformed_root_is_rejected(raw: str) -> None:
    with pytest.raises(HarnessError) as error:
        parse_masker_output(
            raw,
            expected_hash="x",
            expected_profile="p",
            expected_profile_artifact_hash="a",
        )
    assert error.value.code is ErrorCode.MASKER_OUTPUT_MALFORMED


@pytest.mark.parametrize(
    "span",
    [
        {"start": 1, "end": 5},
        {"start": 1, "end": 5, "category": "EMAIL", "note": "why"},
        {"start": "1", "end": 5, "category": "EMAIL"},
        {"start": 1.5, "end": 5, "category": "EMAIL"},
        {"start": True, "end": 5, "category": "EMAIL"},
        {"start": 1, "end": 5, "category": ""},
        {"start": 1, "end": 5, "category": 7},
        {"start": -1, "end": 5, "category": "EMAIL"},
        {"start": 5, "end": 5, "category": "EMAIL"},
        {"start": 9, "end": 5, "category": "EMAIL"},
    ],
    ids=[
        "missing_category",
        "unknown_key",
        "start_as_string",
        "start_as_float",
        "start_as_bool",
        "empty_category",
        "category_as_int",
        "negative_start",
        "zero_length",
        "reversed",
    ],
)
def test_malformed_span_is_rejected(span: dict[str, object]) -> None:
    with pytest.raises(HarnessError) as error:
        parse_masker_output(_payload([span]), **EXPECTED)
    assert error.value.code is ErrorCode.MASKER_OUTPUT_MALFORMED


def test_boolean_is_not_accepted_as_a_coordinate() -> None:
    """`bool`は`int`の派生である。`True`を座標1として通さない。"""
    with pytest.raises(HarnessError, match="not integer"):
        parse_masker_output(_payload([{"start": True, "end": 5, "category": "EMAIL"}]), **EXPECTED)


# ---------------------------------------------------------------------------
# 値を漏らさない（不変条件#7）
# ---------------------------------------------------------------------------


def test_error_messages_do_not_echo_the_masker_output() -> None:
    """Masker出力にはPIIが載り得る。Error Messageへ転記しない。"""
    secret = "alice@example.com"
    raw = _payload([], hash_value=secret)
    with pytest.raises(HarnessError) as error:
        parse_masker_output(raw, **EXPECTED)
    assert secret not in str(error.value)


def test_profile_artifact_mismatch_message_does_not_leak_the_hash() -> None:
    """Artifact Hash自体は秘密ではないが、Message構築の癖を固定する。"""
    raw = _payload([], artifact="sha256:" + "9" * 64)
    with pytest.raises(HarnessError) as error:
        parse_masker_output(raw, **EXPECTED)
    assert "9" * 64 not in str(error.value)


def test_json_syntax_error_message_carries_no_input_fragment() -> None:
    raw = '{"source_normalized_hash": "alice@example.com", "spans": [},}'
    with pytest.raises(HarnessError) as error:
        parse_masker_output(raw, **EXPECTED)
    assert "alice@example.com" not in str(error.value)
    assert "line" in str(error.value)


def test_absurd_span_count_is_rejected_before_span_parsing() -> None:
    """max_spans検査より前に効く安全弁。件数自体が異常な応答を早く切る。"""
    spans = [{"start": 0, "end": 1, "category": "EMAIL"}] * 100_001
    with pytest.raises(HarnessError, match="entries"):
        parse_masker_output(_payload(spans), **EXPECTED)
