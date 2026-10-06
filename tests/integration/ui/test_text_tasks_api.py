"""The new file-free result route keeps the existing Origin/session boundary."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tests.integration.ui.test_workbench_api import _api, _get, _post
from tests.integration.workbench.conftest import WorkbenchEnv, make_env

pytestmark = pytest.mark.integration


@pytest.fixture
def env(tmp_path: Path) -> Iterator[WorkbenchEnv]:
    instance = make_env(tmp_path)
    try:
        yield instance
    finally:
        instance.services.close()


def test_fileless_plan_and_result_need_normal_approval(env: WorkbenchEnv) -> None:
    api = _api(env.services)
    body = {
        "provider_id": "codex",
        "model_id": "fake-model",
        "task_kind": "summary",
        "instruction": "要約してください",
        "reference_text": "合成資料",
        "output_format": "text",
    }
    status, document = _post(api, "/api/workbench/sessions", body)
    assert status == 201
    sid = document["session_id"]
    assert env.runner.calls == []
    assert _get(api, "/api/workbench/sessions/" + sid + "/result", token=None)[0] == 403
    assert _get(api, "/api/workbench/sessions/" + sid + "/result")[0] == 422
    status, _ = _post(
        api,
        "/api/workbench/sessions/" + sid + "/approve-send",
        {"execution_plan_hash": document["execution_plan_hash"]},
    )
    assert status == 200
    status, _ = _post(
        api,
        "/api/workbench/sessions/" + sid + "/send",
        {"execution_plan_hash": document["execution_plan_hash"]},
    )
    assert status == 202
    assert env.gateway.wait_for_idle(30)
    status, artifact = _get(api, "/api/workbench/sessions/" + sid + "/result")
    assert status == 200 and artifact["media_type"] == "text/plain"
    assert artifact["filename"].endswith(".txt")
    assert _get(api, "/api/workbench/sessions/" + sid + "/diff")[0] == 422


@pytest.mark.parametrize(
    "extra",
    [
        {"reference_text": 42},
        {"task_kind": True},
        {"output_format": "html"},
        {"relative_path": "hello.py"},
        {"skip_approval": True},
    ],
)
def test_invalid_text_shape_is_rejected_before_spawn(
    env: WorkbenchEnv, extra: dict[str, Any]
) -> None:
    api = _api(env.services)
    body = {
        "provider_id": "codex",
        "model_id": "fake-model",
        "task_kind": "writing",
        "instruction": "x",
    }
    body.update(extra)
    assert _post(api, "/api/workbench/sessions", body)[0] in (400, 422)
    assert env.runner.calls == []
