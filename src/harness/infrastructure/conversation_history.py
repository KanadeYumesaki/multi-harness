"""Read every saved conversation role through verified repositories and CAS.

This adapter does not import external CLI sessions or invent assistant reasoning.
Incomplete, invalid or oversized history is rejected, never silently shortened.
"""

from __future__ import annotations

from typing import Any

from harness.domain.canonical import canonicalize
from harness.domain.conversation import derive_conversation_hash, derive_message_content_hash
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_bytes, hash_canonical
from harness.ports.artifact_store import ArtifactStorePort
from harness.ports.cli_workbench import SavedConversationHistory
from harness.ports.conversation_store import ConversationMessageStorePort, ConversationStorePort


class SavedConversationReader:
    def __init__(
        self,
        conversations: ConversationStorePort,
        messages: ConversationMessageStorePort,
        artifacts: ArtifactStorePort,
    ) -> None:
        self._conversations = conversations
        self._messages = messages
        self._artifacts = artifacts

    def read_history(self, conversation_id: str, *, maximum_bytes: int) -> SavedConversationHistory:
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "source conversation was not found")
        expected = derive_conversation_hash(
            conversation_id=conversation.conversation_id,
            created_at=conversation.created_at,
            producer=conversation.producer,
        )
        if conversation.conversation_hash != expected or conversation.content_hash != expected:
            raise HarnessError(ErrorCode.ARTIFACT_CONTENT_CONFLICT, "conversation metadata changed")
        rows = self._messages.list_for_conversation(conversation_id)
        messages: list[dict[str, Any]] = []
        references: list[dict[str, object]] = []
        total = 0
        for sequence, message in enumerate(rows, 1):
            if message.conversation_id != conversation_id or message.sequence_number != sequence:
                raise HarnessError(
                    ErrorCode.EVENT_ORDER_VIOLATION,
                    "conversation history is incomplete or unordered",
                )
            expected_content = derive_message_content_hash(
                content_artifact_hash=message.content_artifact_hash,
                role=message.role,
                sequence_number=message.sequence_number,
            )
            if message.content_hash != expected_content:
                raise HarnessError(ErrorCode.ARTIFACT_CONTENT_CONFLICT, "message metadata changed")
            self._artifacts.verify(message.content_artifact_hash).raise_if_repair_required()
            body = self._artifacts.get(message.content_artifact_hash)
            if hash_bytes(body) != message.content_artifact_hash:
                raise HarnessError(ErrorCode.ARTIFACT_CONTENT_CONFLICT, "message content changed")
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                raise HarnessError(
                    ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "history message is not valid UTF-8"
                ) from None
            if "\x00" in text:
                raise HarnessError(
                    ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "history message contains NUL"
                )
            entry: dict[str, Any] = {"sequence": sequence, "role": message.role.value, "body": text}
            total += len(canonicalize(entry))
            if total > maximum_bytes:
                raise HarnessError(
                    ErrorCode.CONTEXT_BUDGET_EXCEEDED,
                    "full conversation exceeds the request limit; no messages were omitted",
                )
            messages.append(entry)
            references.append(
                {"message_id": message.message_id, "content_hash": str(message.content_hash)}
            )
        return SavedConversationHistory(
            messages=tuple(messages),
            source_hash=hash_canonical(
                {"conversation_hash": str(expected), "messages": references},
                artifact_type="saved-conversation-history",
                schema_major=1,
            ),
        )
