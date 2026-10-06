"""Parse official CLI envelopes without executing a process or trusting tool output."""

from __future__ import annotations

from harness.domain.code_proposal import CodeProposal, reject_response, strict_response_json
from harness.domain.workspace_task import TextArtifact


def parse_cli_response(provider: str, payload: bytes, *, maximum_bytes: int) -> CodeProposal:
    return CodeProposal.parse(
        _response_content(provider, payload, maximum_bytes), maximum_bytes=maximum_bytes
    )


def parse_cli_text_response(provider: str, payload: bytes, *, maximum_bytes: int) -> TextArtifact:
    return TextArtifact.parse(
        _response_content(provider, payload, maximum_bytes), maximum_bytes=maximum_bytes
    )


def _response_content(provider: str, payload: bytes, maximum_bytes: int) -> bytes:
    if not payload or len(payload) > maximum_bytes:
        raise reject_response()
    text: object
    if provider == "codex":
        text = _codex_text(payload, maximum_bytes)
    else:
        value = strict_response_json(payload, maximum_bytes=maximum_bytes)
        if not isinstance(value, dict):
            raise reject_response()
        if provider == "claude":
            if (
                value.get("type") != "result"
                or value.get("subtype") != "success"
                or value.get("is_error") is not False
                or value.get("permission_denials") not in (None, [])
            ):
                raise reject_response()
            text = value.get("result")
        elif provider == "gemini":
            if "error" in value:
                raise reject_response()
            text = value.get("response")
        else:
            raise reject_response()
    if not isinstance(text, str):
        raise reject_response()
    try:
        encoded = text.encode("utf-8")
    except UnicodeError:
        raise reject_response() from None
    return encoded


def _codex_text(payload: bytes, maximum_bytes: int) -> str:
    messages: list[str] = []
    stage = "INITIAL"
    disabled_code_mode_reported = False
    for line in payload.splitlines():
        event = strict_response_json(line, maximum_bytes=maximum_bytes)
        if not isinstance(event, dict):
            raise reject_response()
        kind = event.get("type")
        if kind == "thread.started" and stage == "INITIAL":
            stage = "THREAD"
        elif (
            kind == "item.completed"
            and stage == "THREAD"
            and not disabled_code_mode_reported
            and _disabled_code_mode_notice(event.get("item"))
        ):
            # 実CLIが明示無効化を通知する既知の起動診断だけ。実行中のerrorは拒否する。
            disabled_code_mode_reported = True
        elif kind == "turn.started" and stage == "THREAD":
            stage = "TURN"
        elif kind == "item.completed" and stage == "TURN":
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "agent_message":
                # A read-only sandbox alone does not prohibit reads or network tools.
                # Tool events are a policy violation, not usable response data.
                raise reject_response()
            text = item.get("text")
            if not isinstance(text, str):
                raise reject_response()
            messages.append(text)
        elif kind == "turn.completed" and stage == "TURN" and len(messages) == 1:
            stage = "COMPLETED"
        else:
            raise reject_response()
    if stage != "COMPLETED":
        raise reject_response()
    return messages[0]


def _disabled_code_mode_notice(item: object) -> bool:
    return (
        isinstance(item, dict)
        and item.get("type") == "error"
        and item.get("message")
        == (
            "Code Mode is unavailable because code-mode host is disabled. "
            "Code mode will fail closed; enable `features.code_mode_host` "
            "and install `codex-code-mode-host`."
        )
        and set(item) <= {"id", "type", "message"}
    )
