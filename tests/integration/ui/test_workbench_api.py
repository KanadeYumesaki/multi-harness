"""Workbench の HTTP 契約。**Socket を開かず、Request/Response だけで踏む。**

Web 境界（Origin・Token・過大 body・未知 Key・Path escape・GET での状態変更）は
既存 Chat API と同じ規則を、新しい経路にも当てている。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "tests" / "integration" / "workbench"))

from tests.integration.workbench.conftest import WorkbenchEnv, make_env  # noqa: E402

from harness.presentation.local_ui.api import LocalUiApi, Request  # noqa: E402
from harness.presentation.local_ui.composition import build_services  # noqa: E402

pytestmark = pytest.mark.integration

ORIGIN = "http://127.0.0.1:65535"
TOKEN = "a" * 64


def _api(services: Any) -> LocalUiApi:
    return LocalUiApi(services, origin=ORIGIN, session_token=TOKEN, assets={})


def _get(api: LocalUiApi, path: str, *, token: str | None = TOKEN) -> tuple[int, Any]:
    headers = {} if token is None else {"x-harness-session": token}
    response = api.handle(Request(method="GET", path=path, headers=headers))
    return response.status, json.loads(response.body.decode("utf-8"))


def _post(
    api: LocalUiApi,
    path: str,
    body: dict[str, Any],
    *,
    origin: str = ORIGIN,
    token: str = TOKEN,
    content_type: str = "application/json",
) -> tuple[int, Any]:
    response = api.handle(
        Request(
            method="POST",
            path=path,
            headers={
                "origin": origin,
                "x-harness-session": token,
                "content-type": content_type,
            },
            body=json.dumps(body).encode("utf-8"),
        )
    )
    return response.status, json.loads(response.body.decode("utf-8"))


@pytest.fixture
def env(tmp_path: Path) -> Any:
    environment = make_env(tmp_path)
    try:
        yield environment
    finally:
        environment.services.close()


def test_workbench_is_absent_until_the_operator_starts_it_with_a_workspace(
    tmp_path: Path,
) -> None:
    services = build_services(
        repo_root=REPO_ROOT,
        database_path=tmp_path / "db" / "state.sqlite3",
        artifact_root=tmp_path / "cas",
    )
    try:
        api = _api(services)
        status, payload = _get(api, "/api/workbench")
        assert status == 200
        assert payload["available"] is False
        assert "--workspace" in payload["reason"]
        status, payload = _get(api, "/api/workbench/targets")
        assert status == 409
        assert payload["error"]["code"] == "WORKBENCH_NOT_CONFIGURED"
        status, payload = _post(
            api,
            "/api/workbench/sessions",
            {
                "provider_id": "codex",
                "model_id": "m",
                "relative_path": "hello.py",
                "instruction": "x",
            },
        )
        assert status == 409
    finally:
        services.close()


def test_reads_require_the_session_token(env: WorkbenchEnv) -> None:
    api = _api(env.services)
    for path in ("/api/workbench", "/api/workbench/targets"):
        assert _get(api, path, token=None)[0] == 403
        assert _get(api, path, token="b" * 64)[0] == 403
    assert _get(api, "/api/workbench")[0] == 200


def test_writes_require_origin_and_token(env: WorkbenchEnv) -> None:
    api = _api(env.services)
    body = {
        "provider_id": "codex",
        "model_id": "fake-model",
        "relative_path": "hello.py",
        "instruction": "コメントを足してください。",
    }
    assert _post(api, "/api/workbench/sessions", body, origin="http://evil.invalid")[0] == 403
    assert _post(api, "/api/workbench/sessions", body, token="b" * 64)[0] == 403
    assert _post(api, "/api/workbench/sessions", body, content_type="text/plain")[0] == 400
    assert _post(api, "/api/workbench/sessions", body)[0] == 201


def test_unknown_fields_and_bad_shapes_are_refused(env: WorkbenchEnv) -> None:
    api = _api(env.services)
    status, payload = _post(
        api,
        "/api/workbench/sessions",
        {
            "provider_id": "codex",
            "model_id": "fake-model",
            "relative_path": "hello.py",
            "instruction": "x",
            "auth_session": "local-uid:0",
        },
    )
    assert status == 400
    assert "auth_session" in payload["error"]["reason"]
    assert _post(api, "/api/workbench/sessions", {"provider_id": "codex"})[0] == 400


def test_path_shapes_are_refused_before_touching_anything(env: WorkbenchEnv) -> None:
    api = _api(env.services)
    for path in (
        "/api/workbench/sessions/../../etc/passwd",
        "/api/workbench/sessions/%2e%2e",
        "/api/workbench?session=1",
        "/api/workbench/sessions/not-a-uuid",
    ):
        assert _get(api, path)[0] == 400


def test_get_never_changes_state(env: WorkbenchEnv) -> None:
    api = _api(env.services)
    _, session = _post(
        api,
        "/api/workbench/sessions",
        {
            "provider_id": "codex",
            "model_id": "fake-model",
            "relative_path": "hello.py",
            "instruction": "コメントを足してください。",
        },
    )
    sid = session["session_id"]
    for path in ("approve-send", "send", "apply", "approve-apply", "recover", "mark-unknown"):
        assert _get(api, f"/api/workbench/sessions/{sid}/{path}")[0] in {400, 403, 404}
    assert _get(api, f"/api/workbench/sessions/{sid}")[1]["state"] == "DRAFTED"
    assert env.runner.calls == []


def test_oversized_body_is_refused(env: WorkbenchEnv) -> None:
    api = _api(env.services)
    response = api.handle(
        Request(
            method="POST",
            path="/api/workbench/sessions",
            headers={
                "origin": ORIGIN,
                "x-harness-session": TOKEN,
                "content-type": "application/json",
            },
            body=b"{" + b"x" * (env.services.max_body_bytes + 10) + b"}",
        )
    )
    assert response.status == 413


def test_responses_carry_the_security_headers_and_are_not_cached(env: WorkbenchEnv) -> None:
    api = _api(env.services)
    response = api.handle(
        Request(method="GET", path="/api/workbench", headers={"x-harness-session": TOKEN})
    )
    headers = dict(response.headers)
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'none'" in headers["Content-Security-Policy"]


def test_full_flow_over_the_http_contract(env: WorkbenchEnv) -> None:
    api = _api(env.services)
    status, session = _post(
        api,
        "/api/workbench/sessions",
        {
            "provider_id": "codex",
            "model_id": "fake-model",
            "relative_path": "hello.py",
            "instruction": "日本語の説明コメントを追加してください。",
        },
    )
    assert status == 201
    sid = session["session_id"]

    status, confirmation = _get(api, f"/api/workbench/sessions/{sid}/confirmation")
    assert status == 200
    document = json.loads(confirmation["request_payload_text"])
    assert document["target_relative_path"] == "hello.py"
    assert "system prompt" in confirmation["transparency_note"]

    status, _ = _post(
        api,
        f"/api/workbench/sessions/{sid}/approve-send",
        {"execution_plan_hash": session["execution_plan_hash"]},
    )
    assert status == 200
    status, accepted = _post(
        api,
        f"/api/workbench/sessions/{sid}/send",
        {"execution_plan_hash": session["execution_plan_hash"]},
    )
    assert status == 202
    assert env.gateway.wait_for_idle(120)

    status, current = _get(api, f"/api/workbench/sessions/{sid}")
    assert current["state"] == "PROPOSAL_READY"
    status, diff = _get(api, f"/api/workbench/sessions/{sid}/diff")
    assert status == 200
    assert diff["after_text"].startswith("# 日本語の説明コメント")

    status, _ = _post(
        api,
        f"/api/workbench/sessions/{sid}/approve-apply",
        {
            "apply_execution_plan_hash": current["apply_execution_plan_hash"],
            "proposal_hash": current["proposal_hash"],
        },
    )
    assert status == 200
    status, applied = _post(
        api,
        f"/api/workbench/sessions/{sid}/apply",
        {"apply_execution_plan_hash": current["apply_execution_plan_hash"]},
    )
    assert status == 200
    assert applied["state"] == "APPLIED"
    assert env.read("hello.py") == diff["after_text"]
    assert len(env.runner.calls) == 1


def test_history_api_requires_session_and_uses_stored_parent(env: WorkbenchEnv) -> None:
    api = _api(env.services)
    status, parent = _post(
        api,
        "/api/workbench/sessions",
        {
            "provider_id": "codex",
            "model_id": "fake-model",
            "relative_path": "hello.py",
            "instruction": "親の依頼",
        },
    )
    assert status == 201
    path = "/api/workbench/sessions/" + parent["session_id"] + "/history"
    assert _get(api, path, token=None)[0] == 403
    assert _get(api, path, token="b" * 64)[0] == 403
    status, history = _get(api, path)
    assert status == 200
    assert history["history"]["turns"][0]["instruction"] == "親の依頼"
    assert history["history"]["omitted_messages"] == 0
    assert _post(api, path, {})[0] == 404
    status, child = _post(
        api,
        "/api/workbench/sessions",
        {
            "provider_id": "codex",
            "model_id": "fake-model",
            "relative_path": "hello.py",
            "instruction": "続き",
            "parent_session_id": parent["session_id"],
        },
    )
    assert status == 201
    status, confirmation = _get(
        api, "/api/workbench/sessions/" + child["session_id"] + "/confirmation"
    )
    assert status == 200
    payload = json.loads(confirmation["request_payload_text"])
    assert payload["conversation_history"]["turns"][0]["instruction"] == "親の依頼"
    assert env.runner.calls == []


@pytest.mark.parametrize("key", ["parent_session_id", "conversation_id"])
@pytest.mark.parametrize("value", ["../escape", "", "not-a-uuid", 12, True])
def test_history_source_shape_is_validated_before_storage(
    env: WorkbenchEnv, key: str, value: Any
) -> None:
    status, _ = _post(
        _api(env.services),
        "/api/workbench/sessions",
        {
            "provider_id": "codex",
            "model_id": "fake-model",
            "relative_path": "hello.py",
            "instruction": "続き",
            key: value,
        },
    )
    assert status == 400
    assert env.gateway.overview()["sessions"] == []
    assert env.runner.calls == []


def test_client_cannot_supply_a_forged_history_or_skip_its_review(env: WorkbenchEnv) -> None:
    api = _api(env.services)
    for key in ("conversation_history", "handoff", "history_hash", "skip_history_validation"):
        status, _ = _post(
            api,
            "/api/workbench/sessions",
            {
                "provider_id": "codex",
                "model_id": "fake-model",
                "relative_path": "hello.py",
                "instruction": "続き",
                key: {},
            },
        )
        assert status == 400
    assert env.gateway.overview()["sessions"] == []
    assert env.runner.calls == []
