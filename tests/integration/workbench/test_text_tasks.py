"""Real synthetic CLI processes, durable approvals, no target file and no apply."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.integration.workbench.conftest import WorkbenchEnv, make_env

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_bytes
from harness.infrastructure.sqlite.connection_factory import ConnectionFactory

pytestmark = pytest.mark.integration


def draft(env: WorkbenchEnv, **options: Any) -> dict[str, Any]:
    fields = {
        "provider_id": "codex",
        "model_id": "fake-model",
        "relative_path": None,
        "task_kind": "writing",
        "instruction": "合成の紹介文を作成してください。",
        "reference_text": "資料A: 合成企画",
        "output_format": "markdown",
    }
    fields.update(options)
    return env.gateway.create_session(**fields)


def send(env: WorkbenchEnv, document: dict[str, Any]) -> dict[str, Any]:
    sid, digest = document["session_id"], document["execution_plan_hash"]
    env.gateway.approve_send(sid, execution_plan_hash=digest)
    env.gateway.start_send(sid, execution_plan_hash=digest)
    assert env.gateway.wait_for_idle(30)
    return env.gateway.session(sid)


@pytest.mark.parametrize(
    "kind", ["writing", "summary", "planning", "comparison", "review", "consultation"]
)
def test_fileless_tasks_save_text_after_approval_and_never_write_files(
    tmp_path: Path, kind: str
) -> None:
    env = make_env(tmp_path, replacement="# 合成成果物\n\n提供資料に基づく草案。\n")
    before = env.read("hello.py"), env.read("notes.md")
    try:
        document = draft(env, task_kind=kind)
        assert env.runner.calls == []
        assert document["target"] is None and document["before_hash"] is None
        with pytest.raises(HarnessError) as error:
            env.gateway.start_send(
                document["session_id"], execution_plan_hash=document["execution_plan_hash"]
            )
        assert error.value.code is ErrorCode.APPROVAL_REQUIRED
        confirmation = env.gateway.confirmation(document["session_id"])
        payload = json.loads(confirmation["request_payload_text"])
        assert payload["task_kind"] == kind
        assert payload["reference_text"] == "資料A: 合成企画"
        assert "current_file_text" not in payload and "target_relative_path" not in payload
        result = send(env, document)
        assert result["state"] == "PROPOSAL_READY", result["failure"]
        assert result["can_apply"] is False and result["apply_execution_plan_hash"] is None
        artifact = env.gateway.result(document["session_id"])
        assert artifact["result_text"] == "# 合成成果物\n\n提供資料に基づく草案。\n"
        assert artifact["artifact_hash"] == str(hash_bytes(artifact["result_text"].encode()))
        assert artifact["filename"].endswith(".md")
        assert len(env.runner.calls) == 1
        assert before == (env.read("hello.py"), env.read("notes.md"))
        with ConnectionFactory(env.root / "db/state.sqlite3").connect() as connection:
            journal = connection.execute("SELECT state FROM cli_invocation_journal").fetchall()
            assert journal, "durable invocation evidence must exist"
    finally:
        env.services.close()


def test_text_result_blocks_every_file_effect_entrypoint(tmp_path: Path) -> None:
    env = make_env(tmp_path, replacement="本文")
    try:
        document = send(env, draft(env))
        sid = document["session_id"]
        for operation in (
            lambda: env.gateway.diff(sid),
            lambda: env.gateway.approve_apply(
                sid, apply_execution_plan_hash="invalid", proposal_hash="invalid"
            ),
            lambda: env.gateway.apply(sid, apply_execution_plan_hash="invalid"),
            lambda: env.gateway.recover(sid),
        ):
            with pytest.raises(HarnessError) as error:
                operation()
            assert error.value.code is ErrorCode.PATH_OUTSIDE_CAPABILITY
        assert len(env.runner.calls) == 1
        assert env.gateway.session(sid)["state"] == "PROPOSAL_READY"
    finally:
        env.services.close()


@pytest.mark.parametrize(
    "options",
    [
        {"relative_path": "hello.py"},
        {"task_kind": "unknown"},
        {"output_format": "html"},
        {"reference_text": "FDE-HARNESS-CANARY-REFERENCELEAK1"},
        {"reference_text": "x" * 65537},
    ],
)
def test_invalid_reference_or_target_never_starts_or_persists_a_session(
    tmp_path: Path, options: dict[str, Any]
) -> None:
    env = make_env(tmp_path)
    try:
        with pytest.raises(HarnessError):
            draft(env, **options)
        assert env.runner.calls == []
        assert env.gateway.overview()["sessions"] == []
    finally:
        env.services.close()


def test_text_results_survive_restart_and_continue_to_another_provider(tmp_path: Path) -> None:
    env = make_env(tmp_path, replacement="草案A", extra_providers={"claude": "OK"})
    try:
        first = send(env, draft(env))
        sid = first["session_id"]
    finally:
        env.services.close()
    reopened = make_env(tmp_path, replacement="草案B", extra_providers={"claude": "OK"})
    try:
        assert reopened.gateway.result(sid)["result_text"] == "草案A"
        second = draft(reopened, provider_id="claude", task_kind="review", parent_session_id=sid)
        payload = json.loads(
            reopened.gateway.confirmation(second["session_id"])["request_payload_text"]
        )
        prior = payload["conversation_history"]["turns"][0]
        assert prior["result_text"] == "草案A" and prior["reference_text"] == "資料A: 合成企画"
        assert prior["proposal_applied"] is False and prior["replacement_text"] is None
        assert send(reopened, second)["state"] == "PROPOSAL_READY"
    finally:
        reopened.services.close()


def test_text_and_file_history_preserve_their_different_meanings(tmp_path: Path) -> None:
    env = make_env(tmp_path, replacement="草案A")
    try:
        first = send(env, draft(env))
        file = env.gateway.create_session(
            provider_id="codex",
            model_id="fake-model",
            relative_path="hello.py",
            instruction="コメントを追加",
            parent_session_id=first["session_id"],
        )
        payload = json.loads(env.gateway.confirmation(file["session_id"])["request_payload_text"])
        assert payload["conversation_history"]["turns"][0]["result_text"] == "草案A"
        assert send(env, file)["can_apply"] is True
        third = draft(env, parent_session_id=file["session_id"])
        turns = json.loads(env.gateway.confirmation(third["session_id"])["request_payload_text"])[
            "conversation_history"
        ]["turns"]
        assert turns[0]["result_text"] == "草案A" and turns[1]["replacement_text"] == "草案A"
        assert turns[1]["proposal_applied"] is False
    finally:
        env.services.close()


def test_secret_in_text_output_is_not_stored_or_downloadable(tmp_path: Path) -> None:
    env = make_env(tmp_path, replacement="FDE-HARNESS-CANARY-TEXTLEAK1")
    try:
        document = send(env, draft(env))
        assert document["state"] == "SEND_FAILED"
        assert document["failure"]["class"] == "PROPOSAL_REJECTED_BY_SCANNER"
        with pytest.raises(HarnessError):
            env.gateway.result(document["session_id"])
        assert not any(
            b"FDE-HARNESS-CANARY-TEXTLEAK1" in p.read_bytes()
            for p in (env.root / "cas").rglob("*")
            if p.is_file()
        )
    finally:
        env.services.close()


def test_chatgpt_http_generates_text_with_the_same_durable_send_boundary(
    workbench: WorkbenchEnv,
) -> None:
    from chatgpt_fixtures import completed

    from .test_chatgpt_generation import connect_fixture

    auth, transport = connect_fixture(workbench)
    before = workbench.read("hello.py"), workbench.read("notes.md")
    try:
        text = "# 合成の文章成果物\n"
        transport.stream = completed(json.dumps({"result_text": text}, ensure_ascii=False))
        document = draft(workbench, provider_id="chatgpt", model_id="fixture-model")
        confirmation = workbench.gateway.confirmation(document["session_id"])
        wire = json.loads(confirmation["request_payload_text"])
        assert "result_text" in wire["instructions"]
        assert not any(url.endswith("/responses") for _, url, _ in transport.calls)
        observed = []

        def check_durable() -> None:
            with ConnectionFactory(workbench.root / "db/state.sqlite3").connect() as connection:
                state = connection.execute(
                    "SELECT state FROM cli_invocation_journal WHERE session_id=?",
                    (document["session_id"],),
                ).fetchone()
                consumed = connection.execute(
                    "SELECT COUNT(*) FROM approval_consume_ticket WHERE state='CONSUMED'"
                ).fetchone()
                observed.append((state[0], consumed[0]))

        transport.on_inference = check_durable
        finished = send(workbench, document)
        assert finished["state"] == "PROPOSAL_READY"
        assert finished["can_apply"] is False
        assert observed == [("EXECUTION_ATTEMPTED", 1)]
        assert workbench.gateway.result(document["session_id"])["result_text"] == text
        assert [body for _, url, body in transport.calls if url.endswith("/responses")] == [
            confirmation["request_payload_text"].encode()
        ]
        assert workbench.runner.calls == []
        assert before == (workbench.read("hello.py"), workbench.read("notes.md"))
    finally:
        auth.close()


def test_purpose_reference_and_format_are_bound_to_deterministic_plan_content(
    workbench: WorkbenchEnv,
) -> None:
    first = draft(workbench)
    identical = draft(workbench)
    assert first["plan_content_hash"] == identical["plan_content_hash"]
    assert first["execution_plan_hash"] != identical["execution_plan_hash"]
    for changed in (
        {"task_kind": "review"},
        {"reference_text": "資料B: 別の合成条件"},
        {"output_format": "text"},
    ):
        assert draft(workbench, **changed)["plan_content_hash"] != first["plan_content_hash"]
    assert workbench.runner.calls == []
