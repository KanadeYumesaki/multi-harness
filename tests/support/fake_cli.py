#!/usr/bin/env python3
"""試験用の偽 CLI。**実 Process として起動して境界を試す。**

関数 Mock では、argv・env・cwd・stdin・終了 Code・出力上限・timeout・子 Process の
残留といった Process 境界を一つも踏めない。だからこの偽 CLI は本物と同じく
`subprocess` から起動され、Harness が渡した内容を観測して返す。

`FAKE_CLI_MODE` で振る舞いを切り替える。既定は正常応答である。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _read_request() -> dict[str, object]:
    payload = sys.stdin.buffer.read()
    document = json.loads(payload.decode("utf-8"))
    return document if isinstance(document, dict) else {}


def _proposal(document: dict[str, object]) -> str:
    source = document.get("current_file_text")
    text = source if isinstance(source, str) else ""
    replacement = os.environ.get("FAKE_CLI_REPLACEMENT")
    if replacement is not None:
        return replacement
    return "# 日本語の説明コメント（偽 CLI が付けた合成の内容）\n" + text


def _envelope(provider: str, text: str, *, text_task: bool = False) -> str:
    body = json.dumps(
        {"result_text" if text_task else "replacement_text": text}, ensure_ascii=False
    )
    if provider == "codex":
        return "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": "fake-thread"}),
                json.dumps({"type": "turn.started"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"id": "item_0", "type": "agent_message", "text": body},
                    }
                ),
                json.dumps({"type": "turn.completed"}),
            )
        )
    if provider == "claude":
        return json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "permission_denials": [],
                "result": body,
            }
        )
    return json.dumps({"response": body})


#: `codex features list` を真似るための既知の機能名。
#:
#: 本物の出力から採った名前だけを並べる。`unified_exec` は本物と同じく
#: `--disable` を無視して `true` のままにする。
_KNOWN_FEATURES: tuple[str, ...] = (
    "apps",
    "auth_elicitation",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "code_mode_host",
    "computer_use",
    "fast_mode",
    "goals",
    "hooks",
    "image_generation",
    "in_app_browser",
    "in_app_local_automation",
    "mentions_v2",
    "multi_agent",
    "plugin_sharing",
    "plugins",
    "remote_plugin",
    "shell_snapshot",
    "shell_tool",
    "skill_mcp_dependency_install",
    "skill_search",
    "sleep_tool",
    "sqlite",
    "steer",
    "tool_suggest",
    "unified_exec",
    "unified_exec_zsh_fork",
    "view_image",
    "workspace_dependencies",
)

#: `--disable` を無視する機能。本物の Codex の実測に合わせる。
_STICKY_FEATURES: frozenset[str] = frozenset({"unified_exec"})


def _features_list(argv: list[str]) -> int:
    """`features list` を真似る。**未知の名前は本物と同じく非 0 で落ちる。**"""
    disabled: set[str] = set()
    for index, item in enumerate(argv):
        if item != "--disable" or index + 1 >= len(argv):
            continue
        name = argv[index + 1]
        if name not in _KNOWN_FEATURES:
            sys.stderr.write(f"Error: Unknown feature flag: {name}\n")
            return 1
        disabled.add(name)
    forced = json.loads(os.environ.get("FAKE_CLI_FEATURE_OVERRIDE", "{}"))
    for name in _KNOWN_FEATURES:
        state = name not in disabled or name in _STICKY_FEATURES
        if name in forced:
            state = bool(forced[name])
        sys.stdout.write(f"{name:<45} stable  {'true' if state else 'false'}\n")
    return 0


def main() -> int:
    mode = os.environ.get("FAKE_CLI_MODE", "OK")
    provider = os.environ.get("FAKE_CLI_PROVIDER", "codex")
    if "features" in sys.argv[1:] and "list" in sys.argv[1:]:
        return _features_list(sys.argv[1:])
    if mode == "OBSERVE_ARGV":
        sys.stdout.write(
            json.dumps(
                {
                    "argv": sys.argv[1:],
                    "env": dict(os.environ),
                    "cwd": os.getcwd(),
                    "stdin_bytes": len(sys.stdin.buffer.read()),
                }
            )
        )
        return 0
    if mode == "HANG":
        sys.stdin.buffer.read()
        time.sleep(3600)
        return 0
    if mode == "SIGTERM_IGNORE":
        # TERM を無視して居座る。**親 wait の成功を後片付け完了と呼べないことを試す。**
        import signal

        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        sys.stdin.buffer.read()
        time.sleep(3600)
        return 0
    if mode == "PROBE_ESCAPE":
        # 承認した範囲の外を読もうとする。Landlock 境界が効いていれば読めない。
        document = _read_request()
        target = os.environ.get("FAKE_CLI_PROBE_PATH", "")
        try:
            with open(target, "rb") as handle:
                observed = "ALLOWED:" + handle.read(64).decode("utf-8", "replace")
        except OSError as error:
            observed = f"DENIED({error.errno})"
        sys.stdout.write(_envelope(provider, observed))
        return 0
    if mode == "SLOW":
        document = _read_request()
        time.sleep(6)
        sys.stdout.write(
            _envelope(
                provider,
                _proposal(document),
                text_task=document.get("contract") == "ai-workspace-text-request/1",
            )
        )
        return 0
    if mode == "DOUBLE_FORK":
        # 二重 fork で親を失った孫を残す。Session は離れないが、`ppid` は 1 になる。
        read_end, write_end = os.pipe()
        first = os.fork()
        if first == 0:
            os.close(read_end)
            if os.fork() != 0:
                os._exit(0)
            try:
                os.write(write_end, str(os.getpid()).encode("ascii"))
            except OSError:
                pass
            os.close(write_end)
            time.sleep(3600)
            os._exit(0)
        os.close(write_end)
        os.waitpid(first, 0)
        started = b""
        try:
            started = os.read(read_end, 32)
        except OSError:
            pass
        os.close(read_end)
        sys.stdin.buffer.read()
        text = started.decode("ascii", "replace").strip()
        sys.stdout.write("orphaned " + text if text else "not-orphaned")
        sys.stdout.flush()
        return 0
    if mode == "DETACH_CHILD":
        # **Process Group からも Session からも離脱する子** を残して親が終わる。
        # Group だけを見張っていた頃はこれを取り逃がした（R2-A）。
        read_end, write_end = os.pipe()
        first = os.fork()
        if first == 0:
            os.close(read_end)
            os.setsid()
            if os.fork() != 0:
                os._exit(0)
            # 自分の PID を先に伝えてから stdio を手放す。呼出側は封じ込めが
            # 効かない構成の試験で、**自分が起こしたこの子だけ** を回収する。
            try:
                os.write(write_end, str(os.getpid()).encode("ascii"))
            except OSError:
                pass
            os.close(write_end)
            for descriptor in (0, 1, 2):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            time.sleep(3600)
            os._exit(0)
        os.close(write_end)
        os.waitpid(first, 0)
        started = b""
        try:
            started = os.read(read_end, 32)
        except OSError:
            pass
        os.close(read_end)
        sys.stdin.buffer.read()
        text = started.decode("ascii", "replace").strip()
        sys.stdout.write("detached " + text if text else "not-detached")
        sys.stdout.flush()
        return 0
    if mode == "LEAK_CHILD":
        # 孫が Pipe を掴んだまま残る形。親だけ先に終える。
        child = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-c", "import time; time.sleep(3600)"],
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        record = os.environ.get("FAKE_CLI_CHILD_RECORD")
        if record is not None:
            stat = Path(f"/proc/{child.pid}/stat").read_text()
            tail = stat[stat.rfind(")") + 2 :].split()
            Path(record).write_text(
                json.dumps(
                    {
                        "pid": child.pid,
                        "uid": os.getuid(),
                        "start_ticks": int(tail[19]),
                    }
                )
            )
        sys.stdin.buffer.read()
        return 0
    if mode == "FLOOD_STDOUT":
        sys.stdin.buffer.read()
        chunk = "x" * 65536
        while True:
            sys.stdout.write(chunk)
            sys.stdout.flush()
    if mode == "FLOOD_STDERR":
        sys.stdin.buffer.read()
        chunk = "e" * 65536
        while True:
            sys.stderr.write(chunk)
            sys.stderr.flush()
    document = _read_request()
    text_task = document.get("contract") == "ai-workspace-text-request/1"
    if mode == "NON_ZERO":
        sys.stdout.write(_envelope(provider, _proposal(document), text_task=text_task))
        return 3
    if mode == "GARBAGE":
        sys.stdout.write("これは JSON ではない。```json\n{}\n```")
        return 0
    if mode == "TOOL_EVENT":
        sys.stdout.write(
            "\n".join(
                (
                    json.dumps({"type": "thread.started", "thread_id": "fake-thread"}),
                    json.dumps({"type": "turn.started"}),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"id": "item_0", "type": "command_execution", "text": "ls"},
                        }
                    ),
                    json.dumps({"type": "turn.completed"}),
                )
            )
        )
        return 0
    if mode == "STDERR_NOISE":
        sys.stderr.write("FDE-HARNESS-CANARY-STDERRLEAK1 diagnostic noise\n")
    sys.stdout.write(_envelope(provider, _proposal(document), text_task=text_task))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
