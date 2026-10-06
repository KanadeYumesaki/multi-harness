"""書込みがTransaction内であることの検査（不変条件#15）。

## なぜ要るか

`isolation_level=None` のため、SQLiteは明示的な`BEGIN`が無いと
**文ごとに自動Commit**する。Repositoryは「Unit of Workが開いた
Transactionの内側で呼ばれる」前提で書かれているが、その前提を
検査していなければ、外から呼ばれても静かに通ってしまう。

実際に次が再現された。

    正常Event と 不正Event を1回のappendで渡す（Transaction外）
    -> 1件目だけが自動Commitされ、2件目で例外
    -> 部分的に書かれた状態が残る

Append単位の原子性が前提から崩れる。Hash Chainは連続している必要が
あるため、途中まで書かれた状態は後続のAppendを全て狂わせる。

## 規約は破られる前提で書く

「Transaction境界はApplication層が持つ」という規約はあった。
しかしRepositoryは規約が守られたかを見ていなかった。破られたら
止まる形にして初めて、規約が実効を持つ。

読取りには適用しない。Transactionの有無で結果の正しさが変わらず、
Recovery用の照会にまでTransactionを要求することになるためである。
"""

from __future__ import annotations

import sqlite3

from harness.domain.errors import ErrorCode, HarnessError

__all__ = ["require_transaction"]


def require_transaction(connection: sqlite3.Connection, operation: str) -> None:
    """書込みの直前に呼ぶ。Transaction未開始なら停止する。"""
    if not connection.in_transaction:
        raise HarnessError(
            ErrorCode.RUNTIME_SPEC_MISMATCH,
            f"{operation} requires an open transaction; "
            "repositories never commit and must run inside a unit of work "
            "(invariant #15). Without one, SQLite auto-commits each statement "
            "and a partially applied write can survive a failure.",
        )
