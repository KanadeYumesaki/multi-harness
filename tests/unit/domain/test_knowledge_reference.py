"""Bounded references preserve selected bytes; they are not system instructions."""

import json

import pytest

from harness.domain.errors import HarnessError
from harness.domain.hashing import hash_bytes
from harness.domain.knowledge_reference import KnowledgeReference


def test_reference_hash_is_deterministic_and_covers_provenance() -> None:
    reference = KnowledgeReference("chatgpt_memory", "合成", "日本語\r\nReference")
    assert reference.preview_hash == hash_bytes(reference.body())
    assert json.loads(reference.body())["content"] == "日本語\r\nReference"
    assert (
        reference.preview_hash
        == KnowledgeReference("chatgpt_memory", "合成", "日本語\r\nReference").preview_hash
    )
    assert (
        reference.preview_hash
        != KnowledgeReference("chatgpt_conversation", "合成", "日本語\r\nReference").preview_hash
    )


@pytest.mark.parametrize(
    "content,accepted",
    [
        ("x" * 65536, True),
        ("x" * 65537, False),
        ("あ" * 21845, True),
        ("あ" * 21846, False),
        ("\ud800", False),
    ],
)
def test_reference_byte_limit_and_invalid_unicode(content: str, accepted: bool) -> None:
    if accepted:
        assert KnowledgeReference("chatgpt_memory", "合成", content).body()
    else:
        with pytest.raises(HarnessError):
            KnowledgeReference("chatgpt_memory", "合成", content)
