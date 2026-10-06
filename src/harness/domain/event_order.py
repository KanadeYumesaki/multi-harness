"""§1.4.2 Event Stream の順序検証。Sequence の逆行と飛ばしを拒否する。

## 何を防ぐ規則か

Event Ledger は Append-only であり、訂正はCompensating Eventでしか行わない
（不変条件#1）。順序が壊れると、その保証が意味を失う。

危ないのは2方向ある。

| 誤り | 何が起きるか |
|---|---|
| **逆行**（`expected < head`） | 既に書いたEventの位置へ後から書く。過去の書換えと同じ |
| **飛ばし**（`expected > head`） | 間のEventが無いまま先へ進む。欠落が検出されないまま残る |

どちらも `expected_stream_sequence == head` の一致だけで塞げる。
**片側だけ検査すると、もう片方が通る。**

## 拒否したときAttempt Stateを動かさない

Case `AT-EVENT-ORDER-001/SEQUENCE_REGRESSION` は
`attempt_state_unchanged == true` を要求する。Appendが拒否されたのに
Attemptだけ進んでいると、Ledgerと状態Storeが食い違う。
**Appendの成否とState遷移は同一Transactionで一致させる。**
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from harness.domain._registry_generated import ErrorCode

__all__ = [
    "AppendOrderVerdict",
    "EventAppendRequest",
    "verify_append_order",
]

_ACCEPTED: Final[str] = "ACCEPTED"
_REJECTED: Final[str] = "REJECTED"


@dataclass(frozen=True, slots=True)
class EventAppendRequest:
    """1回のAppend要求。"""

    append_result_id: str
    stream_id: str
    #: 呼出側が「今のhead」と思っている値。
    expected_stream_sequence: int
    #: Append しようとしている Event 型（Registry語彙）。
    event_types: tuple[str, ...]
    #: Append前のAttempt State。拒否時はこの値のまま動かさない。
    attempt_state: str


@dataclass(frozen=True, slots=True)
class AppendOrderVerdict:
    """順序検証の結果。`EVENT_APPEND_RESULT` 名前空間のStateを持つ。"""

    append_result_id: str
    state: str
    error_code: ErrorCode | None
    #: 拒否後のAttempt State。拒否なら要求時と同一である。
    attempt_state: str
    observed_head: int

    @property
    def accepted(self) -> bool:
        return self.state == _ACCEPTED

    @property
    def rejected(self) -> bool:
        return self.state == _REJECTED


def verify_append_order(request: EventAppendRequest, *, current_head: int) -> AppendOrderVerdict:
    """Append要求の順序を検証する。

    一致しなければ `REJECTED` を返し、**Attempt Stateを動かさない**。
    逆行も飛ばしも同じ `EVENT_ORDER_VIOLATION` で止める。どちらも
    「Ledgerの並びが呼出側の思い込みと違う」という同じ事実である。
    """
    if current_head < 0:
        raise ValueError("current_head must not be negative")
    if not request.event_types:
        raise ValueError("append request must carry at least one event")

    if request.expected_stream_sequence != current_head:
        return AppendOrderVerdict(
            append_result_id=request.append_result_id,
            state=_REJECTED,
            error_code=ErrorCode.EVENT_ORDER_VIOLATION,
            # 拒否したのだから状態は進まない。ここを進めるとLedgerと食い違う。
            attempt_state=request.attempt_state,
            observed_head=current_head,
        )

    return AppendOrderVerdict(
        append_result_id=request.append_result_id,
        state=_ACCEPTED,
        error_code=None,
        attempt_state=request.attempt_state,
        observed_head=current_head,
    )
