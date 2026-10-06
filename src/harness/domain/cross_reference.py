"""§15.2 Cross Reference 検証。Schema検証とは**別の層**である。

## なぜ分けるか

`EffectReceipt` は `operation_journal_id` を持つ。Schema検証はその値が
「文字列であること」までしか言えない。**その Journal が実在するか**は
Schemaの守備範囲ではない。

分けないと、参照先の無いReceiptがSchema検証を通り、
「検証済み」という記録だけが残る。Journalの無いReceiptは、
どの作用に対する受領証なのか確定しない。

| 層 | 判定 | Subject |
|---|---|---|
| Schema検証 | 形式・型・Enum・Pattern | `SCHEMA_VALIDATION_RESULT` |
| Cross Reference検証 | 参照先の実在 | `SCHEMA_VALIDATION_RESULT`（別Case） |

**Schema検証がACCEPTEDでも、Cross ReferenceがREJECTEDなら不合格である。**
片方だけを見て通す経路を作らない。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from harness.domain._registry_generated import ErrorCode

__all__ = [
    "CrossReferenceResult",
    "verify_effect_receipt_reference",
]

_ACCEPTED: Final[str] = "ACCEPTED"
_REJECTED: Final[str] = "REJECTED"


@dataclass(frozen=True, slots=True)
class CrossReferenceResult:
    """Cross Reference 検証の結果。"""

    schema_validation_id: str
    state: str
    error_code: ErrorCode | None
    #: 見つからなかった参照。実値は載せない（不変条件#7）。
    missing_reference: str | None = None

    @property
    def rejected(self) -> bool:
        return self.state == _REJECTED


def verify_effect_receipt_reference(
    receipt: Mapping[str, Any],
    *,
    known_journal_ids: frozenset[str],
    schema_validation_id: str = "sv-1",
) -> CrossReferenceResult:
    """`EffectReceipt` が実在する Operation Journal を指すことを確かめる。

    Schema検証を通ったRecordだけがここへ来る前提だが、
    **参照Fieldの欠落そのものも拒否する**。欠落を「参照なし」として
    通すと、Journalの無いReceiptが受理される。
    """
    journal_id = receipt.get("operation_journal_id")
    if not isinstance(journal_id, str) or not journal_id:
        return CrossReferenceResult(
            schema_validation_id=schema_validation_id,
            state=_REJECTED,
            error_code=ErrorCode.JOURNAL_REFERENCE_MISSING,
            missing_reference="operation_journal_id",
        )
    if journal_id not in known_journal_ids:
        # 値はあるが指す先が無い。どの作用への受領証か確定しない。
        return CrossReferenceResult(
            schema_validation_id=schema_validation_id,
            state=_REJECTED,
            error_code=ErrorCode.JOURNAL_REFERENCE_MISSING,
            missing_reference="operation_journal_id",
        )
    return CrossReferenceResult(
        schema_validation_id=schema_validation_id,
        state=_ACCEPTED,
        error_code=None,
    )
