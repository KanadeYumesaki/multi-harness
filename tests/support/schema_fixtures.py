"""JSON Schema から**決定論的に**最小の有効Instanceを組み立てる。

## なぜ手書きFixtureにしないか

`AT-SCHEMA-COMPLETE-001/ALL_VALID` は登録済みCore Schema全件の有効Fixtureを
要求する。22 論理Schema・27 Version を手で書くと、次が起きる。

* Schemaが増えたのにFixtureを足し忘れる。件数の手入力が正本から離れる。
* Schemaの`required`が変わってもFixtureが古いまま通り続ける。

**Schemaから導出すれば、Schemaが変わればFixtureも変わる。**
件数もFieldもコードへ書かない（不変条件#18）。

## 決定論である

同じSchemaから必ず同じInstanceを作る。乱数も時刻も使わない。
再現しないFixtureのHashはEvidenceにならない（不変条件#4）。

値は型と制約から決める。`pattern` は既知の代表形（`sha256:` Hash、
RFC 3339 時刻）へ当て、それ以外は制約を満たす固定文字列を返す。
**当てられないpatternは例外で止める。** 適当な値でSchemaを通すと、
「検証した」という記録だけが残る。
"""

from __future__ import annotations

import re
from typing import Any, Final

__all__ = ["FixtureBuildError", "build_valid_instance"]

_HASH_PATTERN: Final[str] = "^sha256:[0-9a-f]{64}$"
_TIMESTAMP_PATTERN: Final[str] = "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"

_SAMPLE_HASH: Final[str] = "sha256:" + "a" * 64
_SAMPLE_TIMESTAMP: Final[str] = "2026-08-17T00:00:00Z"
_SAMPLE_UUID: Final[str] = "018f0000-0000-7000-8000-000000000001"


class FixtureBuildError(RuntimeError):
    """Schemaから有効Instanceを導出できない。**適当な値で埋めない。**"""


def _string_for(node: dict[str, Any], field: str) -> str:
    if "const" in node:
        return str(node["const"])
    if "enum" in node:
        # 決定論のため常に先頭を採る。並びはSchema生成器が固定している。
        return str(node["enum"][0])
    pattern = node.get("pattern")
    if pattern == _HASH_PATTERN:
        return _SAMPLE_HASH
    if pattern == _TIMESTAMP_PATTERN:
        return _SAMPLE_TIMESTAMP
    if pattern is not None:
        raise FixtureBuildError(
            f"{field}: 未知のpattern {pattern!r} に当てられる代表値が無い。"
            "適当な値で埋めるとSchemaを通っただけの記録が残る"
        )
    if node.get("format") == "date-time":
        return _SAMPLE_TIMESTAMP
    if field.endswith("_id") or field == "record_id":
        return _SAMPLE_UUID
    minimum = int(node.get("minLength", 1) or 1)
    return "x" * max(minimum, 1)


def _value_for(node: dict[str, Any], field: str) -> Any:
    """1 Field分の値を作る。型が判定できなければ止める。"""
    if "const" in node:
        return node["const"]
    if "enum" in node:
        return node["enum"][0]

    declared = node.get("type")
    if isinstance(declared, list):
        # `["string", "null"]` のような Union。null 以外を採る。
        candidates = [t for t in declared if t != "null"]
        declared = candidates[0] if candidates else "null"

    if declared == "string":
        return _string_for(node, field)
    if declared == "integer":
        return int(node.get("minimum", 0) or 0)
    if declared == "number":
        return float(node.get("minimum", 0) or 0)
    if declared == "boolean":
        return False
    if declared == "null":
        return None
    if declared == "array":
        minimum = int(node.get("minItems", 0) or 0)
        if minimum == 0:
            return []
        item = node.get("items", {})
        return [_value_for(item, f"{field}[]") for _ in range(minimum)]
    if declared == "object":
        return build_valid_instance(node, path=field)

    raise FixtureBuildError(f"{field}: type を判定できない（{node!r}）")


def build_valid_instance(schema: dict[str, Any], *, path: str = "$") -> dict[str, Any]:
    """`required` を満たす最小の有効Instanceを返す。

    Optional Fieldは入れない。**最小のものが通ることを確かめる**のが目的であり、
    値を盛ると何が効いているのか分からなくなる。
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        # `properties` を持たない自由形式Object（例：Policy `predicate`）。
        # `required` があるのに `properties` が無いのは定義の矛盾なので止める。
        if schema.get("required"):
            raise FixtureBuildError(f"{path}: required があるのに properties が無い（定義の矛盾）")
        return {}

    instance: dict[str, Any] = {}
    for field in schema.get("required", []):
        node = properties.get(field)
        if not isinstance(node, dict):
            raise FixtureBuildError(f"{path}.{field}: required だが properties に定義が無い")
        instance[field] = _value_for(node, field)
    return instance


def pattern_is_known(pattern: str) -> bool:
    """代表値を当てられるpatternか。"""
    return pattern in (_HASH_PATTERN, _TIMESTAMP_PATTERN)


def invalid_hash_value(pattern: str) -> str:
    """Hash Patternに合わない値。`INVALID_HASH` 系のCaseで使う。"""
    if not re.fullmatch(r"\^sha256:.*", pattern):
        raise FixtureBuildError(f"Hash pattern ではない: {pattern!r}")
    return "sha256:NOT-A-VALID-HEX"
