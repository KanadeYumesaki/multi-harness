"""Official HTTP generation. No subprocess, tool dispatch, hidden prompt, or retry."""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from typing import Any

from harness.domain.chatgpt_request import logical_request
from harness.domain.code_proposal import strict_response_json
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_canonical
from harness.infrastructure.provider.chatgpt_auth import RESOURCE, ChatGptAuth
from harness.infrastructure.provider.chatgpt_http import ChatGptTransportError
from harness.ports.chatgpt import HttpGenerationResult, HttpGenerationSpec
from harness.ports.cli_workbench import CliProviderStatus

_SAFE_ERRORS = frozenset(
    {
        "subscription_sharing_usage_limit_exceeded",
        "subscription_sharing_usage_unavailable",
        "invalid_api_key",
        "insufficient_quota",
        "rate_limit_exceeded",
        "model_not_found",
        "invalid_request_error",
        "authentication_error",
        "server_error",
    }
)


def _error(value: object) -> str:
    return str(value) if isinstance(value, str) and value in _SAFE_ERRORS else "PROVIDER_ERROR"


def _structured_error(value: object) -> str:
    if not isinstance(value, dict) or not isinstance(value.get("error"), dict):
        return "PROVIDER_ERROR"
    return _error(value["error"].get("code"))


class ChatGptGeneration:
    def __init__(self, auth: ChatGptAuth) -> None:
        self.auth = auth
        self._lock = threading.Lock()
        self._connection: http.client.HTTPSConnection | None = None
        self._stopped = threading.Event()
        self._read_socket: socket.socket | None = None

    def statuses(self) -> tuple[CliProviderStatus, ...]:
        state = self.auth.status()
        connected = bool(state["connected"])
        return (
            CliProviderStatus(
                provider_id="chatgpt",
                display_name="ChatGPT ログイン連携",
                installed=True,
                package_version=None,
                profile_verified=connected,
                restriction_summary=(
                    "公式HTTPS Responses API / CLI起動なし",
                    "送信前承認・ツール提供なし・別の差分適用承認",
                    "認証トークンは実行中のメモリーだけで保持",
                ),
                residual_risks=(
                    "出力トークン上限はこの連携方式では指定できない",
                    "受信容量や時間で停止しても利用枠の消費は取り消せない",
                    "ChatGPT側の利用枠・クレジット設定は別途確認",
                ),
                login_state=str(state["state"]),
                login_hint="ChatGPTでログインを選び、公式ページで連携を許可してください",
                models=tuple(str(m["slug"]) for m in state["models"]),
                model_labels={str(m["slug"]): str(m["display_name"]) for m in state["models"]},
                blocking_reason=None
                if connected
                else str(state["reason"] or "CHATGPT_LOGIN_REQUIRED"),
            ),
        )

    def resolve(
        self,
        *,
        provider_id: str,
        model_id: str,
        reasoning_effort: str | None = None,
    ) -> HttpGenerationSpec:
        state = self.auth.status()
        if (
            provider_id != "chatgpt"
            or not state["connected"]
            or model_id not in {m["slug"] for m in state["models"]}
            or reasoning_effort is not None
        ):
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "ChatGPT login and a current account model are required; "
                "effort uses provider default",
            )
        reference = self.auth.credential_ref()
        projection: dict[str, Any] = {
            "transport": "HTTPS_RESPONSES_SSE",
            "endpoint": RESOURCE + "/responses",
            "provider_id": provider_id,
            "model_id": model_id,
            "credential_route": "CHATGPT_PLAN_OAUTH",
            "credential_binding": reference.reference,
            "tools": [],
            "store": False,
            "stream": True,
            "automatic_retries": 0,
            "provider_output_token_limit": "UNSUPPORTED",
            "credential_storage": "PROCESS_MEMORY_ONLY",
        }
        return HttpGenerationSpec(
            provider_id,
            model_id,
            hash_canonical(projection, artifact_type="chatgpt-http-runtime", schema_major=1),
            projection,
            reference,
        )

    def verify(self, spec: HttpGenerationSpec) -> None:
        current = self.resolve(provider_id=spec.provider_id, model_id=spec.model_id)
        if current != spec:
            raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "ChatGPT authorization changed")

    def attest(self, spec: HttpGenerationSpec) -> dict[str, Any]:
        self.verify(spec)
        return {
            "transport": "HTTPS_RESPONSES_SSE",
            "endpoint_fixed": True,
            "tools_in_request": False,
            "local_process_spawn": False,
            "credential_binding": spec.credential_ref.reference,
        }

    def _register_connection(self, connection: http.client.HTTPSConnection) -> None:
        with self._lock:
            if self._stopped.is_set():
                raise ChatGptTransportError("STOP_REQUESTED")
            self._connection = connection

    def run(
        self,
        spec: HttpGenerationSpec,
        *,
        payload: bytes,
        timeout_seconds: int,
        maximum_bytes: int,
    ) -> HttpGenerationResult:
        self.verify(spec)
        wire = strict_response_json(payload, maximum_bytes=96 * 1024)
        if not isinstance(wire, dict):
            raise HarnessError(ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "invalid HTTP body")
        logical_request(wire)
        if wire["model"] != spec.model_id:
            raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "HTTP model changed")
        self._stopped.clear()
        deadline = time.monotonic() + timeout_seconds
        connection: http.client.HTTPSConnection | None = None
        status: int | None = None
        response: http.client.HTTPResponse | None = None
        expired = threading.Event()

        def expire() -> None:
            expired.set()
            self.stop()

        # An IO timeout alone can be extended forever by one-byte-at-a-time traffic.
        watchdog = threading.Timer(timeout_seconds, expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            token = self.auth.access(spec.credential_ref)
            if self._stopped.is_set():
                return HttpGenerationResult("UNKNOWN", b"", None, None, "STOP_REQUESTED")
            connection, response = self.auth.transport.request(
                "POST",
                RESOURCE + "/responses",
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                body=payload,
                timeout=max(0.1, deadline - time.monotonic()),
                on_connection=self._register_connection,
            )
            with self._lock:
                self._connection = connection
                # HTTP Connection: close may detach the socket before body consumption.
                raw_socket = getattr(
                    getattr(getattr(response, "fp", None), "raw", None), "_sock", None
                )
                self._read_socket = (
                    raw_socket if isinstance(raw_socket, socket.socket) else connection.sock
                )
            status = response.status
            if expired.is_set() or self._stopped.is_set():
                return HttpGenerationResult("UNKNOWN", b"", status, None, "TIMEOUT_OR_STOP")
            if status != 200:
                # Do not record provider error prose, which can contain echoed credentials.
                data = response.read(min(maximum_bytes, 65536) + 1)
                if expired.is_set() or self._stopped.is_set():
                    return HttpGenerationResult("UNKNOWN", b"", status, None, "TIMEOUT_OR_STOP")
                code = "HTTP_REJECTED"
                if len(data) <= min(maximum_bytes, 65536):
                    try:
                        body = json.loads(data)
                        code = _structured_error(body)
                    except (ValueError, AttributeError):
                        code = "HTTP_REJECTED"
                return HttpGenerationResult("COMPLETED", b"", status, "HTTP_REJECTED", code)
            if response.getheader("Content-Type", "").split(";")[0].strip() != "text/event-stream":
                return HttpGenerationResult(
                    "UNKNOWN", b"", status, None, "SSE_CONTENT_TYPE_REQUIRED"
                )
            consumed = 0
            data_lines: list[bytes] = []
            while time.monotonic() < deadline and not self._stopped.is_set():
                if connection.sock is not None:
                    connection.sock.settimeout(max(0.1, deadline - time.monotonic()))
                line = response.readline(maximum_bytes - consumed + 1)
                consumed += len(line)
                if expired.is_set() or self._stopped.is_set() or time.monotonic() >= deadline:
                    return HttpGenerationResult("UNKNOWN", b"", status, None, "TIMEOUT_OR_STOP")
                if consumed > maximum_bytes:
                    return HttpGenerationResult(
                        "UNKNOWN", b"", status, None, "OUTPUT_LIMIT_EXCEEDED"
                    )
                if not line:
                    break
                if line.strip():
                    if line.startswith(b"data:"):
                        data_lines.append(line[5:].strip())
                    elif not line.startswith((b"event:", b"id:", b":")):
                        return HttpGenerationResult(
                            "UNKNOWN", b"", status, None, "SSE_FRAME_INVALID"
                        )
                    continue
                if not data_lines:
                    continue
                event = strict_response_json(b"\n".join(data_lines), maximum_bytes=maximum_bytes)
                data_lines.clear()
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    return HttpGenerationResult("UNKNOWN", b"", status, None, "SSE_EVENT_INVALID")
                kind = event["type"]
                if kind in ("response.output_item.added", "response.output_item.done"):
                    item = event.get("item")
                    if not isinstance(item, dict) or item.get("type") not in (
                        "message",
                        "reasoning",
                    ):
                        return HttpGenerationResult(
                            "UNKNOWN", b"", status, None, "TOOL_OUTPUT_REJECTED"
                        )
                if kind == "response.completed":
                    completed = event.get("response")
                    if not isinstance(completed, dict) or completed.get("status") != "completed":
                        return HttpGenerationResult(
                            "UNKNOWN", b"", status, kind, "TERMINAL_INVALID"
                        )
                    output = completed.get("output")
                    if not isinstance(output, list):
                        return HttpGenerationResult(
                            "UNKNOWN", b"", status, kind, "TERMINAL_INVALID"
                        )
                    messages = [
                        v for v in output if isinstance(v, dict) and v.get("type") == "message"
                    ]
                    if any(
                        not isinstance(v, dict) or v.get("type") not in ("message", "reasoning")
                        for v in output
                    ):
                        return HttpGenerationResult(
                            "COMPLETED", b"", status, "POLICY_REJECTED", "TOOL_OUTPUT_REJECTED"
                        )
                    if len(messages) != 1 or messages[0].get("role") != "assistant":
                        return HttpGenerationResult(
                            "COMPLETED", b"", status, "POLICY_REJECTED", "OUTPUT_CONTRACT_INVALID"
                        )
                    content = messages[0].get("content")
                    if (
                        not isinstance(content, list)
                        or len(content) != 1
                        or not isinstance(content[0], dict)
                        or content[0].get("type") != "output_text"
                        or not isinstance(content[0].get("text"), str)
                    ):
                        return HttpGenerationResult(
                            "COMPLETED", b"", status, "POLICY_REJECTED", "OUTPUT_CONTRACT_INVALID"
                        )
                    return HttpGenerationResult(
                        "COMPLETED", content[0]["text"].encode("utf-8"), status, kind
                    )
                if kind in ("response.failed", "response.incomplete", "error"):
                    container = event.get("response") or event
                    code = (
                        _structured_error(container)
                        if isinstance(container, dict)
                        else "PROVIDER_ERROR"
                    )
                    return HttpGenerationResult("COMPLETED", b"", status, kind, code)
                if not kind.startswith(("response.",)):
                    return HttpGenerationResult(
                        "UNKNOWN", b"", status, None, "SSE_EVENT_UNSUPPORTED"
                    )
            return HttpGenerationResult("UNKNOWN", b"", status, None, "STREAM_INTERRUPTED")
        except (
            ChatGptTransportError,
            OSError,
            http.client.HTTPException,
            HarnessError,
            ValueError,
        ):
            reason = (
                "TIMEOUT_OR_STOP"
                if expired.is_set() or self._stopped.is_set()
                else "TRANSPORT_UNCONFIRMED"
            )
            return HttpGenerationResult("UNKNOWN", b"", status, None, reason)
        finally:
            watchdog.cancel()
            with self._lock:
                self._connection = None
                self._read_socket = None
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()

    def stop(self) -> None:
        self._stopped.set()
        with self._lock:
            connection = self._connection
            sock = self._read_socket
            if sock is None and connection is not None:
                sock = connection.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    # Local shutdown does not establish the remote operation's outcome.
                    self._stopped.set()
            if connection is not None:
                connection.close()
