"""`ConversationSnapshot@1.0.0` の Domain 型と Hash 導出（設計書 §4.11）。

## SQLite も Filesystem も知らない

CLAUDE.md §2 のとおり `sqlite3`／`os`／`pathlib` を import しない。Artifact CAS の
Path もここへ来ない。

## 3 つの Hash を混同しない

    message_set_hash … 各 Message の content_hash を sequence_number 昇順で並べた配列
    snapshot_hash    … message_set_hash・schema_set_hash・design_sha256・
                       conversation_hash を束ねた値
    conversation_hash … Conversation 作成時の不変メタだけ（Message を含まない）

`snapshot_hash` は **`snapshot_id` を入力に取らない**。ID を入れると、同じ履歴から
作った Snapshot が別物になり、再生成しても同一性を確かめられなくなる。

Hash 自身も入力に取らない。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, Final

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash, hash_canonical

__all__ = [
    "ConversationSnapshot",
    "derive_message_set_hash",
    "derive_snapshot_hash",
]

_HASH_PROFILE_VERSION: Final[int] = 1

_MESSAGE_SET_ARTIFACT_TYPE: Final[str] = "conversation-message-set"
_MESSAGE_SET_SCHEMA_MAJOR: Final[int] = 1
_SNAPSHOT_ARTIFACT_TYPE: Final[str] = "conversation-snapshot"
_SNAPSHOT_SCHEMA_MAJOR: Final[int] = 1


def derive_message_set_hash(content_hashes: Sequence[ContentHash]) -> ContentHash:
    """`content_hash` を **渡された順のまま** 並べて Hash する。

    並べ替えはしない。順序キー昇順で渡すのは呼出側の責務であり、ここで
    並べ直すと「順序が違う 2 つの履歴」が同じ Hash になってしまう。

    空の Conversation も有効な入力である。空配列の Hash は決まった値になり、
    どの非空集合とも一致しない。
    """
    payload = {
        "content_hashes": [str(item) for item in content_hashes],
        "hash_profile_version": _HASH_PROFILE_VERSION,
    }
    return hash_canonical(
        payload,
        artifact_type=_MESSAGE_SET_ARTIFACT_TYPE,
        schema_major=_MESSAGE_SET_SCHEMA_MAJOR,
    )


def derive_snapshot_hash(
    *,
    conversation_hash: ContentHash,
    message_set_hash: ContentHash,
    schema_set_hash: ContentHash,
    design_sha256: ContentHash,
) -> ContentHash:
    """束縛先 4 つから導出する。

    **`snapshot_id` も時刻も PID も入力に取らない。** 引数がこの 4 つだけである
    ことが、その根拠である（不変条件#4）。
    """
    payload = {
        "conversation_hash": str(conversation_hash),
        "design_sha256": str(design_sha256),
        "hash_profile_version": _HASH_PROFILE_VERSION,
        "message_set_hash": str(message_set_hash),
        "schema_set_hash": str(schema_set_hash),
    }
    return hash_canonical(
        payload, artifact_type=_SNAPSHOT_ARTIFACT_TYPE, schema_major=_SNAPSHOT_SCHEMA_MAJOR
    )


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    """`ConversationSnapshot@1.0.0`。

    **本文を持たない。** Message の本文も要約も入れない。持てば Snapshot 自体が
    漏洩経路になる。参照は `message_set_hash` だけである。
    """

    SCHEMA_NAME: ClassVar[str] = "ConversationSnapshot"

    snapshot_id: str
    conversation_id: str
    snapshot_hash: ContentHash
    message_set_hash: ContentHash
    schema_set_hash: ContentHash
    design_sha256: ContentHash
    record_id: str
    created_at: str
    producer: str
    content_hash: ContentHash

    def __post_init__(self) -> None:
        for name in ("snapshot_id", "conversation_id", "record_id", "created_at", "producer"):
            if not getattr(self, name):
                raise HarnessError(ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, f"{name} is empty")

    def recompute_hash(self, *, conversation_hash: ContentHash) -> ContentHash:
        """記録された束縛先から `snapshot_hash` を導き直す。"""
        return derive_snapshot_hash(
            conversation_hash=conversation_hash,
            message_set_hash=self.message_set_hash,
            schema_set_hash=self.schema_set_hash,
            design_sha256=self.design_sha256,
        )
