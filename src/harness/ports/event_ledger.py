"""§1.6 EventLedgerPort。

| Operation | 入力 | 出力 |
|---|---|---|
| `append` | Events、`expected_stream_sequence` | Append Result |
| `load_stream` | Stream ID、After Sequence | Domain Events |
| `verify_chain` | Stream ID | Chain Verification Result |

Repositoryは`commit()`しない。Transaction所有者はApplication層のUnit of Workである
（不変条件#15）。実装はTransactionの内側で呼ばれることを前提にする。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from harness.domain.hashing import ContentHash
from harness.domain.ledger import ChainVerificationResult, LedgerEntry

__all__ = ["AppendResult", "EventLedgerPort", "NewEvent"]


@dataclass(frozen=True, slots=True)
class NewEvent:
    """Append要求1件。`sequence_number`と`event_hash`はLedgerが決める。"""

    stream_id: str
    event_type: str
    payload_hash: ContentHash
    recorded_at: str


@dataclass(frozen=True, slots=True)
class AppendResult:
    appended_count: int
    first_sequence_number: int
    last_sequence_number: int
    head_event_hash: ContentHash


class EventLedgerPort(Protocol):
    """Domain／Application層が依存する抽象。"""

    def append(self, events: Sequence[NewEvent], *, expected_stream_sequence: int) -> AppendResult:
        """`expected_stream_sequence`が現在のStream末尾と一致する場合だけAppendする。

        不一致は`EVENT_ORDER_VIOLATION`の`HarnessError`を送出する。
        """
        ...

    def stream_head(self, stream_id: str) -> int:
        """Streamの末尾Sequence。未使用Streamは0。

        `expected_stream_sequence` のCASへ渡す値であり、Application層が
        SQLを書かずに取得できる必要がある。
        """
        ...

    def load_stream(self, stream_id: str, *, after_sequence: int = 0) -> list[LedgerEntry]: ...

    def verify_chain(self, stream_id: str) -> ChainVerificationResult: ...
