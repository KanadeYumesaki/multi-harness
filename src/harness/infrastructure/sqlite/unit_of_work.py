"""`UnitOfWorkPort` のSQLite実装（不変条件#15）。

Transaction境界の所有者はApplication層である。Repositoryは`commit()`しない。
本Moduleはその境界をSQLiteの`BEGIN IMMEDIATE`で実現する。

`DEFERRED` を提供しない。Effect ProtocolのDurable境界は
`BEGIN IMMEDIATE` から `COMMIT` までの単一Transactionで実現する（ADR-001）。
DEFERREDだと最初の書込みまでロックを取らず、そこで競合が判明する。
"""

from __future__ import annotations

import sqlite3

from harness.infrastructure.sqlite.connection_factory import (
    ConnectionFactory,
    SqliteTransactionScope,
)

__all__ = ["SqliteUnitOfWork"]


class SqliteUnitOfWork:
    def __init__(self, factory: ConnectionFactory, connection: sqlite3.Connection) -> None:
        self._factory = factory
        self._connection = connection

    def begin_immediate(self) -> SqliteTransactionScope:
        return self._factory.begin_immediate(self._connection)

    def in_transaction(self) -> bool:
        return self._connection.in_transaction

    def transaction_identity(self) -> object:
        """同一Transaction文脈の識別子。Repositoryとの一致確認に使う。"""
        return self._connection
