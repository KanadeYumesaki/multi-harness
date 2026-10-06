"""Codexの明示拒否と単なる中断を混同しない。"""

from attest_tool_free_cli import _codex_tool_refusals


def test_missing_tool_array_is_empty_but_null_is_invalid():
    assert _codex_tool_refusals([{}], ())[0]
    assert not _codex_tool_refusals([{"tools": None}], ())[0]
    assert not _codex_tool_refusals([{"tools": [{"type": "function", "name": "shell"}]}], ())[0]


def test_custom_patch_must_be_explicitly_denied_by_read_only_policy():
    part = {
        "type": "custom_tool_call_output",
        "call_id": "call_0",
        "output": (
            "patch rejected: writing is blocked by read-only sandbox; "
            "rejected by user approval settings"
        ),
    }
    assert _codex_tool_refusals([{"input": [part]}], ("apply_patch",))[1] == {"apply_patch"}
    for output in ["aborted", "success", "unsupported call: apply_patch", "permission denied"]:
        assert not _codex_tool_refusals(
            [{"input": [{**part, "output": output}]}], ("apply_patch",)
        )[1]
    assert not _codex_tool_refusals([{"input": [{**part, "call_id": "other"}]}], ("apply_patch",))[
        1
    ]


def test_unregistered_custom_patch_is_explicit_refusal():
    part = {
        "type": "custom_tool_call_output",
        "call_id": "call_0",
        "output": "unsupported custom tool call: apply_patch",
    }
    assert _codex_tool_refusals([{"input": [part]}], ("apply_patch",))[1] == {"apply_patch"}
