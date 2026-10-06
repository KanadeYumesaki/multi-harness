"""Explicit, deterministic history selection. Stored source artifacts are never changed."""

from __future__ import annotations

from typing import Any

from harness.domain.canonical import canonicalize
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_bytes
from harness.domain.workbench import history_hash_projection

MAX_VERIFIED_HISTORY_BYTES = 8 * 1024 * 1024


def validate_selection(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not set(value) <= {"mode", "recent_count"}:
        raise HarnessError(ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "invalid history selection")
    mode = value.get("mode")
    if mode == "full" and set(value) == {"mode"}:
        return {"mode": "full"}
    count = value.get("recent_count")
    if mode == "recent" and type(count) is int and 1 <= count <= 100:
        return {"mode": "recent", "recent_count": count}
    raise HarnessError(ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "invalid history selection")


def select_history(
    messages: list[dict[str, Any]],
    turns: list[dict[str, Any]],
    selection: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    excluded: list[dict[str, Any]] = []
    anchors: list[dict[str, Any]] = []
    if selection["mode"] == "full":
        selected_messages, selected_turns = messages, turns
    else:
        count = selection["recent_count"]
        mandatory = {
            index
            for index, message in enumerate(messages)
            if message["role"] in ("system", "developer")
        }
        if messages:
            mandatory.add(
                0
            )  # Keep the opening request; never discard declared constraints implicitly.
        chosen = mandatory | set(range(max(0, len(messages) - count), len(messages)))
        selected_messages = [m for i, m in enumerate(messages) if i in chosen]
        selected_turns = turns[-count:]
        for index, message in enumerate(messages):
            if index not in chosen:
                excluded.append(
                    {
                        "kind": "saved_message",
                        "sequence": message["sequence"],
                        "reason": "EXPLICIT_RECENT_HISTORY_SELECTION",
                        "original_hash": history_hash_projection(hash_bytes(canonicalize(message))),
                    }
                )
        omitted_turns = turns[:-count]
        if omitted_turns:
            first = omitted_turns[0]
            anchors.append(
                {
                    "initial_instruction": first["instruction"],
                    "initial_target": first["target_relative_path"],
                    "trust": "UNTRUSTED_REFERENCE_DATA",
                }
            )
        for turn in omitted_turns:
            excluded.append(
                {
                    "kind": "workbench_turn",
                    "sequence": turn["sequence"],
                    "reason": "EXPLICIT_RECENT_HISTORY_SELECTION",
                    "original_hash": history_hash_projection(hash_bytes(canonicalize(turn))),
                }
            )
    receipt: dict[str, Any] = {
        "policy": selection,
        "original_messages": len(messages),
        "selected_messages": len(selected_messages),
        "original_turns": len(turns),
        "selected_turns": len(selected_turns),
        "original_bytes": len(canonicalize({"local_messages": messages, "turns": turns})),
        "selected_bytes": len(
            canonicalize({"local_messages": selected_messages, "turns": selected_turns})
        ),
        "excluded": excluded,
        "mandatory_context": anchors,
        "summary_model_invocations": 0,
        "note": "原文は全保存。選ばなかった本文は送信せず、参照Hashを残す。AI要約は行わない。",
    }
    return selected_messages, selected_turns, receipt
