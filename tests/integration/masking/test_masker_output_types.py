"""Masker出力の型混乱（ADR-007 §2、`MASKER_OUTPUT_MALFORMED`）。

Maskerは信頼境界の外側にある。JSONとして妥当でも、**型が想定と違う**
出力は作れる。文字列を期待している箇所に数値、Objectを期待している箇所に
配列。動的型の言語では、そのまま先へ流れて別の場所で落ちる。

落ちた場所と原因が離れていると、Fail-Closedかどうかも判断できなくなる。
Adapterの入口で型を確かめ、`MASKER_OUTPUT_MALFORMED` に寄せる。
"""

from __future__ import annotations

import json

import pytest

from harness.domain.errors import ErrorCode, HarnessError
from harness.infrastructure.masking.masker_output import parse_masker_output

pytestmark = pytest.mark.unit

VALID = {
    "source_normalized_hash": "sha256:" + "a" * 64,
    "normalization_profile": "NFC_CODEPOINT_V3",
    "normalization_profile_artifact_hash": "sha256:" + "b" * 64,
    "spans": [],
}


def _expect_malformed(raw: object) -> HarnessError:
    with pytest.raises(HarnessError) as error:
        parse_masker_output(
            raw,  # type: ignore[arg-type]
            expected_hash=VALID["source_normalized_hash"],
            expected_profile=VALID["normalization_profile"],
            expected_profile_artifact_hash=VALID["normalization_profile_artifact_hash"],
        )
    assert error.value.code is ErrorCode.MASKER_OUTPUT_MALFORMED
    return error.value


@pytest.mark.parametrize("raw", [None, 42, 3.5, b"{}", ["spans"], {"spans": []}, True])
def test_non_string_output_is_malformed(raw: object) -> None:
    """出力が文字列でない。

    Adapterの契約は「標準出力をそのまま返す」であり、渡ってくるのは
    常に `str` のはずである。だが別のMasker実装を差し込んだときに
    ここが崩れる。`json.loads` へ渡す前に型で止める。
    """
    error = _expect_malformed(raw)
    assert "not a string" in str(error)


def test_valid_output_is_accepted() -> None:
    """壊していない出力が落ちるなら、上の試験は何も証明しない。"""
    result = parse_masker_output(
        json.dumps(VALID),
        expected_hash=VALID["source_normalized_hash"],
        expected_profile=VALID["normalization_profile"],
        expected_profile_artifact_hash=VALID["normalization_profile_artifact_hash"],
    )
    assert result.spans == ()
    assert result.source_normalized_hash == VALID["source_normalized_hash"]


@pytest.mark.parametrize("entry", ["not-an-object", 1, None, [0, 4, "PERSON_NAME"], True])
def test_span_entry_that_is_not_an_object_is_malformed(entry: object) -> None:
    """Span要素がObjectでない。

    配列で `[0, 4, "PERSON_NAME"]` のように渡す実装は素直に見えるが、
    座標の順序が合っているかを型で確かめられない。Objectだけを受ける。
    """
    payload = {**VALID, "spans": [entry]}
    error = _expect_malformed(json.dumps(payload))
    assert "spans[0]" in str(error)


def test_malformed_output_does_not_echo_the_masker_payload() -> None:
    """不変条件#7。Maskerが返した文字列をErrorへ載せない。

    Maskerは原文を持っている。出力が壊れているとき、その出力には
    原文の断片が入りうる。「壊れた入力をそのまま見せる」のが
    診断に便利なのは分かるが、それを載せた時点で漏洩経路になる。
    """
    secret = "Zq7Xw2Nv9Kb4Ld6P-CANARY"
    payload = {**VALID, "spans": [f"leaked {secret}"]}
    error = _expect_malformed(json.dumps(payload))
    assert secret not in str(error)
