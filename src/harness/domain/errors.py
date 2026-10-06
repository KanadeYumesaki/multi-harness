"""§1.7／§1.7.1 Error分類とError Code Registry。

Error Codeとその Classification の正本は `design-source/registries/errors.yaml`。
`ErrorCode` / `ErrorClassification` / `ERROR_CLASSIFICATION` は生成物の再輸出である。

§1.7.1「ClassificationとCodeを混同しない」に従い、両者を別型として扱う。

このため `StrEnum` ではなく素の `Enum` を使う。`StrEnum` にすると両者が `str` の
派生となり、名称が重なる `EFFECT_UNKNOWN` について
`ErrorCode.EFFECT_UNKNOWN == ErrorClassification.EFFECT_UNKNOWN` が真になってしまい、
仕様が禁じる混同が静かに成立する。素の `Enum` なら偽になり、
`mypy --strict`（`--strict-equality`）が非重複比較として静的にも検出する。

副作用として、Canonical化・永続化の境界では `.value` を明示する必要がある。
`canonical.py` はEnumを未知型として拒否するため、書き忘れは実行時にも停止する。
"""

from __future__ import annotations

from harness.domain._registry_generated import (
    ERROR_CLASSIFICATION,
    ErrorClassification,
    ErrorCode,
)

__all__ = [
    "ERROR_CLASSIFICATION",
    "ERROR_CODE_COUNT",
    "ErrorClassification",
    "ErrorCode",
    "HarnessError",
    "classification_of",
]

ERROR_CODE_COUNT: int = len(ErrorCode)


def classification_of(code: ErrorCode) -> ErrorClassification:
    """Error CodeのClassificationを返す。未登録Codeは存在し得ない。"""
    return ERROR_CLASSIFICATION[code]


class HarnessError(Exception):
    """正規Error Codeを必須とするDomain例外。

    §1.7.1「未知Code、Classification不一致、廃止CodeはSchema検証で拒否する」に
    対応し、Codeを`ErrorCode`型に限定することで未知Codeの発生源を塞ぐ。
    Messageへ値本体・Secretを含めないのは呼出側の責務である（不変条件#7）。
    """

    def __init__(self, code: ErrorCode, message: str = "") -> None:
        self.code = code
        self.classification = classification_of(code)
        super().__init__(f"{code.value}: {message}" if message else code.value)
