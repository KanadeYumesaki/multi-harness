"""§1.11 Canonical JSON（RFC 8785 JCS）の適合試験。

RFC 8785 Appendix B の公式Test Vector、ECMAScript Number-to-Stringの境界値、
および設計書§1.11が追加で要求する拒否条件（NaN／Infinity／負の0／重複Key／
未対Surrogate）を検証する。

制御文字と合成／分解Unicodeは、Source Fileの編集経路で静かに壊れると試験の意味が
失われる。そのため本Fileでは生の制御文字を一切書かず、すべて `chr()` と `_u()` から
組み立てる。
"""

from __future__ import annotations

import json

import pytest

from harness.domain.canonical import (
    CanonicalizationError,
    canonicalize,
    normalize_nfc,
    parse_strict_json,
    serialize_number,
    to_jcs_bytes,
)

pytestmark = pytest.mark.unit

# 合成済み「é」(U+00E9) と 分解「é」(U+0065 U+0301)
CAFE_COMPOSED = "caf" + chr(0x00E9)
CAFE_DECOMPOSED = "cafe" + chr(0x0301)

_BS = chr(0x5C)  # REVERSE SOLIDUS
_QUOTE = chr(0x22)


def _u(codepoint: int) -> str:
    """JSON Text上の `\\uXXXX` Escape列（6文字）を返す。実文字は返さない。"""
    return _BS + "u" + format(codepoint, "04x")


# --------------------------------------------------------------------------
# RFC 8785 Appendix B: 公式Test Vector
# --------------------------------------------------------------------------


def test_rfc8785_appendix_b_full_vector() -> None:
    """RFC 8785 Appendix B の入力→出力をBytes一致で検証する。"""
    # Appendix B の "string" メンバのJSON Text（Escapeを保った生の並び）。
    json_string_body = (
        _u(0x20AC)
        + "$"
        + _u(0x000F)
        + _u(0x000A)
        + "A'"
        + _u(0x0042)
        + _u(0x0022)
        + _u(0x005C)
        + _BS
        + _BS
        + _BS
        + _QUOTE
        + _BS
        + "/"
    )
    source = (
        "{"
        '"numbers": [333333333.33333329, 1E30, 4.50, 2e-3,'
        " 0.000000000000000000000000001],"
        f'"string": "{json_string_body}",'
        '"literals": [null, true, false]'
        "}"
    )
    value = parse_strict_json(source)

    # 復号後の実体は12文字: € $ U+000F LF A ' B " \ \ " /
    assert value["string"] == (
        chr(0x20AC)
        + "$"
        + chr(0x000F)
        + chr(0x000A)
        + "A'B"
        + chr(0x0022)
        + chr(0x005C)
        + chr(0x005C)
        + chr(0x0022)
        + "/"
    )

    # 期待されるCanonical出力:
    #   € と $ と / はそのまま、U+000F は 、LF は \n、
    #   " は \" 、\ は \\ （2個の \ は \\\\ になる）。
    expected_string = (
        _QUOTE
        + chr(0x20AC)
        + "$"
        + _u(0x000F)
        + _BS
        + "n"
        + "A'B"
        + _BS
        + _QUOTE
        + _BS
        + _BS
        + _BS
        + _BS
        + _BS
        + _QUOTE
        + "/"
        + _QUOTE
    )
    expected = (
        '{"literals":[null,true,false],'
        '"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
        f'"string":{expected_string}}}'
    )
    assert to_jcs_bytes(value).decode("utf-8") == expected


def test_rfc8785_key_ordering_is_utf16_not_codepoint() -> None:
    """Object Keyの整列はUTF-16 code unit順である。

    U+1F602（😂）は非BMPでUTF-16では D83D DE02。先頭code unit 0xD83D は
    U+FB33（דּ）より小さいため、😂 は דּ より前に並ぶ。
    Code Point順（U+1F602 > U+FB33）とは逆になるため、この2Keyの相対順序が
    実装の整列基準を一意に判別する。
    """
    source = (
        "{"
        f'"{_u(0x20AC)}": "Euro Sign",'
        f'"{_u(0x000D)}": "Carriage Return",'
        f'"{_u(0x000A)}": "Newline",'
        '"1": "One",'
        f'"{_u(0x0080)}": "Control{_u(0x007F)}",'
        f'"{_u(0xD83D)}{_u(0xDE02)}": "Smiley",'
        f'"{_u(0x00F6)}": "Latin Small Letter O With Diaeresis",'
        f'"{_u(0xFB33)}": "Hebrew Letter Dalet With Dagesh",'
        '"</script>": "Browser Challenge"'
        "}"
    )
    value = parse_strict_json(source)
    canonical = to_jcs_bytes(value).decode("utf-8")

    assert list(json.loads(canonical)) == [
        chr(0x000A),
        chr(0x000D),
        "1",
        "</script>",
        chr(0x0080),
        chr(0x00F6),
        chr(0x20AC),
        chr(0x1F602),
        chr(0xFB33),
    ]
    # 非BMPがU+FB33より前に来ることを明示的に固定する（Code Point順なら逆）。
    assert canonical.index(chr(0x1F602)) < canonical.index(chr(0xFB33))


def test_non_ascii_and_c1_controls_are_serialized_literally() -> None:
    """RFC 8785 §3.2.2.2: Escapeが必要なのは U+0000..U+001F と `"` `\\` だけ。

    U+007F（DEL）とU+0080（C1制御）はC0範囲外であり、UTF-8のまま出力する。
    RFC Appendix Bの表示上のEscapeは文書可読性のためであり正規化規則ではない。
    """
    canonical = to_jcs_bytes({"k": chr(0x007F) + chr(0x0080) + chr(0x00E9)})
    text = canonical.decode("utf-8")
    assert text == '{"k":"' + chr(0x007F) + chr(0x0080) + chr(0x00E9) + '"}'
    assert _BS + "u" not in text


def test_c0_controls_use_short_escapes_then_lowercase_hex() -> None:
    raw = chr(0x08) + chr(0x09) + chr(0x0A) + chr(0x0C) + chr(0x0D) + chr(0x00) + chr(0x1F)
    expected = (
        '{"k":"'
        + _BS
        + "b"
        + _BS
        + "t"
        + _BS
        + "n"
        + _BS
        + "f"
        + _BS
        + "r"
        + _u(0x0000)
        + _u(0x001F)
        + '"}'
    )
    assert to_jcs_bytes({"k": raw}).decode("utf-8") == expected


def test_quote_and_backslash_are_escaped() -> None:
    raw = "a" + _QUOTE + "b" + _BS + "c"
    expected = _QUOTE + "a" + _BS + _QUOTE + "b" + _BS + _BS + "c" + _QUOTE
    assert to_jcs_bytes(raw).decode("utf-8") == expected


# --------------------------------------------------------------------------
# ECMAScript Number-to-String
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, "0"),
        (1.0, "1"),
        (-1.0, "-1"),
        (4.50, "4.5"),
        (2e-3, "0.002"),
        (1e30, "1e+30"),
        (1e-27, "1e-27"),
        (333333333.33333329, "333333333.3333333"),
        # 指数表記への切替境界
        (1e20, "100000000000000000000"),
        (1e21, "1e+21"),
        (1e-6, "0.000001"),
        (1e-7, "1e-7"),
        # IEEE-754 の両端
        (5e-324, "5e-324"),
        (1.7976931348623157e308, "1.7976931348623157e+308"),
        (0.1, "0.1"),
        (-0.5, "-0.5"),
    ],
)
def test_number_serialization_matches_ecmascript(value: float, expected: str) -> None:
    assert serialize_number(value) == expected


@pytest.mark.parametrize("value", [0, 1, -1, 9007199254740991, -9007199254740991])
def test_safe_integers_serialize_as_decimal(value: int) -> None:
    assert serialize_number(value) == str(value)


@pytest.mark.parametrize("value", [2**53, -(2**53), 10**30])
def test_unsafe_integers_are_rejected(value: int) -> None:
    """IEEE-754で正確に表せない整数はCanonical表現が実装依存になるため拒否する。"""
    with pytest.raises(CanonicalizationError, match="safe range"):
        serialize_number(value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nan_and_infinity_are_rejected(value: float) -> None:
    with pytest.raises(CanonicalizationError, match="NaN and Infinity"):
        serialize_number(value)


def test_negative_zero_is_rejected() -> None:
    """§1.11「負の0を禁止」。-0.0 == 0.0 のため符号で判定する。"""
    with pytest.raises(CanonicalizationError, match="negative zero"):
        serialize_number(-0.0)
    with pytest.raises(CanonicalizationError, match="negative zero"):
        to_jcs_bytes({"k": -0.0})


def test_bool_is_not_a_number() -> None:
    """Pythonの bool は int の派生。JSON上は真偽値であり数値ではない。"""
    assert to_jcs_bytes([True, False]) == b"[true,false]"
    with pytest.raises(CanonicalizationError, match="bool is not a number"):
        serialize_number(True)


# --------------------------------------------------------------------------
# 拒否条件
# --------------------------------------------------------------------------


def test_duplicate_object_keys_rejected_at_parse() -> None:
    """§1.11「重複Object Key：Parse時に拒否」。"""
    with pytest.raises(CanonicalizationError, match="duplicate object key"):
        parse_strict_json('{"a":1,"a":2}')


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_json_nan_literals_rejected_at_parse(literal: str) -> None:
    with pytest.raises(CanonicalizationError):
        parse_strict_json('{"k":' + literal + "}")


def test_lone_surrogate_rejected() -> None:
    """§1.11「不正Unicode、未対Unicode Surrogate：拒否」。"""
    with pytest.raises(CanonicalizationError, match="lone surrogate"):
        to_jcs_bytes({"k": chr(0xD800)})
    with pytest.raises(CanonicalizationError, match="lone surrogate"):
        to_jcs_bytes({chr(0xDFFF): "v"})
    with pytest.raises(CanonicalizationError, match="lone surrogate"):
        canonicalize({"k": chr(0xD800)})


def test_non_string_object_key_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="non-string object key"):
        to_jcs_bytes({1: "v"})
    with pytest.raises(CanonicalizationError, match="non-string object key"):
        canonicalize({1: "v"})


def test_unsupported_type_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="unsupported type"):
        to_jcs_bytes({"k": {1, 2}})


def test_invalid_utf8_bytes_rejected_at_parse() -> None:
    with pytest.raises(CanonicalizationError, match="not valid UTF-8"):
        parse_strict_json(b'{"k":"\xff"}')


def test_error_message_does_not_leak_values() -> None:
    """不変条件#7: 例外MessageへSecret値を出さない。位置と型だけ報告する。"""
    canary = "SUPER-SECRET-CANARY-VALUE"
    with pytest.raises(CanonicalizationError) as excinfo:
        to_jcs_bytes({"outer": {"inner": [1, {"leaf": {canary}}]}})
    assert canary not in str(excinfo.value)
    assert "/outer/inner/1/leaf" in str(excinfo.value)


# --------------------------------------------------------------------------
# NFC正規化（§1.11「入力検証前にNFCへ正規化」）
# --------------------------------------------------------------------------


def test_canonicalize_applies_nfc_to_values() -> None:
    assert canonicalize({"k": CAFE_COMPOSED}) == canonicalize({"k": CAFE_DECOMPOSED})


def test_canonicalize_applies_nfc_to_keys() -> None:
    assert canonicalize({CAFE_COMPOSED: 1}) == canonicalize({CAFE_DECOMPOSED: 1})


def test_to_jcs_bytes_does_not_normalize() -> None:
    """純RFC 8785はUnicode正規化しない。原Bytes保持文脈のための素の基本関数。"""
    assert to_jcs_bytes({"k": CAFE_COMPOSED}) != to_jcs_bytes({"k": CAFE_DECOMPOSED})


def test_keys_colliding_under_nfc_are_rejected() -> None:
    """正規化後に衝突するKeyはCanonical表現が一意にならないため停止する。"""
    colliding = {CAFE_COMPOSED: 1, CAFE_DECOMPOSED: 2}
    assert len(colliding) == 2
    with pytest.raises(CanonicalizationError, match="collide under NFC"):
        canonicalize(colliding)
    with pytest.raises(CanonicalizationError, match="collide under NFC"):
        to_jcs_bytes(colliding)


def test_normalize_nfc_is_idempotent() -> None:
    once = normalize_nfc(CAFE_DECOMPOSED)
    assert normalize_nfc(once) == once == CAFE_COMPOSED


# --------------------------------------------------------------------------
# 決定性
# --------------------------------------------------------------------------


def test_canonicalization_is_insertion_order_independent() -> None:
    """Dictの挿入順はCanonical Bytesへ影響しない（Plan決定性の前提）。"""
    a = {"b": 1, "a": 2, "c": [{"z": 1, "y": 2}]}
    b = {"c": [{"y": 2, "z": 1}], "a": 2, "b": 1}
    assert canonicalize(a) == canonicalize(b)


def test_tuple_and_list_are_equivalent() -> None:
    """`tuple`はPlan Content Projectionで多用されるためlistと同値に扱う。"""
    assert canonicalize({"k": (1, 2, 3)}) == canonicalize({"k": [1, 2, 3]})


def test_array_order_is_significant() -> None:
    assert canonicalize([1, 2]) != canonicalize([2, 1])


def test_nested_structure_roundtrips_through_strict_parse() -> None:
    value = {"a": [1, {"b": "x"}], "c": None, "d": True}
    canonical = canonicalize(value)
    assert canonicalize(parse_strict_json(canonical)) == canonical
