"""HTTP generation uses the existing approval CAS, durable journal and separate apply path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from chatgpt_fixtures import completed, event, login, setup_auth

from harness.domain.chatgpt_request import logical_request, responses_request
from harness.domain.errors import HarnessError
from harness.infrastructure.provider.chatgpt_generation import ChatGptGeneration
from harness.infrastructure.sqlite.connection_factory import ConnectionFactory

from .conftest import WorkbenchEnv

pytestmark = pytest.mark.integration


def connect_fixture(env: WorkbenchEnv) -> tuple[object, object]:
    auth, transport, _ = setup_auth()
    login(auth)
    generation = ChatGptGeneration(auth)
    env.gateway._service.chatgpt = generation
    env.gateway._worker.chatgpt = generation
    assert env.gateway.chatgpt_connection is not None
    auth.remember = env.gateway.chatgpt_connection.remember
    env.gateway.chatgpt_connection.auth = auth
    return auth, transport


def draft(env: WorkbenchEnv) -> dict[str, object]:
    return env.gateway.create_session(
        provider_id="chatgpt",
        model_id="fixture-model",
        relative_path="hello.py",
        instruction="日本語の説明コメントを追加してください。",
    )


def generate(env: WorkbenchEnv, session: dict[str, object]) -> dict[str, object]:
    env.gateway.approve_send(
        session["session_id"], execution_plan_hash=session["execution_plan_hash"]
    )
    env.gateway.start_send(
        session["session_id"], execution_plan_hash=session["execution_plan_hash"]
    )
    assert env.gateway.wait_for_idle(30)
    return env.gateway.session(session["session_id"])


def test_exact_http_body_is_reviewed_and_approval_is_durable_before_request(
    workbench: WorkbenchEnv,
) -> None:
    auth, transport = connect_fixture(workbench)
    session = draft(workbench)
    confirmation = workbench.gateway.confirmation(session["session_id"])
    wire = json.loads(confirmation["request_payload_text"])
    assert set(wire) == {"model", "store", "stream", "instructions", "input"}
    assert wire["store"] is False and wire["stream"] is True
    assert logical_request(wire)["current_file_text"] == workbench.read("hello.py")
    assert not any(url.endswith("/responses") for _, url, _ in transport.calls)
    assert workbench.runner.calls == []
    with pytest.raises(HarnessError):
        workbench.gateway.start_send(
            session["session_id"], execution_plan_hash=session["execution_plan_hash"]
        )
    observed: list[dict[str, object]] = []

    def observe() -> None:
        connection = ConnectionFactory(workbench.root / "db" / "state.sqlite3").connect()
        try:
            journal = connection.execute(
                "SELECT state FROM cli_invocation_journal WHERE session_id=?",
                (session["session_id"],),
            ).fetchone()
            consumed = connection.execute(
                "SELECT COUNT(*) AS n FROM approval_consume_ticket WHERE state='CONSUMED'"
            ).fetchone()
            observed.append({"journal": journal["state"], "consumed": consumed["n"]})
        finally:
            connection.close()

    transport.on_inference = observe
    finished = generate(workbench, session)
    assert finished["state"] == "PROPOSAL_READY"
    assert observed == [{"journal": "EXECUTION_ATTEMPTED", "consumed": 1}]
    sent = [body for _, url, body in transport.calls if url.endswith("/responses")]
    assert sent == [confirmation["request_payload_text"].encode()]
    assert "exit_code" not in finished["send_observation"]
    assert "descendant_cleanup" not in finished["send_observation"]
    assert workbench.read("hello.py") != "# 合成コメント\nvalue = 1\n"
    diff = workbench.gateway.diff(session["session_id"])
    with pytest.raises(HarnessError):
        workbench.gateway.apply(
            session["session_id"], apply_execution_plan_hash=finished["apply_execution_plan_hash"]
        )
    workbench.gateway.approve_apply(
        session["session_id"],
        apply_execution_plan_hash=finished["apply_execution_plan_hash"],
        proposal_hash=diff["after_hash"],
    )
    applied = workbench.gateway.apply(
        session["session_id"], apply_execution_plan_hash=finished["apply_execution_plan_hash"]
    )
    assert applied["state"] == "APPLIED"
    assert workbench.read("hello.py") == "# 合成コメント\nvalue = 1\n"
    assert workbench.runner.calls == []
    assert len(sent) == 1
    for path in [
        workbench.root / "db" / "state.sqlite3",
        *list((workbench.root / "cas").rglob("*")),
    ]:
        if path.is_file():
            for token in (b"fixture-access-only", b"fixture-refresh-only"):
                assert token not in path.read_bytes()
    auth.close()


@pytest.mark.parametrize(
    ("kind", "state"),
    [
        ("interrupted", "SEND_UNKNOWN"),
        ("tool", "SEND_UNKNOWN"),
        ("incomplete", "SEND_FAILED"),
        ("failed", "SEND_FAILED"),
        ("invalid-output", "SEND_FAILED"),
        ("overflow", "SEND_UNKNOWN"),
    ],
)
def test_incomplete_or_unsafe_stream_never_reaches_apply(
    workbench: WorkbenchEnv,
    kind: str,
    state: str,
) -> None:
    auth, transport = connect_fixture(workbench)
    before = workbench.read("hello.py")
    if kind == "interrupted":
        transport.stream = event({"type": "response.output_text.delta", "delta": "partial"})
    elif kind == "tool":
        transport.stream = event(
            {"type": "response.output_item.added", "item": {"type": "function_call"}}
        )
    elif kind == "incomplete":
        transport.stream = event(
            {"type": "response.incomplete", "response": {"status": "incomplete"}}
        )
    elif kind == "failed":
        transport.stream = event(
            {
                "type": "response.failed",
                "response": {
                    "error": {"code": "subscription_sharing_usage_limit_exceeded"},
                },
            }
        )
    elif kind == "invalid-output":
        transport.stream = completed('{"replacement_text":"ok","unexpected":true}')
    else:
        transport.stream = b"data: " + b"x" * (512 * 1024 + 1)
    session = generate(workbench, draft(workbench))
    assert session["state"] == state
    assert workbench.read("hello.py") == before
    assert len([url for _, url, _ in transport.calls if url.endswith("/responses")]) == 1
    with pytest.raises(HarnessError):
        workbench.gateway.resume_send(session["session_id"])
    with pytest.raises(HarnessError):
        workbench.gateway.diff(session["session_id"])
    auth.close()


def test_logout_invalidates_old_approval_without_sending(workbench: WorkbenchEnv) -> None:
    auth, transport = connect_fixture(workbench)
    session = draft(workbench)
    workbench.gateway.approve_send(
        session["session_id"], execution_plan_hash=session["execution_plan_hash"]
    )
    auth.disconnect()
    with pytest.raises(HarnessError):
        workbench.gateway.start_send(
            session["session_id"], execution_plan_hash=session["execution_plan_hash"]
        )
    assert not any(url.endswith("/responses") for _, url, _ in transport.calls)
    auth.close()


def test_http_history_can_continue_into_another_provider(workbench: WorkbenchEnv) -> None:
    auth, _ = connect_fixture(workbench)
    parent = generate(workbench, draft(workbench))
    continued = workbench.gateway.create_session(
        provider_id="codex",
        model_id="fake-model",
        relative_path="notes.md",
        instruction="前の説明を参考にしてください。",
        parent_session_id=parent["session_id"],
        history_selection={"mode": "recent", "recent_count": 1},
    )
    request = json.loads(
        workbench.gateway.confirmation(continued["session_id"])["request_payload_text"]
    )
    assert request["conversation_history"]["turns"][0]["provider_id"] == "chatgpt"
    assert request["conversation_history"]["turns"][0]["proposal_applied"] is False
    assert (
        request["conversation_history"]["turns"][0]["replacement_text"]
        == "# 合成コメント\nvalue = 1\n"
    )
    auth.close()


def test_unknown_model_effort_and_http_fields_are_rejected(workbench: WorkbenchEnv) -> None:
    auth, transport = connect_fixture(workbench)
    for overrides in ({"model_id": "invented-model"}, {"reasoning_effort": "invented-effort"}):
        values = {
            "provider_id": "chatgpt",
            "model_id": "fixture-model",
            "relative_path": "hello.py",
            "instruction": "説明を追加",
            **overrides,
        }
        with pytest.raises(HarnessError):
            workbench.gateway.create_session(**values)
    wire = responses_request("fixture-model", {"model_id": "fixture-model"})
    wire["max_output_tokens"] = 1
    with pytest.raises(HarnessError):
        logical_request(wire)
    assert not any(url.endswith("/responses") for _, url, _ in transport.calls)
    auth.close()


def test_chatgpt_workbench_needs_no_cli_profile(tmp_path: Path) -> None:
    import os

    from workbench_fixtures import build_demo_worktree

    from harness.presentation.local_ui.composition import WorkbenchSetup, build_services

    from .conftest import REPO_ROOT

    worktree = build_demo_worktree(tmp_path / "demo")
    services = build_services(
        repo_root=REPO_ROOT,
        database_path=tmp_path / "state" / "db.sqlite3",
        artifact_root=tmp_path / "state" / "cas",
        workbench=WorkbenchSetup(
            workspace=worktree,
            runtime_profile=None,
            workspace_label="chatgpt-only",
            auth_session="local-uid:" + str(os.getuid()),
        ),
    )
    try:
        assert services.workbench is not None
        overview = services.workbench.overview()
        assert [p["provider_id"] for p in overview["providers"]] == ["chatgpt"]
        assert overview["chatgpt"]["connected"] is False
        assert services.workbench.targets()["targets"]
        with pytest.raises(HarnessError):
            services.workbench.create_session(
                provider_id="codex",
                model_id="unconfigured",
                relative_path="hello.py",
                instruction="no CLI route",
            )
    finally:
        services.close()


def test_chatgpt_generated_secret_is_rejected_before_storage(workbench: WorkbenchEnv) -> None:
    auth, transport = connect_fixture(workbench)
    canary = "FDE-HARNESS-CANARY-HTTPLEAK1"
    transport.stream = completed(json.dumps({"replacement_text": "# " + canary + "\n"}))
    before = workbench.read("hello.py")
    result = generate(workbench, draft(workbench))
    assert result["state"] == "SEND_FAILED"
    assert result["failure"]["class"] == "PROPOSAL_REJECTED_BY_SCANNER"
    assert canary not in json.dumps(result)
    assert workbench.read("hello.py") == before
    for path in (workbench.root / "cas").rglob("*"):
        if path.is_file():
            assert canary.encode() not in path.read_bytes()
    auth.close()


@pytest.mark.parametrize("phase", ["headers", "body_keep_alive", "body_close"])
def test_slow_drip_http_is_cut_off_at_deadline_without_retry(
    phase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use real local socket IO: per-read timeouts must not extend the total deadline."""
    import http.client
    import socket
    import threading
    import time

    from harness.infrastructure.provider import chatgpt_http

    auth, _, _ = setup_auth()
    login(auth)
    auth.transport = chatgpt_http.OpenAiHttps()
    generation = ChatGptGeneration(auth)
    spec = generation.resolve(provider_id="chatgpt", model_id="fixture-model")
    wire = json.dumps(responses_request("fixture-model", {"model_id": "fixture-model"})).encode()
    client, server = socket.socketpair()
    stopped = threading.Event()
    calls: list[bytes] = []

    class LocalSocketHttps(http.client.HTTPConnection):
        def __init__(self, host: str, *, timeout: float, context: object) -> None:
            super().__init__(host, timeout=timeout)

        def connect(self) -> None:
            self.sock = client

    monkeypatch.setattr(chatgpt_http.http.client, "HTTPSConnection", LocalSocketHttps)

    def respond() -> None:
        try:
            server.settimeout(5)
            request = b""
            while b"\r\n\r\n" not in request:
                request += server.recv(65536)
            calls.append(request)
            if phase == "headers":
                data = b"HTTP/1.1 200 OK\r\n" + b"X-Slow: " + b"a" * 1000
            else:
                connection_header = b"close" if phase == "body_close" else b"keep-alive"
                server.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: "
                    + connection_header
                    + b"\r\n\r\n"
                )
                data = b"data: " + b" " * 1000
            for byte in data:
                if stopped.wait(0.01):
                    break
                server.sendall(bytes([byte]))
        except OSError:
            stopped.set()
        finally:
            server.close()

    thread = threading.Thread(target=respond, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        result = generation.run(spec, payload=wire, timeout_seconds=1, maximum_bytes=512 * 1024)
        elapsed = time.monotonic() - started
        assert elapsed < 3, elapsed
        assert result.outcome == "UNKNOWN"
        assert result.error_code == "TIMEOUT_OR_STOP"
        assert len(calls) == 1
    finally:
        stopped.set()
        client.close()
        thread.join(timeout=5)
        auth.close()
    assert not thread.is_alive()
