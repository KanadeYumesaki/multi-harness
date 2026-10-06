"""`Conversation@1.0.0` / `ConversationMessage@2.0.0` の SQLite Repository。

不変条件#15 に従い `commit()` しない。呼出側の Unit of Work が開いた
`BEGIN IMMEDIATE` Transaction の内側で実行される前提とする。

## 本文を持たない

保存するのは識別子と Hash だけである。本文は Artifact CAS にあり、ここは
`content_artifact_hash` で参照する。**行に本文を入れない。** 入れると SQLite
自体が漏洩経路になり、消去要求へ応えるには行ごと消すしかなくなる。

## 親を書き換えない

Message 追加時に `conversation` 表へ触らない。件数も持たない。UPDATE 文を
1 つも書かないことが、Append-only の実装上の根拠である。

## 順序キーの衝突

`UNIQUE (conversation_id, sequence_number)` を DB 側の制約で持つ。
Application 側の採番だけに頼らない。`BEGIN IMMEDIATE` が書込みロックを取るため、
同時追加は直列化される。
"""

from __future__ import annotations

import sqlite3

from harness.domain.context_budget import MessageRole
from harness.domain.conversation import Conversation, ConversationMessage
from harness.domain.conversation_snapshot import ConversationSnapshot
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash
from harness.infrastructure.sqlite.transaction_guard import require_transaction

__all__ = [
    "SqliteConversationMessageRepository",
    "SqliteConversationRepository",
    "SqliteConversationSnapshotRepository",
]

_STORE_VERSION = 1


class SqliteConversationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def add(self, conversation: Conversation) -> None:
        require_transaction(self._connection, "conversation add")
        try:
            self._connection.execute(
                """
                INSERT INTO conversation (
                    conversation_id, conversation_hash, record_id,
                    created_at, producer, content_hash, store_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation.conversation_id,
                    str(conversation.conversation_hash),
                    conversation.record_id,
                    conversation.created_at,
                    conversation.producer,
                    str(conversation.content_hash),
                    _STORE_VERSION,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # `INSERT OR REPLACE` を使わない。同じIDへ別の内容を書けると、
            # どちらが本物か決められなくなる。
            raise HarnessError(
                ErrorCode.STORAGE_WRITE_FAILED,
                f"conversation {conversation.conversation_id} already exists",
            ) from exc

    def get(self, conversation_id: str) -> Conversation | None:
        row = self._connection.execute(
            """
            SELECT conversation_id, conversation_hash, record_id, created_at, producer,
                   content_hash
            FROM conversation WHERE conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()
        return None if row is None else _to_conversation(row)

    def list_all(self) -> tuple[Conversation, ...]:
        rows = self._connection.execute(
            """
            SELECT conversation_id, conversation_hash, record_id, created_at, producer,
                   content_hash
            FROM conversation ORDER BY created_at, conversation_id
            """
        ).fetchall()
        return tuple(_to_conversation(row) for row in rows)


class SqliteConversationMessageRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def add(self, message: ConversationMessage) -> None:
        require_transaction(self._connection, "conversation message add")
        parent = self._connection.execute(
            "SELECT 1 FROM conversation WHERE conversation_id = ?", (message.conversation_id,)
        ).fetchone()
        if parent is None:
            raise HarnessError(
                ErrorCode.STORAGE_WRITE_FAILED,
                f"conversation {message.conversation_id} does not exist",
            )
        try:
            self._connection.execute(
                """
                INSERT INTO conversation_message (
                    message_id, conversation_id, role, sequence_number,
                    content_artifact_hash, record_id, created_at, producer,
                    content_hash, store_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.message_id,
                    message.conversation_id,
                    message.role.value,
                    message.sequence_number,
                    str(message.content_artifact_hash),
                    message.record_id,
                    message.created_at,
                    message.producer,
                    str(message.content_hash),
                    _STORE_VERSION,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HarnessError(
                ErrorCode.STORAGE_WRITE_FAILED,
                f"message {message.message_id} or sequence_number "
                f"{message.sequence_number} already exists in "
                f"conversation {message.conversation_id}",
            ) from exc

    def list_for_conversation(self, conversation_id: str) -> tuple[ConversationMessage, ...]:
        rows = self._connection.execute(
            """
            SELECT message_id, conversation_id, role, sequence_number, content_artifact_hash,
                   record_id, created_at, producer, content_hash
            FROM conversation_message
            WHERE conversation_id = ?
            ORDER BY sequence_number
            """,
            (conversation_id,),
        ).fetchall()
        return tuple(_to_message(row) for row in rows)

    def next_sequence_number(self, conversation_id: str) -> int:
        row = self._connection.execute(
            "SELECT MAX(sequence_number) FROM conversation_message WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        current = row[0] if row and row[0] is not None else 0
        return int(current) + 1


def _to_conversation(row: sqlite3.Row | tuple[object, ...]) -> Conversation:
    return Conversation(
        conversation_id=str(row[0]),
        conversation_hash=ContentHash.parse(str(row[1])),
        record_id=str(row[2]),
        created_at=str(row[3]),
        producer=str(row[4]),
        content_hash=ContentHash.parse(str(row[5])),
    )


def _to_message(row: sqlite3.Row | tuple[object, ...]) -> ConversationMessage:
    return ConversationMessage(
        message_id=str(row[0]),
        conversation_id=str(row[1]),
        role=MessageRole(str(row[2])),
        sequence_number=int(row[3]),  # type: ignore[arg-type]
        content_artifact_hash=ContentHash.parse(str(row[4])),
        record_id=str(row[5]),
        created_at=str(row[6]),
        producer=str(row[7]),
        content_hash=ContentHash.parse(str(row[8])),
    )


class SqliteConversationSnapshotRepository:
    """Snapshot の保存。**INSERT と SELECT だけ。**

    再生成は新しい Record として足す。既存 Snapshot を UPDATE も DELETE もしない。
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def add(self, snapshot: ConversationSnapshot) -> None:
        require_transaction(self._connection, "conversation snapshot add")
        parent = self._connection.execute(
            "SELECT 1 FROM conversation WHERE conversation_id = ?", (snapshot.conversation_id,)
        ).fetchone()
        if parent is None:
            raise HarnessError(
                ErrorCode.STORAGE_WRITE_FAILED,
                f"conversation {snapshot.conversation_id} does not exist",
            )
        try:
            self._connection.execute(
                """
                INSERT INTO conversation_snapshot (
                    snapshot_id, conversation_id, snapshot_hash, message_set_hash,
                    schema_set_hash, design_sha256, record_id, created_at, producer,
                    content_hash, store_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.conversation_id,
                    str(snapshot.snapshot_hash),
                    str(snapshot.message_set_hash),
                    str(snapshot.schema_set_hash),
                    str(snapshot.design_sha256),
                    snapshot.record_id,
                    snapshot.created_at,
                    snapshot.producer,
                    str(snapshot.content_hash),
                    _STORE_VERSION,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HarnessError(
                ErrorCode.STORAGE_WRITE_FAILED,
                f"snapshot {snapshot.snapshot_id} already exists",
            ) from exc

    def list_for_conversation(self, conversation_id: str) -> tuple[ConversationSnapshot, ...]:
        rows = self._connection.execute(
            """
            SELECT snapshot_id, conversation_id, snapshot_hash, message_set_hash,
                   schema_set_hash, design_sha256, record_id, created_at, producer, content_hash
            FROM conversation_snapshot
            WHERE conversation_id = ?
            ORDER BY created_at, snapshot_id
            """,
            (conversation_id,),
        ).fetchall()
        return tuple(_to_snapshot(row) for row in rows)


def _to_snapshot(row: sqlite3.Row | tuple[object, ...]) -> ConversationSnapshot:
    return ConversationSnapshot(
        snapshot_id=str(row[0]),
        conversation_id=str(row[1]),
        snapshot_hash=ContentHash.parse(str(row[2])),
        message_set_hash=ContentHash.parse(str(row[3])),
        schema_set_hash=ContentHash.parse(str(row[4])),
        design_sha256=ContentHash.parse(str(row[5])),
        record_id=str(row[6]),
        created_at=str(row[7]),
        producer=str(row[8]),
        content_hash=ContentHash.parse(str(row[9])),
    )
