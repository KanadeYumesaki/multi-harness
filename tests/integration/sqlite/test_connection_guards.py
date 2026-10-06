"""接続生成時のFail-Closed（§1.14、不変条件#15）。

## なぜ必要か

`connection_factory.py` は PRAGMA を「適用して**読み戻して**検証する」と
書いてあり、既存試験も読み戻し値が期待どおりであることを確かめている。
だが**期待と違ったときに止まるか**は一度も試していなかった。

検証コードが常に合格を返す状態になっていても、正常系の試験は同じように
通る。「検証している」と「検証が効いている」は別である。

SQLiteの `PRAGMA` は**黙って無視されることがある**。WALへ移れない
Filesystem、他接続が保持中のjournal_mode、Buildオプションの違い。
どれもエラーにならず既定値のまま進む。`synchronous` が既定値へ落ちれば、
Commitしたつもりの Event が電源断で消える。動いている間は何も起きない。

あわせて `SqliteUnitOfWork.transaction_identity()` の**実体**も確かめる。
Application層の同一Transaction判定に使われるが、これまで試験用の代役
しか実行されていなかった。

## 差し替え方

`sqlite3.Connection` は不変型でMethodを差し替えられない。
`sqlite3.connect` を差し替え、**読み戻しの結果だけを偽る Proxy** を返す。
PRAGMAの適用そのものは本物へ流すので、「適用したのに効いていない」
という実際の状況に近い形になる。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from harness.domain.errors import ErrorCode, HarnessError
from harness.infrastructure.sqlite.connection_factory import (
    ConnectionFactory,
    ConnectionRole,
)
from harness.infrastructure.sqlite.migrations import migrate
from harness.infrastructure.sqlite.unit_of_work import SqliteUnitOfWork

pytestmark = pytest.mark.integration

RECORDED_AT = "2026-08-15T00:00:00Z"


@pytest.fixture
def factory(tmp_path: Path) -> ConnectionFactory:
    made = ConnectionFactory(tmp_path / "harness.db")
    migrate(made, recorded_at=RECORDED_AT)
    return made


class _FakeCursor:
    """`fetchone()` だけを持つ最小の代役。"""

    def __init__(self, row: tuple[object] | None) -> None:
        self._row = row

    def fetchone(self) -> tuple[object] | None:
        return self._row


class _LyingConnection:
    """特定PRAGMAの**読み戻し**だけ偽る Proxy。他は本物へ委譲する。"""

    def __init__(self, real: sqlite3.Connection, pragma: str, row: tuple[object] | None) -> None:
        self._real = real
        self._pragma = pragma
        self._row = row
        self.closed = False

    def execute(self, sql: str, *arguments: Any) -> Any:
        if sql.strip() == f"PRAGMA {self._pragma}":
            return _FakeCursor(self._row)
        return self._real.execute(sql, *arguments)

    def close(self) -> None:
        self.closed = True
        self._real.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"_real", "_pragma", "_row", "closed"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self._real, name, value)


def _install_liar(
    monkeypatch: pytest.MonkeyPatch, pragma: str, row: tuple[object] | None
) -> list[_LyingConnection]:
    created: list[_LyingConnection] = []
    real_connect = sqlite3.connect

    def fake_connect(*arguments: Any, **keywords: Any) -> Any:
        proxy = _LyingConnection(real_connect(*arguments, **keywords), pragma, row)
        created.append(proxy)
        return proxy

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    return created


# ---------------------------------------------------------------------------
# PRAGMA 読み戻し検証
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pragma", "wrong_value"),
    [
        ("journal_mode", "delete"),
        ("synchronous", 0),
        ("foreign_keys", 0),
        ("busy_timeout", 1),
    ],
)
def test_pragma_that_did_not_take_effect_is_rejected(
    factory: ConnectionFactory,
    monkeypatch: pytest.MonkeyPatch,
    pragma: str,
    wrong_value: object,
) -> None:
    """読み戻した値が期待と違えば接続を作らせない。"""
    _install_liar(monkeypatch, pragma, (wrong_value,))

    with pytest.raises(HarnessError) as error:
        factory.connect(ConnectionRole.RUNTIME)
    assert error.value.code is ErrorCode.STORAGE_WRITE_FAILED
    assert pragma in str(error.value)


def test_pragma_returning_no_row_is_rejected(
    factory: ConnectionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PRAGMA` が行を返さない場合。未知のPRAGMAはSQLiteでは空を返す。

    `None` を「まだ設定されていない」と読んで先へ進むと、
    検証を通したことにならない。
    """
    _install_liar(monkeypatch, "foreign_keys", None)

    with pytest.raises(HarnessError) as error:
        factory.connect(ConnectionRole.RUNTIME)
    assert error.value.code is ErrorCode.STORAGE_WRITE_FAILED


def test_failed_pragma_verification_does_not_leak_the_connection(
    factory: ConnectionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """検証に失敗したら接続を閉じる。

    閉じずに例外を投げると、失敗のたびにSQLite接続とFDが残る。
    Fail-Closedの経路で資源が漏れるのは、失敗が続くほど悪化するという
    ことである。
    """
    created = _install_liar(monkeypatch, "journal_mode", ("delete",))

    with pytest.raises(HarnessError):
        factory.connect(ConnectionRole.RUNTIME)

    assert created, "接続が作られていない。試験の前提が崩れている"
    assert all(proxy.closed for proxy in created), "検証失敗後に接続が閉じられていない"


def test_healthy_connection_passes_verification(factory: ConnectionFactory) -> None:
    """壊していない接続が落ちるなら、上の試験は何も証明しない。"""
    connection = factory.connect(ConnectionRole.RUNTIME)
    try:
        assert str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# transaction_identity の実体
# ---------------------------------------------------------------------------


def test_unit_of_work_identity_is_stable_and_distinguishes_connections(
    factory: ConnectionFactory,
) -> None:
    """Application層の同一Transaction判定に使われる識別子。

    これまで実体は一度も呼ばれておらず、試験用の代役だけが動いていた。
    Application層はこの値を**不透明な識別子**として比較するだけなので、
    要件は2つしかない。同じ接続なら等しいこと、違う接続なら等しくないこと。
    """
    first = factory.connect(ConnectionRole.RUNTIME)
    second = factory.connect(ConnectionRole.RUNTIME)
    try:
        one = SqliteUnitOfWork(factory, first)
        one_again = SqliteUnitOfWork(factory, first)
        other = SqliteUnitOfWork(factory, second)

        assert one.transaction_identity() is one_again.transaction_identity()
        assert one.transaction_identity() is not other.transaction_identity()
    finally:
        first.close()
        second.close()


def test_unit_of_work_reports_transaction_state(factory: ConnectionFactory) -> None:
    """`in_transaction()` がTransactionの開閉に追随すること。

    Application層はこれを見て「Masker呼出し中にDBを掴んでいないか」を
    判断する。常にFalseを返す実装でも、正常系の試験は通ってしまう。
    """
    connection = factory.connect(ConnectionRole.RUNTIME)
    try:
        unit = SqliteUnitOfWork(factory, connection)
        assert unit.in_transaction() is False
        with factory.begin_immediate(connection):
            assert unit.in_transaction() is True
        assert unit.in_transaction() is False
    finally:
        connection.close()
