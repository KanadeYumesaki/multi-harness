"""Text tasks have an output contract separate from file replacement."""

from __future__ import annotations

import json

import pytest

from harness.domain.chatgpt_request import logical_request, responses_request
from harness.domain.code_proposal import CodeProposal
from harness.domain.errors import HarnessError
from harness.domain.workbench import WorkbenchLimits
from harness.domain.workspace_task import (
    TextArtifact,
    build_text_request,
    task_catalog,
    validate_task,
)
from harness.infrastructure.provider.cli_response import parse_cli_text_response


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"replacement_text": "wrong contract"},
        {"result_text": ""},
        {"result_text": " \n"},
        {"result_text": None},
        {"result_text": ["text"]},
        {"result_text": "x", "extra": True},
        {"result_text": "\u0000"},
    ],
)
def test_text_artifact_rejects_wrong_or_empty_output(value: object) -> None:
    with pytest.raises(HarnessError):
        TextArtifact.parse(json.dumps(value).encode(), maximum_bytes=1024)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"result_text":"one","result_text":"two"}',
        b'{"result_text":NaN}',
        b"\xff",
        b"\x00",
        b'{"result_text":"\\ud800"}',
    ],
)
def test_text_artifact_uses_strict_json(payload: bytes) -> None:
    with pytest.raises(HarnessError):
        TextArtifact.parse(payload, maximum_bytes=1024)


def test_output_bytes_are_preserved_and_file_parser_is_unchanged() -> None:
    value = "  # 草案\n\n事実と推測を分ける。\n"
    payload = json.dumps({"result_text": value}, ensure_ascii=False).encode()
    assert TextArtifact.parse(payload, maximum_bytes=1024).content == value.encode()
    with pytest.raises(HarnessError):
        CodeProposal.parse(payload, maximum_bytes=1024)
    with pytest.raises(HarnessError):
        TextArtifact.parse(payload, maximum_bytes=len(payload) - 1)


@pytest.mark.parametrize("provider", ["codex", "claude", "gemini"])
def test_all_cli_envelopes_parse_text_without_accepting_code(provider: str) -> None:
    from tests.support.fake_cli import _envelope

    payload = _envelope(provider, "# 合成文章\n", text_task=True).encode()
    assert (
        parse_cli_text_response(provider, payload, maximum_bytes=2048).content
        == "# 合成文章\n".encode()
    )
    with pytest.raises(HarnessError):
        parse_cli_text_response(provider, _envelope(provider, "file").encode(), maximum_bytes=2048)


def test_catalog_and_http_wire_bind_the_text_task_rules() -> None:
    for purpose in task_catalog():
        if purpose["task_kind"] == "file_edit":
            continue
        request = build_text_request(
            provider_id="chatgpt",
            model_id="fixture-model",
            task_kind=purpose["task_kind"],
            instruction="提供資料をもとに作成",
            reference_text="条件: 合成",
            output_format="markdown",
            limits=WorkbenchLimits(),
            conversation_history=None,
        )
        assert "target_relative_path" not in request
        assert "current_file_text" not in request
        wire = responses_request("fixture-model", request)
        assert logical_request(wire) == request
        wire["instructions"] = "unreviewed instructions"
        with pytest.raises(HarnessError):
            logical_request(wire)


@pytest.mark.parametrize(
    "kind,format,reference",
    [
        ("unknown", "text", ""),
        ("writing", "html", ""),
        ("writing", "text", "\x00"),
        ("writing", "text", "\ud800"),
    ],
)
def test_purpose_and_reference_fail_closed(kind: str, format: str, reference: str) -> None:
    with pytest.raises(HarnessError):
        validate_task(kind, format, reference, WorkbenchLimits())


def test_reference_byte_limit_has_an_exact_boundary() -> None:
    limits = WorkbenchLimits(max_source_bytes=6)
    assert validate_task("summary", "text", "日本", limits) == "日本".encode()
    with pytest.raises(HarnessError):
        validate_task("summary", "text", "日本語", limits)
