"""§1.6 UnitOfWorkPort。Transaction境界の所有者。

不変条件#15「DB接続は`ConnectionFactory`だけから生成し、Repositoryは`commit()`しない。
Transaction所有者はApplication層のUnit of Work」を型で表現する。

Effect ProtocolのDurable境界は`BEGIN IMMEDIATE`から`COMMIT`までの単一Transactionで
実現する（ADR-001）。そのため`begin_immediate`だけを公開し、DEFERREDを提供しない。
"""

from __future__ import annotations

from types import TracebackType
from typing import Protocol

__all__ = ["TransactionScope", "UnitOfWorkPort"]


class TransactionScope(Protocol):
    """`BEGIN IMMEDIATE`で開かれたTransaction。

    正常終了でCommit、例外でRollbackする。例外を握り潰さない（不変条件#9）。
    """

    def __enter__(self) -> TransactionScope: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool: ...


class UnitOfWorkPort(Protocol):
    def begin_immediate(self) -> TransactionScope: ...

    def in_transaction(self) -> bool:
        """Transactionが開いているか。

        外部Process呼出しの直前に確認する。`BEGIN IMMEDIATE` は書込みロックを
        取るため、応答を待つ間ずっとDBを掴むことになる。Application層が
        `sqlite3.Connection` を見ずに判定できるよう、Portへ出す。
        """
        ...
