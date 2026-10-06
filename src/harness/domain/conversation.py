"""会話の Domain 型と Hash 導出（設計書 §4.11／Owner Decision CMC-1〜CMC-6）。

## SQLite も Filesystem も知らない

CLAUDE.md §2 のとおり、この module は `sqlite3`／`os`／`subprocess`／Provider SDK を
import しない。Path も持たない。保存先は Infrastructure の関心である。

## role を作り直さない

`MessageRole` は `domain/context_budget.py` の既存 enum をそのまま使う。
`ContextFragment.message_role` と同じ 7 値であり、**Chat 用に別の語彙を作らない**。
Provider 出力は `UNTRUSTED_PROVIDER_DATA` であり、Control Role へ昇格しない。

## Hash の役割分担（§4.11）

    content_artifact_hash  … 本文Bytesそのもの。Artifact CASが持つ
    content_hash（Message）… 本文Hash・role・sequence_number を束ねた値
    conversation_hash      … conversation_id と作成時の不変メタだけ

`conversation_hash` は **Message を入力に取らない**（CMC-4-A）。だから Message を
足しても動かない。Message 集合の完全性は `ConversationSnapshot` の
`message_set_hash` が見る。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final

from harness.domain.context_budget import MessageRole
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash, hash_canonical

__all__ = [
    "Conversation",
    "ConversationMessage",
    "MessageRole",
    "derive_conversation_hash",
    "derive_message_content_hash",
]

#: §1.11 の Hash Profile Version。Profile を新設しない（CMC-12-A）。
_HASH_PROFILE_VERSION: Final[int] = 1

#: Domain Separation（§1.11）。`hash_canonical` が前置する。
#: Schema Major は Version に合わせる（Conversation は 1、Message は 2）。
_CONVERSATION_ARTIFACT_TYPE: Final[str] = "conversation"
_CONVERSATION_SCHEMA_MAJOR: Final[int] = 1
_MESSAGE_ARTIFACT_TYPE: Final[str] = "conversation-message"
_MESSAGE_SCHEMA_MAJOR: Final[int] = 2


def derive_conversation_hash(
    *, conversation_id: str, created_at: str, producer: str
) -> ContentHash:
    """作成時の不変メタだけから導出する（CMC-4-A）。

    **Message を入力に取らない。** 引数に Message が無いことが、
    「Message 追加で動かない」ことの根拠である。
    """
    if not conversation_id or not created_at or not producer:
        raise HarnessError(
            ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "conversation hash inputs must not be empty"
        )
    payload = {
        "conversation_id": conversation_id,
        "created_at": created_at,
        "hash_profile_version": _HASH_PROFILE_VERSION,
        "producer": producer,
    }
    return hash_canonical(
        payload,
        artifact_type=_CONVERSATION_ARTIFACT_TYPE,
        schema_major=_CONVERSATION_SCHEMA_MAJOR,
    )


def derive_message_content_hash(
    *, content_artifact_hash: ContentHash, role: MessageRole, sequence_number: int
) -> ContentHash:
    """本文 Hash・`role`・`sequence_number` から導出する（CMC-3-A）。

    **`message_id`／`conversation_id`／時刻／PID を入力に取らない**（不変条件#4）。
    引数がこの 3 つだけであることが、その根拠である。Hash 自身も取らない。
    """
    if sequence_number < 1:
        raise HarnessError(ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "sequence_number must be >= 1")
    payload = {
        "content_artifact_hash": str(content_artifact_hash),
        "hash_profile_version": _HASH_PROFILE_VERSION,
        "role": role.value,
        "sequence_number": sequence_number,
    }
    return hash_canonical(
        payload, artifact_type=_MESSAGE_ARTIFACT_TYPE, schema_major=_MESSAGE_SCHEMA_MAJOR
    )


@dataclass(frozen=True, slots=True)
class Conversation:
    """`Conversation@1.0.0`。

    **`message_ids` を持たない。** 持てば Message 追加のたびに親を書き換える
    ことになり、Append-only と両立しない（CMC-9-A 相当／設計書 §4.11）。

    **`message_count` を持たない。** 件数の正本が 2 か所になる（不変条件#18）。
    件数は Message 集合から導く。
    """

    SCHEMA_NAME: ClassVar[str] = "Conversation"

    conversation_id: str
    conversation_hash: ContentHash
    record_id: str
    created_at: str
    producer: str
    content_hash: ContentHash

    def __post_init__(self) -> None:
        for name in ("conversation_id", "record_id", "created_at", "producer"):
            if not getattr(self, name):
                raise HarnessError(
                    ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, f"{name} must not be empty"
                )
        expected = derive_conversation_hash(
            conversation_id=self.conversation_id,
            created_at=self.created_at,
            producer=self.producer,
        )
        if expected != self.conversation_hash:
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                "conversation_hash does not match its declared identity",
            )


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    """`ConversationMessage@2.0.0`。

    本文は Artifact CAS にあり、ここは `content_artifact_hash` で参照するだけである。
    **inline 本文 Field を持たない**（CMC-1-A／CMC-2-A）。

    **`token_count` を持たない。** Token 会計は `TokenBudgetPolicy` と
    `TokenProfileSnapshot` が持つ。
    """

    SCHEMA_NAME: ClassVar[str] = "ConversationMessage"

    message_id: str
    conversation_id: str
    role: MessageRole
    sequence_number: int
    content_artifact_hash: ContentHash
    record_id: str
    created_at: str
    producer: str
    content_hash: ContentHash

    def __post_init__(self) -> None:
        for name in ("message_id", "conversation_id", "record_id", "created_at", "producer"):
            if not getattr(self, name):
                raise HarnessError(
                    ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, f"{name} must not be empty"
                )
        if self.sequence_number < 1:
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "sequence_number must be >= 1"
            )
        expected = derive_message_content_hash(
            content_artifact_hash=self.content_artifact_hash,
            role=self.role,
            sequence_number=self.sequence_number,
        )
        if expected != self.content_hash:
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                "content_hash does not match body/role/sequence_number",
            )
