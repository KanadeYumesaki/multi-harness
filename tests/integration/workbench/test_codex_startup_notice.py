"""機能を無効化した起動通知と実行エラーを区別する。"""

import json

import pytest

from harness.domain.errors import HarnessError
from harness.infrastructure.provider.cli_response import parse_cli_response

NOTICE = {
    "type": "item.completed",
    "item": {
        "id": "item_0",
        "type": "error",
        "message": "Code Mode is unavailable because code-mode host is disabled. "
        "Code mode will fail closed; enable `features.code_mode_host` "
        "and install `codex-code-mode-host`.",
    },
}


def events():
    return [
        {"type": "thread.started"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": json.dumps({"replacement_text": "# 合成コメント\n"}),
            },
        },
        {"type": "turn.completed"},
    ]


def parse(rows):
    return parse_cli_response(
        "codex", "\n".join(json.dumps(x) for x in rows).encode(), maximum_bytes=10000
    )


def test_exact_disabled_notice_before_turn_is_supported():
    rows = events()
    rows.insert(1, NOTICE)
    assert parse(rows) is not None


@pytest.mark.parametrize("mode", ["during_turn", "duplicate", "other_error", "extra_field"])
def test_notice_cannot_hide_errors_or_extra_events(mode):
    rows = events()
    notice = json.loads(json.dumps(NOTICE))
    if mode == "other_error":
        notice["item"]["message"] = "network error"
    if mode == "extra_field":
        notice["item"]["command"] = "hidden"
    rows.insert(2 if mode == "during_turn" else 1, notice)
    if mode == "duplicate":
        rows.insert(1, NOTICE)
    with pytest.raises(HarnessError):
        parse(rows)
