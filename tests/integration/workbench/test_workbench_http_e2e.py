"""実 Socket の HTTP Server → Application → 実 Process の偽 CLI → 実ファイル適用。

**DB を試験内で直接書いて経路を飛ばさない。** 画面が叩くのと同じ URL と Header で、
起動から適用まで通す。既存 Chat API が同じ Server 上で壊れていないことも見る。
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from harness.presentation.local_ui.api import LocalUiApi
from harness.presentation.local_ui.server import start_server

from .conftest import WorkbenchEnv, make_env

pytestmark = pytest.mark.integration

_SESSION_IN_HTML = re.compile(r'name="harness-session" content="([0-9a-f]{64})"')


class Client:
    def __init__(self, base: str, token: str, provider_id: str = "codex") -> None:
        self.base = base
        self.token = token
        self.provider_id = provider_id

    def call(
        self, path: str, *, method: str = "GET", body: dict[str, Any] | None = None, **headers: str
    ) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - loopback only
            self.base + path, data=data, method=method
        )
        request.add_header("X-Harness-Session", headers.get("token", self.token))
        if data is not None:
            request.add_header("Content-Type", "application/json")
            request.add_header("Origin", headers.get("origin", self.base))
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8")
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, {"raw": raw[:200]}


@pytest.fixture(params=["codex", "claude"])
def served(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[tuple[WorkbenchEnv, Client]]:
    env = make_env(tmp_path, provider=request.param)
    server = start_server(
        build_api=lambda origin, token, assets: LocalUiApi(
            env.services, origin=origin, session_token=token, assets=assets
        ),
        port=0,
        max_body_bytes=env.services.max_body_bytes,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with urllib.request.urlopen(server.url + "/", timeout=30) as response:  # noqa: S310
        html = response.read().decode("utf-8")
    match = _SESSION_IN_HTML.search(html)
    assert match is not None
    assert "workbench-panel" in html
    try:
        yield env, Client(server.url, match.group(1), request.param)
    finally:
        server.shutdown()
        env.services.close()


def test_existing_chat_api_still_answers_over_a_real_socket(
    served: tuple[WorkbenchEnv, Client],
) -> None:
    """`ThreadingHTTPServer` と共有接続の組合せが壊れていないこと。"""
    _, client = served
    status, payload = client.call("/api/conversations")
    assert status == 200
    assert "conversations" in payload
    status, payload = client.call("/api/conversations", method="POST", body={})
    assert status == 201


def test_browser_flow_from_request_to_applied_file(
    served: tuple[WorkbenchEnv, Client],
) -> None:
    env, client = served
    status, overview = client.call("/api/workbench")
    assert status == 200 and overview["available"] is True
    assert [row["provider_id"] for row in overview["providers"]] == [client.provider_id, "chatgpt"]
    chatgpt = overview["providers"][1]
    assert chatgpt["profile_verified"] is False
    assert chatgpt["blocking_reason"] == "CHATGPT_LOGIN_REQUIRED"
    assert chatgpt["models"] == []
    assert env.runner.calls == []

    status, targets = client.call("/api/workbench/targets")
    assert status == 200
    assert "hello.py" in [row["relative_path"] for row in targets["targets"]]

    status, session = client.call(
        "/api/workbench/sessions",
        method="POST",
        body={
            "provider_id": client.provider_id,
            "model_id": "fake-model",
            "relative_path": "hello.py",
            "instruction": "日本語の説明コメントを追加してください。",
        },
    )
    assert status == 201
    sid = session["session_id"]

    status, confirmation = client.call(f"/api/workbench/sessions/{sid}/confirmation")
    assert status == 200
    assert json.loads(confirmation["request_payload_text"])["instruction"].startswith("日本語")

    assert (
        client.call(
            f"/api/workbench/sessions/{sid}/approve-send",
            method="POST",
            body={"execution_plan_hash": session["execution_plan_hash"]},
        )[0]
        == 200
    )
    status, _ = client.call(
        f"/api/workbench/sessions/{sid}/send",
        method="POST",
        body={"execution_plan_hash": session["execution_plan_hash"]},
    )
    assert status == 202

    # 別タブからの二重送信。**Button の disabled ではなくサーバー側が断る。**
    status, refused = client.call(
        f"/api/workbench/sessions/{sid}/send",
        method="POST",
        body={"execution_plan_hash": session["execution_plan_hash"]},
    )
    assert status == 409

    current = _await_state(client, sid, {"PROPOSAL_READY", "SEND_FAILED", "SEND_UNKNOWN"})
    assert current["state"] == "PROPOSAL_READY"

    status, diff = client.call(f"/api/workbench/sessions/{sid}/diff")
    assert status == 200
    assert diff["unified_diff"].startswith("--- a/hello.py")

    assert (
        client.call(
            f"/api/workbench/sessions/{sid}/approve-apply",
            method="POST",
            body={
                "apply_execution_plan_hash": current["apply_execution_plan_hash"],
                "proposal_hash": current["proposal_hash"],
            },
        )[0]
        == 200
    )
    status, applied = client.call(
        f"/api/workbench/sessions/{sid}/apply",
        method="POST",
        body={"apply_execution_plan_hash": current["apply_execution_plan_hash"]},
    )
    assert status == 200
    assert applied["state"] == "APPLIED"
    assert env.read("hello.py") == diff["after_text"]
    assert env.read("notes.md").startswith("# メモ")
    assert len(env.runner.calls) == 1


def test_cross_origin_write_is_refused_over_the_socket(
    served: tuple[WorkbenchEnv, Client],
) -> None:
    env, client = served
    status, _ = client.call(
        "/api/workbench/sessions",
        method="POST",
        body={
            "provider_id": client.provider_id,
            "model_id": "fake-model",
            "relative_path": "hello.py",
            "instruction": "x",
        },
        origin="http://attacker.invalid",
    )
    assert status == 403
    assert env.runner.calls == []


def _await_state(client: Client, sid: str, wanted: set[str]) -> dict[str, Any]:
    deadline = time.monotonic() + 120
    payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status, payload = client.call(f"/api/workbench/sessions/{sid}")
        assert status == 200
        if payload["state"] in wanted:
            return payload
        time.sleep(0.25)
    raise AssertionError(f"session stayed in {payload.get('state')}")


def test_http_model_effort_selection_is_validated_and_visible(served):
    env, client = served
    model = "opus" if client.provider_id == "claude" else "gpt-5.6-sol"
    body = {
        "provider_id": client.provider_id,
        "model_id": model,
        "relative_path": "hello.py",
        "instruction": "説明を追加",
        "reasoning_effort": "high",
    }
    status, session = client.call("/api/workbench/sessions", method="POST", body=body)
    assert status == 201
    assert session["reasoning_effort"] == "high"
    status, confirm = client.call(
        "/api/workbench/sessions/" + session["session_id"] + "/confirmation"
    )
    assert status == 200
    assert confirm["reasoning_effort"] == "high"
    assert confirm["runtime_projection"]["reasoning_effort"] == "high"
    assert json.loads(confirm["request_payload_text"])["reasoning_effort"] == "high"
    for invalid in ["ultracode", "high --tools Bash", True, {}, ""]:
        status, _ = client.call(
            "/api/workbench/sessions", method="POST", body={**body, "reasoning_effort": invalid}
        )
        assert status in (400, 422)
    assert not env.runner.calls


def test_approved_operations_continue_after_client_reconnect(
    served: tuple[WorkbenchEnv, Client],
) -> None:
    env, client = served
    _, session = client.call(
        "/api/workbench/sessions",
        method="POST",
        body={
            "provider_id": client.provider_id,
            "model_id": "fake-model",
            "relative_path": "hello.py",
            "instruction": "説明を追加",
        },
    )
    base = "/api/workbench/sessions/" + session["session_id"]
    body = {"execution_plan_hash": session["execution_plan_hash"]}
    assert client.call(base + "/approve-send", method="POST", body=body)[0] == 200
    assert env.runner.calls == []
    client = Client(client.base, client.token, client.provider_id)
    assert client.call(base)[1]["state"] == "SEND_APPROVED"
    assert client.call(base + "/send", method="POST", body=body)[0] == 202
    current = _await_state(client, session["session_id"], {"PROPOSAL_READY", "SEND_FAILED"})
    assert current["state"] == "PROPOSAL_READY"
    assert (
        client.call(
            base + "/approve-apply",
            method="POST",
            body={
                "apply_execution_plan_hash": current["apply_execution_plan_hash"],
                "proposal_hash": current["proposal_hash"],
            },
        )[0]
        == 200
    )
    before = env.read("hello.py")
    client = Client(client.base, client.token, client.provider_id)
    assert client.call(base)[1]["state"] == "APPLY_APPROVED"
    status, applied = client.call(
        base + "/apply",
        method="POST",
        body={
            "apply_execution_plan_hash": current["apply_execution_plan_hash"],
        },
    )
    assert status == 200 and applied["state"] == "APPLIED"
    assert env.read("hello.py") != before
    assert len(env.runner.calls) == 1
    assert client.call(base + "/resume-send", method="POST", body={})[0] == 409
    assert len(env.runner.calls) == 1
