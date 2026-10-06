"""§1.14 LedgerのUPDATE／DELETE禁止 — 多層防御の第3層（SQLite authorizer）。

TASK-MVP0A-002で次の2層を用意した。

1. `event_ledger`へのUPDATE／DELETE拒否Trigger（実体を守る）
2. Migration接続とRuntime接続の分離

本Moduleは3層目として、**SQL文の発行そのもの**をRuntime接続で拒否する。
Triggerだけでは「Triggerを落としてからUPDATE」という順序を止められない。
authorizerは`DROP TRIGGER`／`DROP TABLE`／`ALTER TABLE`も拒否するため、
この経路を先に塞ぐ。

Migration接続へは設定しない。Schema変更はMigrationの責務である。
"""

from __future__ import annotations

import sqlite3
from typing import Final

__all__ = ["LEDGER_TABLES", "install_runtime_authorizer"]

# 追記専用として保護するテーブル。
LEDGER_TABLES: Final[frozenset[str]] = frozenset({"event_ledger"})

# 保護対象テーブルに対して拒否する操作。
_DENIED_ON_LEDGER: Final[frozenset[int]] = frozenset({sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE})

# Schemaそのものを変更する操作はRuntime接続で一切拒否する。
# これを許すとTriggerを落としてからUPDATEできてしまう。
_DENIED_SCHEMA_ACTIONS: Final[frozenset[int]] = frozenset(
    {
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_TABLE,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_DROP_TEMP_TRIGGER,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TEMP_INDEX,
        sqlite3.SQLITE_DROP_VIEW,
        sqlite3.SQLITE_DROP_TEMP_VIEW,
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_VIEW,
    }
)


def install_runtime_authorizer(connection: sqlite3.Connection) -> None:
    """Runtime接続へauthorizerを設定する。

    * `event_ledger`へのUPDATE／DELETEを拒否する
    * Schema変更（CREATE／DROP／ALTER）を拒否する
    * それ以外は許可する（INSERT・SELECT・Transaction制御）
    """

    def authorize(
        action: int,
        arg1: str | None,
        arg2: str | None,
        database: str | None,
        trigger: str | None,
    ) -> int:
        if action in _DENIED_SCHEMA_ACTIONS:
            return sqlite3.SQLITE_DENY
        if action in _DENIED_ON_LEDGER and arg1 in LEDGER_TABLES:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
