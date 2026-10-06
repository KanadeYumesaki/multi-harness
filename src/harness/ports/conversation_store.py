"""会話 Store の Port（CLAUDE.md §2）。

Application 層はここだけを見る。`infrastructure/` の具象を直接 import しない。

## 保存先 Path を出さない

Artifact の `storage_path` は Manifest 側の関心であり、この Port は扱わない。
本文は `ArtifactStorePort` が持ち、ここは `content_artifact_hash` で参照する。

## Repository は Commit しない

不変条件#15 のとおり、Transaction 境界は Application 層の Unit of Work が持つ。
この Port の実装は `commit()` を呼ばない。
"""

from __future__ import annotations

from typing import Protocol

from harness.domain.conversation import Conversation, ConversationMessage
from harness.domain.conversation_snapshot import ConversationSnapshot

__all__ = [
    "ConversationMessageStorePort",
    "ConversationSnapshotStorePort",
    "ConversationStorePort",
]


class ConversationStorePort(Protocol):
    """`Conversation@1.0.0` の保存と取得。

    **更新操作を持たない。** Append-only であり、既存 Record を UPDATE しない
    （設計書 §4.11）。訂正が要る場合は既存の訂正規則に従う。
    """

    def add(self, conversation: Conversation) -> None:
        """1 件追加する。既存 ID への再追加は `STORAGE_WRITE_FAILED`。"""
        ...

    def get(self, conversation_id: str) -> Conversation | None: ...

    def list_all(self) -> tuple[Conversation, ...]:
        """作成順に返す。**Filesystem 列挙順を使わない。**"""
        ...


class ConversationMessageStorePort(Protocol):
    """`ConversationMessage@2.0.0` の保存と取得。"""

    def add(self, message: ConversationMessage) -> None:
        """1 件追加する。

        同一 Conversation 内の `sequence_number` 重複は `STORAGE_WRITE_FAILED`。
        既存 Record を書き換えない。親 Conversation も書き換えない。
        """
        ...

    def list_for_conversation(self, conversation_id: str) -> tuple[ConversationMessage, ...]:
        """`sequence_number` 昇順で返す。

        時刻や `message_id` で並べない。Filesystem 列挙順も使わない。
        """
        ...

    def next_sequence_number(self, conversation_id: str) -> int:
        """次に使う順序キー。Transaction 内で呼ぶ前提である。

        `BEGIN IMMEDIATE` が書込みロックを取るので、同時追加でも重複しない。
        """
        ...


class ConversationSnapshotStorePort(Protocol):
    """`ConversationSnapshot@1.0.0` の保存と取得。

    **更新操作を持たない。** 再生成は新しい Snapshot Record として足す
    （設計書 §4.11 / Append-only）。既存 Snapshot を書き換えない。
    """

    def add(self, snapshot: ConversationSnapshot) -> None:
        """1 件追加する。既存 ID への再追加は `STORAGE_WRITE_FAILED`。"""
        ...

    def list_for_conversation(self, conversation_id: str) -> tuple[ConversationSnapshot, ...]:
        """作成順に返す。**Filesystem 列挙順を使わない。**"""
        ...
