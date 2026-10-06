"""正本Ledgerを**読むだけ**のView。Case観測へ実Ledgerを渡すために使う。

`LedgerProbe` は Ledger を持たない層の Case 用の代替である。Orchestrator が
実際に SQLite の正本 Ledger へ Append するようになった Case では、代替では
なく**正本そのもの**を観測しなければ「Ledger に残ったか」を確かめられない
（設計書§19.1.1）。

本 View は書込みAPIを持たない。読むだけである。Probe が値を作らないのと
同じ理由で、観測器が Ledger を動かせてはならない。
"""

from __future__ import annotations

from harness.ports.event_ledger import EventLedgerPort

__all__ = ["RealLedgerView"]


class RealLedgerView:
    """`load_stream` と `stream_head` だけを使って観測面を組む。"""

    def __init__(self, ledger: EventLedgerPort, stream_id: str) -> None:
        self._ledger = ledger
        self.stream_id = stream_id

    @property
    def appended(self) -> list[str]:
        """Stream の先頭から現在までの Event 型名。実Ledgerから読む。"""
        return [entry.event_type for entry in self._ledger.load_stream(self.stream_id)]

    @property
    def head(self) -> int:
        head = self._ledger.stream_head(self.stream_id)
        listed = len(self.appended)
        if head != listed:
            # Head と列が食い違う Ledger を「観測できた」と言わない。
            raise AssertionError(
                f"正本Ledgerが不整合である: head={head} entries={listed}（stream={self.stream_id}）"
            )
        return head

    @property
    def effect_attempts(self) -> int:
        """Effect 試行を表す Event の件数。Input Read では 0 になるはずである。"""
        started = {"ACTION_PREPARED", "ACTION_EXECUTING", "EXECUTION_ATTEMPTED", "EFFECT_ATTEMPTED"}
        return sum(1 for name in self.appended if name in started)
