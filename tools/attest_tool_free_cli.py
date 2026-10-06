"""実CLIを外部通信不能の環境で検査する。実資格情報は渡さない。"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any


def attest_claude(argv: tuple[str, ...]) -> dict[str, Any]:
    """CLIのtool registryと不正tool要求の拒否を、偽の応答で確認する。"""
    try:
        result = subprocess.run(  # noqa: S603 - fixed local probe, explicit argv and isolated network
            [
                "/usr/bin/unshare",
                "--user",
                "--map-root-user",
                "--net",
                "--pid",
                "--fork",
                "--kill-child",
                os.path.realpath(sys.executable),
                str(Path(__file__).resolve()),
                json.dumps(list(argv)),
            ],
            capture_output=True,
            timeout=50,
            check=False,
            env={"PATH": "/usr/bin:/bin"},
        )
        report = json.loads(result.stdout)
        if result.returncode != 0 or not isinstance(report, dict):
            return {"satisfied": False, "reason": "offline CLI probe failed"}
        return report
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"satisfied": False, "reason": "offline CLI probe unavailable"}


def _probe(argv: list[str]) -> dict[str, Any]:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        fcntl.ioctl(control.fileno(), 0x8914, struct.pack("16sh", b"lo", 0x49))
    requests: list[dict[str, Any]] = []
    tool_names = (
        "Bash",
        "Read",
        "Write",
        "WebFetch",
        "Agent",
        "ToolSearch",
        "mcp__synthetic__call",
    )
    with tempfile.TemporaryDirectory(prefix="harness-tool-free-") as folder:
        marker = Path(folder) / "tool-marker"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 2 * 1024 * 1024 or len(requests) >= 4:
                    self.send_error(400)
                    return
                body = json.loads(self.rfile.read(length))
                results = [
                    part
                    for msg in body.get("messages", [])
                    for part in (
                        msg.get("content", []) if isinstance(msg.get("content"), list) else []
                    )
                    if part.get("type") == "tool_result"
                ]
                requests.append(
                    {
                        "tools": body.get("tools"),
                        "results": results,
                        "model": body.get("model"),
                        "effort": body.get("output_config", {}).get("effort"),
                    }
                )
                message = {
                    "id": "msg_synthetic",
                    "type": "message",
                    "role": "assistant",
                    "model": "synthetic-model",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
                events: list[tuple[str, dict[str, Any]]] = [
                    ("message_start", {"type": "message_start", "message": message})
                ]
                if len(requests) == 1:
                    for index, name in enumerate(tool_names):
                        events.extend(
                            [
                                (
                                    "content_block_start",
                                    {
                                        "type": "content_block_start",
                                        "index": index,
                                        "content_block": {
                                            "type": "tool_use",
                                            "id": f"tool_{index}",
                                            "name": name,
                                            "input": {},
                                        },
                                    },
                                ),
                                (
                                    "content_block_delta",
                                    {
                                        "type": "content_block_delta",
                                        "index": index,
                                        "delta": {
                                            "type": "input_json_delta",
                                            "partial_json": json.dumps(
                                                {
                                                    "command": f"touch {marker}",
                                                    "file_path": str(marker),
                                                    "content": "synthetic",
                                                    "url": "http://127.0.0.1/",
                                                    "prompt": "synthetic",
                                                    "query": "synthetic",
                                                }
                                            ),
                                        },
                                    },
                                ),
                                (
                                    "content_block_stop",
                                    {"type": "content_block_stop", "index": index},
                                ),
                            ]
                        )
                    stop = "tool_use"
                else:
                    events.extend(
                        [
                            (
                                "content_block_start",
                                {
                                    "type": "content_block_start",
                                    "index": 0,
                                    "content_block": {"type": "text", "text": ""},
                                },
                            ),
                            (
                                "content_block_delta",
                                {
                                    "type": "content_block_delta",
                                    "index": 0,
                                    "delta": {
                                        "type": "text_delta",
                                        "text": '{"replacement_text":"synthetic"}',
                                    },
                                },
                            ),
                            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                        ]
                    )
                    stop = "end_turn"
                events.extend(
                    [
                        (
                            "message_delta",
                            {
                                "type": "message_delta",
                                "delta": {"stop_reason": stop, "stop_sequence": None},
                                "usage": {"output_tokens": 1},
                            },
                        ),
                        ("message_stop", {"type": "message_stop"}),
                    ]
                )
                data = "".join(
                    "event: " + event + "\ndata: " + json.dumps(value) + "\n\n"
                    for event, value in events
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            result = subprocess.run(  # noqa: S603 - fixed local probe, explicit argv and isolated network
                argv,
                input=b"Return replacement_text JSON for a synthetic comment.",
                capture_output=True,
                timeout=35,
                check=False,
                cwd=folder,
                env={
                    "HOME": folder,
                    "PATH": "/usr/bin:/bin",
                    "ANTHROPIC_API_KEY": "synthetic-not-a-secret",
                    "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                },
            )
            rejected = {
                r.get("tool_use_id")
                for request in requests
                for r in request["results"]
                if r.get("is_error") is True and "No such tool available" in str(r.get("content"))
            }
            satisfied = (
                result.returncode == 0
                and len(requests) == 2
                and all(request["tools"] == [] for request in requests)
                and rejected == {f"tool_{i}" for i in range(len(tool_names))}
                and not marker.exists()
            )
            return {
                "satisfied": satisfied,
                "provider": "claude",
                "external_requests": 0,
                "tool_registry_empty": bool(requests) and all(r["tools"] == [] for r in requests),
                "rejected_tools": list(tool_names) if satisfied else [],
                "synthetic_requests": len(requests),
                "requested_models": [r["model"] for r in requests],
                "requested_efforts": [r["effort"] for r in requests],
            }
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


def _codex_tool_refusals(
    requests: list[dict[str, Any]], names: tuple[str, ...]
) -> tuple[bool, set[str]]:
    """実要求のtool登録と、注入した各callへの明示拒否だけを認める。"""
    registry_ok = bool(requests) and all(
        isinstance(r.get("tools", []), list)
        and all(
            isinstance(t, dict)
            and t.get("type") == "function"
            and t.get("name") == "request_user_input"
            for t in r.get("tools", [])
        )
        for r in requests
    )
    rejected: set[str] = set()
    for request in requests:
        for part in request.get("input", []):
            for i, name in enumerate(names):
                expected_type = "function_call_output"
                expected = f"unsupported call: {name}"
                if name == "apply_patch":
                    expected_type = "custom_tool_call_output"
                    expected = (
                        "patch rejected: writing is blocked by read-only sandbox; "
                        "rejected by user approval settings"
                    )
                elif name == "request_user_input":
                    expected = "request_user_input is unavailable in Default mode"
                if (
                    part.get("type") == expected_type
                    and part.get("call_id") == f"call_{i}"
                    and (
                        part.get("output") == expected
                        or (
                            name == "apply_patch"
                            and part.get("output") == "unsupported custom tool call: apply_patch"
                        )
                    )
                ):
                    rejected.add(name)
    return registry_ok, rejected


def _probe_generation(argv: list[str], provider: str, settings: dict[str, Any]) -> dict[str, Any]:
    """Codex/Geminiの正規CLIへ不正tool応答を返す。実資格情報は使わない。"""
    if provider not in {"codex", "gemini"}:
        return {"satisfied": False, "reason": "unsupported diagnostic provider"}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        fcntl.ioctl(control.fileno(), 0x8914, struct.pack("16sh", b"lo", 0x49))
    names = (
        ("exec_command", "shell", "apply_patch", "web_search", "request_user_input")
        if provider == "codex"
        else ("run_shell_command", "read_file", "write_file", "web_fetch", "google_web_search")
    )
    requests: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="harness-generation-probe-") as folder:
        marker = Path(folder) / "tool-marker"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 2 * 1024 * 1024 or len(requests) >= 4:
                    self.send_error(400)
                    return
                body = json.loads(self.rfile.read(length))
                requests.append(body)
                first = len(requests) == 1
                if provider == "codex":
                    items = (
                        [
                            {
                                "type": "function_call",
                                "id": f"fc_{i}",
                                "call_id": f"call_{i}",
                                "status": "completed",
                                "name": name,
                                "arguments": json.dumps(
                                    {"cmd": f"touch {marker}", "command": f"touch {marker}"}
                                ),
                            }
                            for i, name in enumerate(names)
                        ]
                        if first
                        else [
                            {
                                "id": "msg_synthetic",
                                "type": "message",
                                "status": "completed",
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": '{"replacement_text":"synthetic"}',
                                    }
                                ],
                            }
                        ]
                    )
                    if first:
                        items[2] = {
                            "type": "custom_tool_call",
                            "id": "fc_2",
                            "call_id": "call_2",
                            "status": "completed",
                            "name": "apply_patch",
                            "input": (
                                f"*** Begin Patch\n*** Add File: {marker}\n"
                                "+synthetic\n*** End Patch"
                            ),
                        }
                    events = [
                        {
                            "type": "response.created",
                            "response": {
                                "id": "resp_synthetic",
                                "status": "in_progress",
                                "output": [],
                            },
                        }
                    ]
                    for i, item in enumerate(items):
                        for event in ("response.output_item.added", "response.output_item.done"):
                            events.append({"type": event, "output_index": i, "item": item})
                    events.append(
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp_synthetic",
                                "status": "completed",
                                "output": items,
                                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                            },
                        }
                    )
                    raw = "".join("data: " + json.dumps(e) + "\n\n" for e in events).encode()
                    content_type = "text/event-stream"
                else:
                    parts = (
                        [
                            {
                                "functionCall": {
                                    "name": name,
                                    "args": {
                                        "command": f"touch {marker}",
                                        "file_path": str(marker),
                                        "content": "synthetic",
                                        "url": "http://127.0.0.1/",
                                    },
                                }
                            }
                            for name in names
                        ]
                        if first
                        else [{"text": '{"replacement_text":"synthetic"}'}]
                    )
                    response = {
                        "candidates": [
                            {
                                "content": {"role": "model", "parts": parts},
                                "finishReason": "STOP",
                                "index": 0,
                            }
                        ],
                        "usageMetadata": {
                            "promptTokenCount": 1,
                            "candidatesTokenCount": 1,
                            "totalTokenCount": 2,
                        },
                    }
                    streaming = "alt=sse" in self.path
                    raw = (
                        ("data: " + json.dumps(response) + "\n\n")
                        if streaming
                        else json.dumps(response)
                    ).encode()
                    content_type = "text/event-stream" if streaming else "application/json"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = {"PATH": "/usr/bin:/bin", "HOME": folder, "CI": "1", "NO_COLOR": "1"}
        command = list(argv)
        if provider == "codex":
            command[-1:-1] = [
                "-c",
                'model_provider="synthetic"',
                "-c",
                'model_providers.synthetic.name="Synthetic"',
                "-c",
                f'model_providers.synthetic.base_url="http://127.0.0.1:{server.server_port}/v1"',
                "-c",
                'model_providers.synthetic.wire_api="responses"',
                "-c",
                "model_providers.synthetic.requires_openai_auth=false",
            ]
            # This neutral directory replaces only the production cwd, never tool restrictions.
            command[command.index("--cd") + 1] = folder
            env["CODEX_HOME"] = folder
        else:
            settings = json.loads(json.dumps(settings))
            settings.setdefault("security", {})["auth"] = {"selectedType": "gemini-api-key"}
            user_config = Path(folder) / ".gemini"
            user_config.mkdir()
            # 本番Gateは個人設定を認証方式だけに制限する。合成資格で同じ形を検証。
            (user_config / "settings.json").write_text(
                json.dumps({"security": {"auth": {"selectedType": "gemini-api-key"}}}),
                encoding="utf-8",
            )
            path = Path(folder) / "settings.json"
            path.write_text(json.dumps(settings), encoding="utf-8")
            env.update(
                GEMINI_API_KEY="synthetic-not-a-secret",
                GOOGLE_GEMINI_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                GEMINI_CLI_SYSTEM_SETTINGS_PATH=str(path),
            )
        try:
            result = subprocess.run(  # noqa: S603 - isolated network and synthetic credentials
                command,
                input=b"Return replacement_text JSON only.",
                capture_output=True,
                timeout=35,
                check=False,
                cwd=folder,
                env=env,
            )
            rejected: set[str] = set()
            if provider == "codex":
                registry_ok, rejected = _codex_tool_refusals(requests, names)
            else:
                registry_ok = bool(requests) and all(
                    r.get("tools") == [{"functionDeclarations": []}] for r in requests
                )
                for request in requests:
                    for content in request.get("contents", []):
                        for part in content.get("parts", []):
                            response = part.get("functionResponse", {})
                            name = response.get("name")
                            if (
                                name in names
                                and response.get("response", {}).get("error")
                                == f'Tool "{name}" not found.'
                            ):
                                rejected.add(name)
            satisfied = (
                result.returncode == 0
                and len(requests) == 2
                and registry_ok
                and rejected == set(names)
                and not marker.exists()
                and not Path(str(marker) + "-hook").exists()
                and not Path(str(marker) + "-mcp").exists()
                and not Path(str(marker) + "-discovery").exists()
            )
            return {
                "satisfied": satisfied,
                "provider": provider,
                "external_requests": 0,
                "generation_registry_verified": registry_ok,
                "exit_code": result.returncode,
                "marker_created": marker.exists(),
                "hook_marker_created": Path(str(marker) + "-hook").exists(),
                "mcp_marker_created": Path(str(marker) + "-mcp").exists(),
                "discovery_marker_created": Path(str(marker) + "-discovery").exists(),
                "registered_tools": [
                    [t.get("name", t.get("type")) for t in r.get("tools", [])] for r in requests
                ],
                "rejected_tools": sorted(rejected),
                "synthetic_requests": len(requests),
                "residual_tool": "request_user_input (unavailable)"
                if provider == "codex"
                else None,
            }
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    try:
        argv = json.loads(sys.argv[1])
        report = (
            _probe_generation(argv, sys.argv[2], json.loads(sys.argv[3]))
            if len(sys.argv) > 2
            else _probe(argv)
        )
        print(json.dumps(report))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        print(json.dumps({"satisfied": False, "reason": "isolated CLI probe failed"}))
