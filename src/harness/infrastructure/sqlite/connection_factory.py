"""唯一のSQLite接続生成点（不変条件#15、§1.14 物理永続化境界）。

`sqlite3.connect()`の直接使用は本Module以外で禁止し、
`tests/spec_lint/test_layer_dependencies.py`が静的に検査する。

§1.14「SQLite接続は`ConnectionFactory`から生成し、WAL、`synchronous=FULL`、
`foreign_keys=ON`を接続ごとに検証する」に従い、**設定するだけでなく設定後に読み戻して
検証**する。PRAGMAは黙って無視されることがあるため、設定の成功を仮定しない。

Migration用接続とRuntime用接続を`ConnectionRole`で分離する。
Runtime接続はLedgerのUPDATE／DELETEをTriggerで拒否される側であり、
Migration接続だけがSchema変更を行う。
"""

from __future__ import annotations

import sqlite3
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Final, Literal

from harness.domain.errors import ErrorCode, HarnessError
from harness.infrastructure.sqlite.authorizer import install_runtime_authorizer

__all__ = ["ConnectionFactory", "ConnectionRole", "SqliteTransactionScope"]

_BUSY_TIMEOUT_MS: Final[int] = 5000


class ConnectionRole(Enum):
    """接続の用途。Schema変更を行えるのはMIGRATIONだけとする。"""

    RUNTIME = "RUNTIME"
    MIGRATION = "MIGRATION"


class SqliteTransactionScope:
    """`BEGIN IMMEDIATE` から `COMMIT` までのTransaction。

    正常終了でCommit、例外でRollbackして例外を再送出する（不変条件#9）。
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> SqliteTransactionScope:
        self._connection.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """常に`False`を返す。例外を握り潰さない（不変条件#9）。

        戻り値型を`bool`にすると「Trueを返し得る＝例外を飲み込み得る」と読めるため、
        `Literal[False]`で不可能であることを型で示す。
        """
        if exc_type is None:
            self._connection.commit()
        else:
            self._connection.rollback()
        return False

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection


class ConnectionFactory:
    """単一SQLite DBへの接続を生成する唯一の入口。"""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    @property
    def database_path(self) -> Path:
        return self._database_path

    def connect(
        self,
        role: ConnectionRole = ConnectionRole.RUNTIME,
        *,
        cross_thread: bool = False,
    ) -> sqlite3.Connection:
        """PRAGMAを適用し、**読み戻して検証**した接続を返す。

        ``cross_thread`` は「この接続を複数Threadから使う」宣言である。既定は
        ``False``（sqlite3の既定と同じ、生成Thread以外からの利用を拒否）。

        ``True`` を渡してよいのは、**接続の直列化を呼出側が保証している**場合
        だけである。ローカルUIは `ThreadingHTTPServer` がRequestごとに別Thread
        を作るため、Lockで1度に1 Requestへ直列化したうえでこれを使う。宣言を
        省いて共有すると `ProgrammingError` になり、UIからは「storage
        temporarily unavailable」としか見えない失敗になる。
        """
        # 本Moduleが sqlite3.connect() の唯一の生成点（不変条件#15）。
        connection = sqlite3.connect(
            self._database_path,
            isolation_level=None,  # BEGIN を明示制御する。暗黙Transactionを使わない
            timeout=_BUSY_TIMEOUT_MS / 1000,
            check_same_thread=not cross_thread,
        )
        connection.row_factory = sqlite3.Row
        try:
            self._apply_and_verify_pragmas(connection, role)
            if role is ConnectionRole.RUNTIME:
                # 多層防御の第3層。Schema変更とLedger UPDATE／DELETEをSQL発行時点で拒否する。
                # PRAGMA適用後に設定する（authorizerはPRAGMAも拒否対象にし得るため）。
                install_runtime_authorizer(connection)
        except Exception:
            connection.close()
            raise
        return connection

    def begin_immediate(self, connection: sqlite3.Connection) -> SqliteTransactionScope:
        return SqliteTransactionScope(connection)

    # ------------------------------------------------------------------
    # PRAGMA
    # ------------------------------------------------------------------

    def _apply_and_verify_pragmas(
        self, connection: sqlite3.Connection, role: ConnectionRole
    ) -> None:
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")

        self._require(connection, "journal_mode", "wal", role)
        self._require(connection, "synchronous", 2, role)  # 2 = FULL
        self._require(connection, "foreign_keys", 1, role)
        self._require(connection, "busy_timeout", _BUSY_TIMEOUT_MS, role)

    def _require(
        self,
        connection: sqlite3.Connection,
        pragma: str,
        expected: object,
        role: ConnectionRole,
    ) -> None:
        row = connection.execute(f"PRAGMA {pragma}").fetchone()
        actual = row[0] if row is not None else None
        normalized = actual.lower() if isinstance(actual, str) else actual
        if normalized != expected:
            raise HarnessError(
                ErrorCode.STORAGE_WRITE_FAILED,
                f"PRAGMA {pragma} is {actual!r}, expected {expected!r} "
                f"(role={role.value}, db={self._database_path})",
            )
