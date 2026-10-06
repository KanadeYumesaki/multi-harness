"""§1.11 Canonical JSON規約（RFC 8785 JCS）。

本Moduleは設計書§1.11の次の規定を実装する。

* Canonical JSON : RFC 8785 JCS準拠
* 文字コード     : UTF-8
* Unicode        : 入力検証前にNFCへ正規化（Artifact Binary Hashは対象外）
* 数値           : JCS表現。NaN、Infinity、負の0、実装依存Decimalを禁止
* 重複Object Key : Parse時に拒否
* 不正Unicode、未対Surrogate: 拒否

判定不能・規約違反はすべて例外で停止する（不変条件#9「例外を握り潰さない」）。
例外Messageに値本体を含めない。Secret値がCanonicalizer経由でLogへ出ることを防ぐため、
違反箇所はJSON Pointerと型名だけで報告する（不変条件#7）。

本Moduleは純粋関数だけを提供し、Clock・乱数・Filesystem・環境変数へ依存しない。
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any, Final

__all__ = [
    "CanonicalizationError",
    "canonicalize",
    "normalize_nfc",
    "parse_strict_json",
    "serialize_number",
    "to_jcs_bytes",
]

# ECMAScript の安全整数範囲。RFC 8785はIEEE-754 doubleを前提とするため、
# これを超える整数はCanonical表現が実装依存になる。Fail-Closedで拒否する。
_MAX_SAFE_INTEGER: Final[int] = 2**53 - 1
_MIN_SAFE_INTEGER: Final[int] = -(2**53 - 1)

# RFC 8785 §3.2.2.2 が要求する短縮Escape。
_SHORT_ESCAPES: Final[Mapping[int, str]] = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}

_FLOAT_REPR_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<int>\d+)(?:\.(?P<frac>\d+))?(?:[eE](?P<exp>[+-]?\d+))?$"
)


class CanonicalizationError(ValueError):
    """Canonical化できない入力。値本体はMessageへ含めない。"""


def _pointer(path: Sequence[str]) -> str:
    """RFC 6901 JSON Pointer。診断用であり値を含めない。"""
    if not path:
        return ""
    escaped = (segment.replace("~", "~0").replace("/", "~1") for segment in path)
    return "/" + "/".join(escaped)


def normalize_nfc(text: str) -> str:
    """§1.11「入力検証前にNFCへ正規化」。"""
    return unicodedata.normalize("NFC", text)


def _check_wellformed(text: str, path: Sequence[str], role: str) -> None:
    """未対Surrogateを含む文字列を拒否する。

    Pythonのstrは孤立Surrogate（U+D800..U+DFFF）を保持できるが、
    UTF-8としてEncodeできずJCS出力が定義されない。
    """
    for char in text:
        if 0xD800 <= ord(char) <= 0xDFFF:
            raise CanonicalizationError(f"lone surrogate in {role} at {_pointer(path) or '<root>'}")


def serialize_number(value: int | float) -> str:
    """ECMAScript `Number::toString` 相当のCanonical数値表現を返す。

    RFC 8785 §3.2.2.3 は数値のSerializeをECMAScriptのNumber-to-Stringへ委ねる。
    Pythonの`repr(float)`は最短往復表現を返すが指数形式の綴りが異なるため、
    桁列と指数へ分解してECMAScriptの規則で組み直す。
    """
    if isinstance(value, bool):
        # bool は int の派生。JSON上は真偽値であり数値ではない。
        raise CanonicalizationError("bool is not a number")

    if isinstance(value, int):
        if not _MIN_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER:
            raise CanonicalizationError(
                "integer outside IEEE-754 safe range is not canonicalizable"
            )
        return str(value)

    if math.isnan(value) or math.isinf(value):
        raise CanonicalizationError("NaN and Infinity are not permitted")

    if value == 0.0:
        # -0.0 は禁止。math.copysign で符号を判定する（-0.0 == 0.0 のため）。
        if math.copysign(1.0, value) < 0:
            raise CanonicalizationError("negative zero is not permitted")
        return "0"

    if value < 0:
        return "-" + serialize_number(-value)

    digits, point = _decompose_float(value)
    return _format_es_number(digits, point)


def _decompose_float(value: float) -> tuple[str, int]:
    """正の有限floatを (有効桁列, 小数点位置) へ分解する。

    戻り値 ``(digits, point)`` は ``value == 0.digits * 10**point`` を満たす。
    ECMAScript仕様の記法では ``s = digits``、``k = len(digits)``、``n = point``。
    """
    text = repr(value)
    match = _FLOAT_REPR_RE.match(text)
    if match is None:  # pragma: no cover - repr(float) は常にこの形式
        raise CanonicalizationError("unsupported float representation")

    int_part = match.group("int")
    frac_part = match.group("frac") or ""
    exponent = int(match.group("exp") or 0)

    digits = int_part + frac_part
    point = len(int_part) + exponent

    stripped = digits.lstrip("0")
    point -= len(digits) - len(stripped)
    digits = stripped.rstrip("0")

    if not digits:  # pragma: no cover - value == 0.0 は呼出前に処理済み
        raise CanonicalizationError("unsupported float representation")
    return digits, point


def _format_es_number(digits: str, point: int) -> str:
    """ECMAScript Number-to-String の桁組み立て規則。"""
    k = len(digits)
    if k <= point <= 21:
        return digits + "0" * (point - k)
    if 0 < point <= 21:
        return digits[:point] + "." + digits[point:]
    if -6 < point <= 0:
        return "0." + "0" * (-point) + digits

    exponent = point - 1
    sign = "+" if exponent >= 0 else "-"
    mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
    return f"{mantissa}e{sign}{abs(exponent)}"


def _serialize_string(text: str) -> str:
    """RFC 8785 §3.2.2.2 の文字列Escape。"""
    out = ['"']
    for char in text:
        code = ord(char)
        short = _SHORT_ESCAPES.get(code)
        if short is not None:
            out.append(short)
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _sort_key(key: str) -> bytes:
    """RFC 8785 のObject Key整列順（UTF-16 code unit列の辞書順）。

    UTF-16BE Bytes列の辞書順はUTF-16 code unit列の辞書順と一致する。
    Code Point順とは非BMP文字の扱いで異なるため、`sorted(key)` では代用しない。
    """
    return key.encode("utf-16-be")


def _serialize(value: Any, path: list[str]) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        _check_wellformed(value, path, "string")
        return _serialize_string(value)
    if isinstance(value, int | float):
        try:
            return serialize_number(value)
        except CanonicalizationError as exc:
            raise CanonicalizationError(f"{exc} at {_pointer(path) or '<root>'}") from exc
    if isinstance(value, Mapping):
        return _serialize_object(value, path)
    if isinstance(value, list | tuple):
        return _serialize_array(value, path)

    raise CanonicalizationError(
        f"unsupported type {type(value).__name__} at {_pointer(path) or '<root>'}"
    )


def _serialize_object(value: Mapping[Any, Any], path: list[str]) -> str:
    keys: list[str] = []
    for key in value:
        if not isinstance(key, str):
            raise CanonicalizationError(
                f"non-string object key of type {type(key).__name__} "
                f"at {_pointer(path) or '<root>'}"
            )
        _check_wellformed(key, path, "object key")
        keys.append(key)

    # NFC正規化後に衝突するKeyはCanonical表現が一意にならない。
    # 呼出側が normalize=True を使うか否かに関わらず、ここで検出して停止する。
    normalized_seen: dict[str, str] = {}
    for key in keys:
        normalized = normalize_nfc(key)
        if normalized in normalized_seen:
            raise CanonicalizationError(
                f"object keys collide under NFC normalization at {_pointer(path) or '<root>'}"
            )
        normalized_seen[normalized] = key

    keys.sort(key=_sort_key)
    members = []
    for key in keys:
        path.append(key)
        members.append(f"{_serialize_string(key)}:{_serialize(value[key], path)}")
        path.pop()
    return "{" + ",".join(members) + "}"


def _serialize_array(value: Sequence[Any], path: list[str]) -> str:
    elements = []
    for index, element in enumerate(value):
        path.append(str(index))
        elements.append(_serialize(element, path))
        path.pop()
    return "[" + ",".join(elements) + "]"


def to_jcs_bytes(value: Any) -> bytes:
    """RFC 8785 JCS のみを適用する。Unicode正規化は行わない。

    Artifact Binary Hashのように原Bytesを保つ必要がある文脈、および
    RFC 8785そのものへの適合を試験する文脈で使う。
    設計書§1.11が要求するHash入力は :func:`canonicalize` を使うこと。
    """
    return _serialize(value, []).encode("utf-8")


def canonicalize(value: Any) -> bytes:
    """§1.11のCanonical Bytesを返す。NFC正規化 → JCSの順に適用する。

    Plan、Approval、Policy、Receipt、Schema SetのHash入力はこの関数を通す。
    """
    return to_jcs_bytes(_normalize_tree(value, []))


def _normalize_tree(value: Any, path: list[str]) -> Any:
    """文字列（Key・値の双方）をNFCへ正規化した等価構造を返す。"""
    if isinstance(value, str):
        _check_wellformed(value, path, "string")
        return normalize_nfc(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(
                    f"non-string object key of type {type(key).__name__} "
                    f"at {_pointer(path) or '<root>'}"
                )
            _check_wellformed(key, path, "object key")
            normalized_key = normalize_nfc(key)
            if normalized_key in result:
                raise CanonicalizationError(
                    f"object keys collide under NFC normalization at {_pointer(path) or '<root>'}"
                )
            path.append(key)
            result[normalized_key] = _normalize_tree(item, path)
            path.pop()
        return result
    if isinstance(value, list | tuple):
        out: list[Any] = []
        for index, element in enumerate(value):
            path.append(str(index))
            out.append(_normalize_tree(element, path))
            path.pop()
        return out
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalizationError("duplicate object key in JSON input")
        result[key] = value
    return result


def parse_strict_json(text: str | bytes) -> Any:
    """§1.11「重複Object Key：Parse時に拒否」を満たすJSON Parse。

    NaN／Infinity／-Infinity のJSON拡張literalも拒否する。
    """
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CanonicalizationError("input is not valid UTF-8") from exc

    def _reject_constant(name: str) -> Any:
        raise CanonicalizationError(f"{name} literal is not permitted")

    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise CanonicalizationError(f"invalid JSON at line {exc.lineno}") from exc
