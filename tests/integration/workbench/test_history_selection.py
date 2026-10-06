"""Selection changes transmission only, never source history or unresolved-effect checks."""

from __future__ import annotations

import json

import pytest

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.history_selection import select_history, validate_selection

from .conftest import WorkbenchEnv

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "value",
    [
        {"mode": "recent", "recent_count": 0},
        {"mode": "recent", "recent_count": True},
        {"mode": "recent", "recent_count": 101},
        {"mode": "unknown"},
        {"mode": "full", "extra": 1},
    ],
)
def test_invalid_history_choices_fail_closed(value: object) -> None:
    with pytest.raises(HarnessError):
        validate_selection(value)


def test_selection_retains_anchors_and_records_every_exclusion() -> None:
    messages = [
        {"sequence": i, "role": "system" if i == 2 else "user", "body": str(i)} for i in range(1, 8)
    ]
    turns = [
        {"sequence": i, "instruction": "initial constraint", "target_relative_path": "x.py"}
        for i in range(1, 8)
    ]
    before = json.dumps([messages, turns], sort_keys=True)
    selected, recent, receipt = select_history(
        messages, turns, {"mode": "recent", "recent_count": 1}
    )
    assert [m["sequence"] for m in selected] == [1, 2, 7]
    assert [t["sequence"] for t in recent] == [7]
    assert len(receipt["excluded"]) == 10
    assert receipt["mandatory_context"][0]["initial_instruction"] == "initial constraint"
    assert receipt["summary_model_invocations"] == 0
    assert json.dumps([messages, turns], sort_keys=True) == before


def test_selection_saves_bytes_and_old_sources_stay_intact(
    workbench: WorkbenchEnv,
) -> None:
    parent = None
    original: list[dict[str, object]] = []
    # Drafted turns are references only; none of these operations send anything.
    for _ in range(6):
        parent = workbench.gateway.create_session(
            provider_id="codex",
            model_id="fake-model",
            relative_path="hello.py",
            instruction="整形してください。",
            parent_session_id=parent and parent["session_id"],
            history_selection={"mode": "recent", "recent_count": 1},
        )
        original.append(workbench.gateway._service.inspect(parent["session_id"]))
    full = workbench.gateway.context_preview(
        parent_session_id=parent["session_id"],
        conversation_id=None,
        history_selection={"mode": "full"},
    )
    recent = workbench.gateway.context_preview(
        parent_session_id=parent["session_id"],
        conversation_id=None,
        history_selection={"mode": "recent", "recent_count": 1},
    )
    assert full["binding"]["turn_count"] == 6
    assert recent["binding"]["turn_count"] == 1
    assert len(recent["binding"]["selection_receipt"]["excluded"]) == 5
    assert recent["binding"]["source_binding_hash"] == full["binding"]["source_binding_hash"]
    assert recent["binding"]["history_bytes"] < full["binding"]["history_bytes"]
    assert [workbench.gateway._service.inspect(d["session_id"]) for d in original] == original
    changed = workbench.gateway.create_session(
        provider_id="codex",
        model_id="fake-model",
        relative_path="hello.py",
        instruction="整形してください。",
        parent_session_id=parent["session_id"],
        history_selection={"mode": "recent", "recent_count": 1},
        important_notes="互換性を維持してください。",
    )
    confirmation = workbench.gateway.confirmation(changed["session_id"])
    request = json.loads(confirmation["request_payload_text"])
    assert request["important_notes"] == "互換性を維持してください。"
    assert confirmation["context_budget"]["request_bytes"] == len(
        confirmation["request_payload_text"].encode()
    )
    assert confirmation["context_budget"]["token_count_assurance"] == "UNKNOWN"
    assert workbench.runner.calls == []


def test_excluded_unknown_turn_still_blocks_continuation(workbench: WorkbenchEnv) -> None:
    service = workbench.gateway._service
    older = workbench.gateway.create_session(
        provider_id="codex",
        model_id="fake-model",
        relative_path="hello.py",
        instruction="first",
    )
    newer = workbench.gateway.create_session(
        provider_id="codex",
        model_id="fake-model",
        relative_path="hello.py",
        instruction="second",
        parent_session_id=older["session_id"],
    )
    original = service.inspect

    def unknown(session_id: str) -> dict[str, object]:
        value = original(session_id)
        if session_id == older["session_id"]:
            value["state"] = "SEND_UNKNOWN"
        return value

    service.inspect = unknown
    with pytest.raises(HarnessError) as error:
        workbench.gateway.create_session(
            provider_id="codex",
            model_id="fake-model",
            relative_path="hello.py",
            instruction="third",
            parent_session_id=newer["session_id"],
            history_selection={"mode": "recent", "recent_count": 1},
        )
    assert error.value.code is ErrorCode.UNRECONCILED_EFFECT_PRESENT
    assert workbench.runner.calls == []


def test_large_full_history_is_refused_but_explicit_recent_selection_fits(
    workbench: WorkbenchEnv,
) -> None:
    (workbench.worktree / "hello.py").write_text("# synthetic context\n" * 1000)
    parent = None
    for _ in range(6):
        parent = workbench.gateway.create_session(
            provider_id="codex",
            model_id="fake-model",
            relative_path="hello.py",
            instruction="履歴参照",
            parent_session_id=parent and parent["session_id"],
            history_selection={"mode": "recent", "recent_count": 1},
        )
    with pytest.raises(HarnessError) as error:
        workbench.gateway.create_session(
            provider_id="codex",
            model_id="fake-model",
            relative_path="hello.py",
            instruction="続き",
            parent_session_id=parent["session_id"],
            history_selection={"mode": "full"},
        )
    assert error.value.code is ErrorCode.CONTEXT_BUDGET_EXCEEDED
    selected = workbench.gateway.create_session(
        provider_id="codex",
        model_id="fake-model",
        relative_path="hello.py",
        instruction="続き",
        parent_session_id=parent["session_id"],
        history_selection={"mode": "recent", "recent_count": 1},
    )
    confirmation = workbench.gateway.confirmation(selected["session_id"])
    assert (
        confirmation["context_budget"]["request_bytes"]
        <= confirmation["context_budget"]["maximum_request_bytes"]
    )
    assert confirmation["handoff"]["selection_receipt"]["original_turns"] == 6
    assert confirmation["handoff"]["selection_receipt"]["selected_turns"] == 1
    assert workbench.runner.calls == []
