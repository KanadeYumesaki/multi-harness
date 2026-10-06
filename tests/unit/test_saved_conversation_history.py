"""Malformed or incomplete local history is rejected before it becomes model context."""

from __future__ import annotations

from copy import copy
from types import SimpleNamespace
from typing import Any

import pytest

from harness.domain.context_budget import MessageRole
from harness.domain.conversation import (
    Conversation,
    ConversationMessage,
    derive_conversation_hash,
    derive_message_content_hash,
)
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_bytes
from harness.infrastructure.conversation_history import SavedConversationReader


def reader_for(
    body: bytes, *, artifact_body: bytes | None = None
) -> tuple[SavedConversationReader, Any, Any]:
    digest = hash_bytes(body)
    conversation_id = "00000000-0000-0000-0000-000000000001"
    now = "2026-10-02T00:00:00Z"
    conversation_hash = derive_conversation_hash(
        conversation_id=conversation_id, created_at=now, producer="synthetic"
    )
    conversation = Conversation(
        conversation_id=conversation_id,
        conversation_hash=conversation_hash,
        record_id="conversation-record",
        created_at=now,
        producer="synthetic",
        content_hash=conversation_hash,
    )
    message = ConversationMessage(
        message_id="message",
        conversation_id=conversation_id,
        role=MessageRole.USER_TASK,
        sequence_number=1,
        content_artifact_hash=digest,
        record_id="message-record",
        created_at=now,
        producer="synthetic",
        content_hash=derive_message_content_hash(
            content_artifact_hash=digest, role=MessageRole.USER_TASK, sequence_number=1
        ),
    )
    conversations = SimpleNamespace(get=lambda _: conversation)
    messages = SimpleNamespace(list_for_conversation=lambda _: (message,))
    artifacts = SimpleNamespace(
        verify=lambda _: SimpleNamespace(raise_if_repair_required=lambda: None),
        get=lambda _: body if artifact_body is None else artifact_body,
    )
    return SavedConversationReader(conversations, messages, artifacts), conversation, message


@pytest.mark.parametrize("body", [bytes([255]), b"zero" + bytes([0]) + b"byte"])
def test_invalid_utf8_or_nul_is_rejected_without_body_in_error(body: bytes) -> None:
    reader, conversation, _ = reader_for(body)
    with pytest.raises(HarnessError) as error:
        reader.read_history(conversation.conversation_id, maximum_bytes=4096)
    assert error.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION
    assert "zero" not in str(error.value)


def test_changed_artifact_is_rejected() -> None:
    reader, conversation, _ = reader_for(b"original", artifact_body=b"changed")
    with pytest.raises(HarnessError) as error:
        reader.read_history(conversation.conversation_id, maximum_bytes=4096)
    assert error.value.code is ErrorCode.ARTIFACT_CONTENT_CONFLICT


def test_source_identity_drift_is_rejected() -> None:
    reader, conversation, _ = reader_for(b"body")
    broken = copy(conversation)
    object.__setattr__(broken, "content_hash", hash_bytes(b"changed"))
    reader._conversations = SimpleNamespace(get=lambda _: broken)
    with pytest.raises(HarnessError) as error:
        reader.read_history(conversation.conversation_id, maximum_bytes=4096)
    assert error.value.code is ErrorCode.ARTIFACT_CONTENT_CONFLICT


def test_full_history_budget_rejects_without_truncating() -> None:
    reader, conversation, _ = reader_for(b"all content must fit")
    with pytest.raises(HarnessError) as error:
        reader.read_history(conversation.conversation_id, maximum_bytes=1)
    assert error.value.code is ErrorCode.CONTEXT_BUDGET_EXCEEDED


def test_missing_artifact_validation_propagates() -> None:
    reader, conversation, _ = reader_for(b"body")

    def missing(_: Any) -> Any:
        raise HarnessError(ErrorCode.STORAGE_WRITE_FAILED, "missing artifact")

    reader._artifacts.verify = missing
    with pytest.raises(HarnessError) as error:
        reader.read_history(conversation.conversation_id, maximum_bytes=4096)
    assert error.value.code is ErrorCode.STORAGE_WRITE_FAILED


def test_reference_fingerprint_changes_on_append_but_message_ids_are_not_sent() -> None:
    reader, conversation, message = reader_for(b"original")
    first = reader.read_history(conversation.conversation_id, maximum_bytes=4096)
    second = copy(message)
    object.__setattr__(second, "sequence_number", 2)
    object.__setattr__(second, "message_id", "second-message")
    object.__setattr__(
        second,
        "content_hash",
        derive_message_content_hash(
            content_artifact_hash=second.content_artifact_hash, role=second.role, sequence_number=2
        ),
    )
    reader._messages.list_for_conversation = lambda _: (message, second)
    next_history = reader.read_history(conversation.conversation_id, maximum_bytes=4096)
    assert first.source_hash != next_history.source_hash
    assert [m["body"] for m in next_history.messages] == ["original", "original"]
    assert all("message_id" not in m and "created_at" not in m for m in next_history.messages)
