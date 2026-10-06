"""Untrusted CLI output and non-retryable remote invocation contract."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from harness.domain.cli_invocation import CliInvocationJournal, InvocationState
from harness.domain.code_proposal import CodeProposal
from harness.domain.errors import HarnessError
from harness.domain.hashing import hash_bytes
from harness.infrastructure.provider.cli_response import parse_cli_response


def proposal(text: str = "print(1)\n") -> str:
    return json.dumps({"replacement_text": text})


def envelope(provider: str, text: str) -> bytes:
    if provider == "claude":
        return json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "result": text}
        ).encode()
    if provider == "gemini":
        return json.dumps({"response": text, "stats": {}}).encode()
    return b"\n".join(
        json.dumps(event).encode()
        for event in [
            {"type": "thread.started", "thread_id": "example"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
            {"type": "turn.completed", "usage": {}},
        ]
    )


@pytest.mark.parametrize("provider", ["codex", "claude", "gemini"])
def test_complete_response_retains_exact_file_bytes(provider: str) -> None:
    text = "# 日本語\r\nprint(1)\n"
    result = parse_cli_response(provider, envelope(provider, proposal(text)), maximum_bytes=4096)
    assert result.replacement == text.encode()
    assert result.content_hash == hash_bytes(text.encode())


@pytest.mark.parametrize("text", ["", "a", "a\r\nb\n"])
def test_replacement_is_not_trimmed(text: str) -> None:
    assert (
        CodeProposal.parse(proposal(text).encode(), maximum_bytes=100).replacement == text.encode()
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b"[]",
        b'{"replacement_text":null}',
        b'{"replacement_text":1}',
        b'{"replacement_text":"x","path":"../../other"}',
        b'{"replacement_text":"a","replacement_text":"b"}',
        b'{"replacement_text":NaN}',
        b'{"replacement_text":"\\ud800"}',
        b'{"replacement_text":"\\u0000"}',
        b"\xff",
        b'```json\n{"replacement_text":"x"}\n```',
    ],
)
def test_ambiguous_or_command_shaped_result_rejected(payload: bytes) -> None:
    with pytest.raises(HarnessError):
        CodeProposal.parse(payload, maximum_bytes=1024)


def test_size_limit_is_in_bytes_and_exact_boundary_allowed() -> None:
    payload = proposal("日本語").encode()
    assert CodeProposal.parse(payload, maximum_bytes=len(payload)).replacement == "日本語".encode()
    with pytest.raises(HarnessError):
        CodeProposal.parse(payload, maximum_bytes=len(payload) - 1)


@pytest.mark.parametrize("provider", ["codex", "claude", "gemini"])
def test_outer_response_limit_precedes_decoding(provider: str) -> None:
    payload = envelope(provider, proposal())
    with pytest.raises(HarnessError):
        parse_cli_response(provider, payload, maximum_bytes=len(payload) - 1)


@pytest.mark.parametrize(
    "event",
    [
        {"type": "turn.failed", "error": {"message": "untrusted-diagnostic"}},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "id"}},
        {"type": "item.completed", "item": {"type": "mcp_tool_call"}},
        {"type": "item.completed", "item": {"type": "file_change"}},
    ],
)
def test_codex_tool_or_error_event_never_becomes_a_proposal(event: dict[str, object]) -> None:
    good = envelope("codex", proposal()).splitlines()
    payload = b"\n".join(good[:2] + [json.dumps(event).encode()] + good[2:])
    with pytest.raises(HarnessError) as caught:
        parse_cli_response("codex", payload, maximum_bytes=4096)
    assert "untrusted-diagnostic" not in str(caught.value)


@pytest.mark.parametrize("mutation", ["truncate", "repeat", "after_complete"])
def test_codex_requires_one_complete_turn(mutation: str) -> None:
    lines = envelope("codex", proposal()).splitlines()
    if mutation == "truncate":
        lines.pop()
    elif mutation == "repeat":
        lines.insert(3, lines[2])
    else:
        lines.append(lines[2])
    with pytest.raises(HarnessError):
        parse_cli_response("codex", b"\n".join(lines), maximum_bytes=4096)


@pytest.mark.parametrize(
    "changes",
    [
        {"is_error": True},
        {"is_error": 0},
        {"subtype": "error_max_turns"},
        {"permission_denials": [{"tool_name": "Read"}]},
    ],
)
def test_claude_errors_and_denied_tools_are_rejected(changes: dict[str, object]) -> None:
    document = json.loads(envelope("claude", proposal()))
    document.update(changes)
    with pytest.raises(HarnessError):
        parse_cli_response("claude", json.dumps(document).encode(), maximum_bytes=4096)


def test_gemini_error_even_with_response_is_rejected() -> None:
    with pytest.raises(HarnessError):
        parse_cli_response(
            "gemini", json.dumps({"response": proposal(), "error": {}}).encode(), maximum_bytes=4096
        )


def journal() -> CliInvocationJournal:
    return CliInvocationJournal(
        "call", hash_bytes(b"plan"), hash_bytes(b"request"), hash_bytes(b"runtime")
    )


def test_remote_response_hash_is_captured_only_after_attempt() -> None:
    prepared = journal()
    attempted = prepared.attempted()
    completed = attempted.captured(hash_bytes(b"actual response"))
    assert prepared.state is InvocationState.PREPARED_DURABLE
    assert prepared.response_hash is None
    assert completed.response_hash == hash_bytes(b"actual response")
    assert completed.store_version == 2


@pytest.mark.parametrize("state", ["prepared", "attempted", "captured", "unknown"])
def test_no_retry_or_skipped_invocation_stages(state: str) -> None:
    record = journal()
    if state != "prepared":
        record = record.attempted()
    if state == "captured":
        record = record.captured(hash_bytes(b"actual"))
    elif state == "unknown":
        record = record.unknown()
    if state == "prepared":
        with pytest.raises(HarnessError):
            record.captured(hash_bytes(b"not observed"))
    else:
        with pytest.raises(HarnessError):
            record.attempted()
    if state in ("captured", "unknown"):
        with pytest.raises(HarnessError):
            record.captured(hash_bytes(b"another"))


def test_contradictory_persisted_journal_is_rejected() -> None:
    with pytest.raises(ValueError):
        replace(journal(), response_hash=hash_bytes(b"unobserved"))
    with pytest.raises(ValueError):
        replace(journal(), state=InvocationState.RESPONSE_CAPTURED)
