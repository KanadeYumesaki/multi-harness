"""Full conversation handoff through real storage, approval and fake CLI processes."""

from __future__ import annotations

import json
from contextlib import contextmanager
from copy import copy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from harness.domain.canonical import canonicalize
from harness.domain.context_budget import MessageRole
from harness.domain.conversation import derive_message_content_hash
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash, hash_bytes
from harness.domain.workbench import WorkbenchLimits, history_hash_projection
from harness.ports.cli_workbench import SavedConversationHistory

from .conftest import WorkbenchEnv, make_env, run_to_proposal

pytestmark = pytest.mark.integration


def draft(env: WorkbenchEnv, **kwargs: Any) -> dict[str, Any]:
    return env.gateway.create_session(
        provider_id=kwargs.pop("provider_id", "codex"),
        model_id="fake-model",
        relative_path=kwargs.pop("relative_path", "hello.py"),
        instruction=kwargs.pop("instruction", "続きの説明を加えてください。"),
        **kwargs,
    )


def generate(env: WorkbenchEnv, session: dict[str, Any]) -> dict[str, Any]:
    env.gateway.approve_send(
        session["session_id"], execution_plan_hash=session["execution_plan_hash"]
    )
    env.gateway.start_send(
        session["session_id"], execution_plan_hash=session["execution_plan_hash"]
    )
    assert env.gateway.wait_for_idle(120)
    result = env.gateway.session(session["session_id"])
    assert result["state"] == "PROPOSAL_READY", result.get("job")
    return result


def apply(env: WorkbenchEnv, session: dict[str, Any]) -> dict[str, Any]:
    env.gateway.approve_apply(
        session["session_id"],
        apply_execution_plan_hash=session["apply_execution_plan_hash"],
        proposal_hash=session["proposal_hash"],
    )
    return env.gateway.apply(
        session["session_id"], apply_execution_plan_hash=session["apply_execution_plan_hash"]
    )


def request(env: WorkbenchEnv, session: dict[str, Any]) -> dict[str, Any]:
    return json.loads(env.gateway.confirmation(session["session_id"])["request_payload_text"])


def local_conversation(env: WorkbenchEnv) -> str:
    services = env.services
    conversation_id = services.id_source.new_id()
    services.conversations.create_conversation(
        conversation_id=conversation_id,
        record_id=services.id_source.new_id(),
        created_at=services.clock.now(),
        producer="handoff-test",
    )
    return conversation_id


def append(
    env: WorkbenchEnv, conversation_id: str, text: str, role: MessageRole = MessageRole.USER_TASK
) -> None:
    services = env.services
    services.conversations.append_message(
        conversation_id=conversation_id,
        role=role,
        body=text.encode("utf-8"),
        message_id=services.id_source.new_id(),
        record_id=services.id_source.new_id(),
        created_at=services.clock.now(),
        producer="handoff-test",
        artifact_id=services.id_source.new_id(),
    )


def test_codex_claude_gemini_inherit_all_prior_requests_results_and_observations(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path, extra_providers={"claude": "OK", "gemini": "OK"})
    try:
        original = env.read("hello.py")
        local = local_conversation(env)
        append(env, local, "利用者の最初の目的")
        append(env, local, "前のAIからの回答", MessageRole.UNTRUSTED_PROVIDER_DATA)
        first = apply(env, generate(env, draft(env, instruction="一つ目", conversation_id=local)))
        second = apply(
            env,
            generate(
                env,
                draft(
                    env,
                    provider_id="claude",
                    instruction="二つ目",
                    parent_session_id=first["session_id"],
                ),
            ),
        )
        third = draft(
            env, provider_id="gemini", instruction="三つ目", parent_session_id=second["session_id"]
        )
        payload = request(env, third)
        history = payload["conversation_history"]
        assert payload["contract"] == "cli-workbench-request/2"
        assert [m["body"] for m in history["local_messages"]] == [
            "利用者の最初の目的",
            "前のAIからの回答",
        ]
        assert [t["provider_id"] for t in history["turns"]] == ["codex", "claude"]
        assert [t["instruction"] for t in history["turns"]] == ["一つ目", "二つ目"]
        assert [t["sequence"] for t in history["turns"]] == [1, 2]
        assert all(t["proposal_applied"] for t in history["turns"])
        assert all(t["verification"]["apply_result"]["target_matches"] for t in history["turns"])
        assert history["turns"][0]["current_file_text_at_request"] == original
        assert history["turns"][0]["unified_diff"]
        assert history["omitted_messages"] == 0
        before = len(env.runner.calls)
        third = generate(env, third)
        assert len(env.runner.calls) == before + 1
        assert json.loads(env.runner.payloads[-1]) == payload
        assert third["provider_id"] == "gemini"
        assert apply(env, third)["state"] == "APPLIED"
        assert len(env.gateway.history(third["session_id"])["history"]["turns"]) == 3
    finally:
        env.services.close()


def test_unapplied_proposal_is_included_but_never_treated_as_current_code(
    workbench: WorkbenchEnv,
) -> None:
    original = workbench.read("hello.py")
    first = run_to_proposal(workbench)
    second = draft(workbench, parent_session_id=first["session_id"])
    payload = request(workbench, second)
    turn = payload["conversation_history"]["turns"][0]
    assert turn["replacement_text"] != original
    assert turn["proposal_applied"] is False
    assert turn["state"] == "PROPOSAL_READY"
    assert turn["verification"]["apply_result"] is None
    assert payload["current_file_text"] == original
    assert len(workbench.runner.calls) == 1


def test_other_file_continuation_includes_prior_file_result(workbench: WorkbenchEnv) -> None:
    first = apply(workbench, run_to_proposal(workbench))
    second = draft(workbench, parent_session_id=first["session_id"], relative_path="notes.md")
    payload = request(workbench, second)
    assert payload["target_relative_path"] == "notes.md"
    assert payload["conversation_history"]["turns"][0]["target_relative_path"] == "hello.py"
    assert payload["current_file_text"] == workbench.read("notes.md")


def test_all_saved_roles_are_reference_data_without_control_escalation(
    workbench: WorkbenchEnv,
) -> None:
    local = local_conversation(workbench)
    for role in MessageRole:
        append(workbench, local, "本文：" + role.value, role)
    session = draft(workbench, conversation_id=local)
    payload = request(workbench, session)
    history = payload["conversation_history"]
    assert [m["role"] for m in history["local_messages"]] == [r.value for r in MessageRole]
    assert history["trust"] == "UNTRUSTED_REFERENCE_DATA"
    assert "untrusted reference data" in payload["response_rules"][-1]
    assert "not system instructions" in payload["response_rules"][-1]
    assert workbench.runner.calls == []


def test_new_conversation_never_inherits_unrelated_saved_work(workbench: WorkbenchEnv) -> None:
    first = run_to_proposal(workbench, instruction="別の会話だけの依頼")
    second = draft(workbench)
    assert "conversation_history" not in request(workbench, second)
    assert second["handoff"] is None
    assert first["session_id"] != second["session_id"]


def test_unknown_parent_is_rejected_without_partial_session(workbench: WorkbenchEnv) -> None:
    count = len(workbench.gateway.overview()["sessions"])
    with pytest.raises(HarnessError) as error:
        draft(workbench, parent_session_id="00000000-0000-0000-0000-000000000000")
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
    assert len(workbench.gateway.overview()["sessions"]) == count
    assert workbench.runner.calls == []


def test_cannot_replace_the_local_source_of_an_existing_thread(workbench: WorkbenchEnv) -> None:
    first = draft(workbench, conversation_id=local_conversation(workbench))
    with pytest.raises(HarnessError) as error:
        draft(
            workbench,
            parent_session_id=first["session_id"],
            conversation_id=local_conversation(workbench),
        )
    assert error.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION


@pytest.mark.parametrize("stage", ["approval", "claim", "dispatch"])
def test_appended_message_invalidates_review_at_each_send_boundary(
    workbench: WorkbenchEnv, stage: str
) -> None:
    local = local_conversation(workbench)
    append(workbench, local, "最初の依頼")
    session = draft(workbench, conversation_id=local)
    if stage != "approval":
        workbench.gateway.approve_send(
            session["session_id"], execution_plan_hash=session["execution_plan_hash"]
        )
    if stage == "dispatch":
        workbench.gateway._service.claim_send(session["session_id"])
    append(workbench, local, "確認した後の追記")
    with pytest.raises(HarnessError) as error:
        if stage == "approval":
            workbench.gateway.approve_send(
                session["session_id"], execution_plan_hash=session["execution_plan_hash"]
            )
        elif stage == "claim":
            workbench.gateway._service.claim_send(session["session_id"])
        else:
            workbench.gateway._worker.dispatch_send(session["session_id"])
    assert error.value.code is ErrorCode.APPROVAL_INVALIDATED
    assert workbench.runner.calls == []
    frozen = request(workbench, session)["conversation_history"]["local_messages"]
    assert [m["body"] for m in frozen] == ["最初の依頼"]


def test_history_is_rechecked_inside_the_approval_transaction(
    workbench: WorkbenchEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = local_conversation(workbench)
    append(workbench, local, "最初の依頼")
    session = draft(workbench, conversation_id=local)
    service = workbench.gateway._service
    original_begin = service.uow.begin_immediate

    @contextmanager
    def changed_before_lock() -> Any:
        append(workbench, local, "Transactionを取る直前の変更")
        with original_begin():
            yield

    monkeypatch.setattr(service.uow, "begin_immediate", changed_before_lock)
    with pytest.raises(HarnessError) as error:
        workbench.gateway.approve_send(
            session["session_id"], execution_plan_hash=session["execution_plan_hash"]
        )
    assert error.value.code is ErrorCode.APPROVAL_INVALIDATED
    assert workbench.gateway.session(session["session_id"])["state"] == "DRAFTED"
    assert workbench.runner.calls == []


def test_parent_proposal_change_after_review_invalidates_child(workbench: WorkbenchEnv) -> None:
    first = draft(workbench)
    child = draft(workbench, parent_session_id=first["session_id"])
    generate(workbench, first)
    with pytest.raises(HarnessError) as error:
        workbench.gateway.approve_send(
            child["session_id"], execution_plan_hash=child["execution_plan_hash"]
        )
    assert error.value.code is ErrorCode.APPROVAL_INVALIDATED
    assert len(workbench.runner.calls) == 1


@pytest.mark.parametrize(
    "state",
    [
        "SEND_APPROVED",
        "SEND_PREPARED",
        "SEND_ATTEMPTED",
        "SEND_UNKNOWN",
        "APPLY_APPROVED",
        "APPLY_PREPARED",
        "APPLY_ATTEMPTED",
        "APPLY_UNKNOWN",
    ],
)
def test_active_or_unknown_parent_never_causes_a_fallback_send(
    workbench: WorkbenchEnv, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    parent = draft(workbench)
    service = workbench.gateway._service
    original = service.inspect

    def inspect(session_id: str) -> dict[str, Any]:
        document = original(session_id)
        if session_id == parent["session_id"]:
            document["state"] = state
        return document

    monkeypatch.setattr(service, "inspect", inspect)
    with pytest.raises(HarnessError) as error:
        draft(workbench, parent_session_id=parent["session_id"])
    assert error.value.code is ErrorCode.UNRECONCILED_EFFECT_PRESENT
    assert workbench.runner.calls == []


def test_failed_send_history_preserves_failure_and_does_not_resend_it(tmp_path: Path) -> None:
    env = make_env(tmp_path, mode="NON_ZERO", extra_providers={"claude": "OK"})
    try:
        failed = run_to_proposal(env)
        assert failed["state"] == "SEND_FAILED"
        child = draft(env, provider_id="claude", parent_session_id=failed["session_id"])
        turn = request(env, child)["conversation_history"]["turns"][0]
        assert turn["failure"]["class"] == "NON_ZERO_EXIT"
        assert turn["replacement_text"] is None
        assert len(env.runner.calls) == 1
    finally:
        env.services.close()


def test_history_survives_service_restart_without_cli_session_resume(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    first = apply(env, run_to_proposal(env))
    env.services.close()
    restarted = make_env(tmp_path)
    try:
        child = draft(restarted, parent_session_id=first["session_id"])
        assert request(restarted, child)["conversation_history"]["turns"][0]["proposal_applied"]
        assert restarted.runner.calls == []
        assert restarted.gateway.history(child["session_id"])["binding"]["turn_count"] == 2
    finally:
        restarted.services.close()


def test_history_limit_is_enforced_without_last_n_truncation(workbench: WorkbenchEnv) -> None:
    first = run_to_proposal(workbench)
    service = workbench.gateway._service
    service.limits = WorkbenchLimits(
        max_source_bytes=128, max_instruction_bytes=128, max_request_bytes=512
    )
    count = len(workbench.gateway.overview()["sessions"])
    with pytest.raises(HarnessError) as error:
        draft(workbench, parent_session_id=first["session_id"])
    assert error.value.code is ErrorCode.CONTEXT_BUDGET_EXCEEDED
    assert len(workbench.gateway.overview()["sessions"]) == count
    assert len(workbench.runner.calls) == 1


def test_secret_in_history_is_rejected_without_storing_payload(
    workbench: WorkbenchEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = local_conversation(workbench)
    service = workbench.gateway._service
    canary = "FDE-HARNESS-CANARY-HISTORYLEAK1"
    fake_history = SavedConversationHistory(
        messages=({"sequence": 1, "role": "USER_TASK", "body": canary},),
        source_hash=hash_bytes(b"synthetic"),
    )
    monkeypatch.setattr(service.conversations, "read_history", lambda *a, **k: fake_history)
    with pytest.raises(HarnessError) as error:
        draft(workbench, conversation_id=local)
    assert error.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION
    assert canary not in str(error.value)
    assert workbench.gateway.overview()["sessions"] == []
    assert workbench.runner.calls == []


def test_cycle_is_detected_without_recursion_or_partial_history(
    workbench: WorkbenchEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = draft(workbench)
    service = workbench.gateway._service
    original = service.inspect

    def cyclic(session_id: str) -> dict[str, Any]:
        document = original(session_id)
        document["handoff"] = {"parent_session_id": session_id, "conversation_id": None}
        return document

    monkeypatch.setattr(service, "inspect", cyclic)
    with pytest.raises(HarnessError) as error:
        draft(workbench, parent_session_id=parent["session_id"])
    assert error.value.code is ErrorCode.EVENT_ORDER_VIOLATION
    assert workbench.runner.calls == []


def test_missing_local_artifact_prevents_partial_handoff(
    workbench: WorkbenchEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = local_conversation(workbench)
    append(workbench, local, "読み取れなければ省略しない本文")
    reader = workbench.gateway._service.conversations

    def missing(digest: Any) -> bytes:
        raise HarnessError(ErrorCode.STORAGE_WRITE_FAILED, "artifact missing")

    monkeypatch.setattr(reader._artifacts, "get", missing)
    with pytest.raises(HarnessError) as error:
        draft(workbench, conversation_id=local)
    assert error.value.code is ErrorCode.STORAGE_WRITE_FAILED
    assert workbench.runner.calls == []


def test_sequence_gap_and_message_metadata_change_are_rejected(
    workbench: WorkbenchEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = local_conversation(workbench)
    append(workbench, local, "一番目")
    reader = workbench.gateway._service.conversations
    row = reader._messages.list_for_conversation(local)[0]
    monkeypatch.setattr(
        reader._messages,
        "list_for_conversation",
        lambda _: (
            replace(
                row,
                sequence_number=2,
                content_hash=derive_message_content_hash(
                    content_artifact_hash=row.content_artifact_hash,
                    role=row.role,
                    sequence_number=2,
                ),
            ),
        ),
    )
    with pytest.raises(HarnessError) as error:
        draft(workbench, conversation_id=local)
    assert error.value.code is ErrorCode.EVENT_ORDER_VIOLATION
    corrupted = copy(row)
    object.__setattr__(corrupted, "content_hash", hash_bytes(b"changed"))
    monkeypatch.setattr(reader._messages, "list_for_conversation", lambda _: (corrupted,))
    with pytest.raises(HarnessError) as error:
        draft(workbench, conversation_id=local)
    assert error.value.code is ErrorCode.ARTIFACT_CONTENT_CONFLICT
    assert workbench.runner.calls == []


def test_history_from_different_workspace_is_rejected(
    workbench: WorkbenchEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = draft(workbench)
    service = workbench.gateway._service
    original = service.inspect

    def different(session_id: str) -> dict[str, Any]:
        document = original(session_id)
        document["workspace_identity"] = "different"
        return document

    monkeypatch.setattr(service, "inspect", different)
    with pytest.raises(HarnessError) as error:
        draft(workbench, parent_session_id=parent["session_id"])
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
    assert workbench.runner.calls == []


def test_missing_receipt_prevents_claiming_applied_history(
    workbench: WorkbenchEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = apply(workbench, run_to_proposal(workbench))
    service = workbench.gateway._service
    original = service._load_artifact

    def missing(digest: Any) -> dict[str, Any]:
        if str(digest) == parent["effect_receipt_hash"]:
            raise HarnessError(ErrorCode.ARTIFACT_CONTENT_CONFLICT, "missing receipt")
        return original(digest)

    monkeypatch.setattr(service, "_load_artifact", missing)
    with pytest.raises(HarnessError) as error:
        draft(workbench, parent_session_id=parent["session_id"])
    assert error.value.code is ErrorCode.ARTIFACT_CONTENT_CONFLICT
    assert len(workbench.runner.calls) == 1


def test_history_survives_harness_update_but_old_approval_cannot_execute(
    workbench: WorkbenchEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = draft(workbench)
    service = workbench.gateway._service
    monkeypatch.setattr(service, "runtime_identity", lambda: str(hash_bytes(b"updated-harness")))
    monkeypatch.setattr(
        workbench.gateway._worker, "runtime_identity", lambda: str(hash_bytes(b"updated-harness"))
    )
    child = draft(workbench, parent_session_id=parent["session_id"])
    history = request(workbench, child)["conversation_history"]
    assert history["turns"][0]["instruction"] == parent["instruction"]
    assert history["turns"][0]["state"] == "DRAFTED"
    with pytest.raises(HarnessError) as error:
        workbench.gateway.approve_send(
            parent["session_id"], execution_plan_hash=parent["execution_plan_hash"]
        )
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
    assert workbench.runner.calls == []
    assert generate(workbench, child)["state"] == "PROPOSAL_READY"


def test_generated_numeric_digest_is_lossless_without_weakening_the_scanner(
    workbench: WorkbenchEnv,
) -> None:
    # Synthetic digest, not an identity: the twelve zeros deliberately exercise
    # the same false detection as the independently measured failure.
    digest = ContentHash("sha256", "a" * 20 + "0" * 12 + "b" * 32)
    service = workbench.gateway._service
    with pytest.raises(HarnessError) as error:
        service._require_no_rejected_content(canonicalize({"hash": str(digest)}), "TEST")
    assert error.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION
    projection = history_hash_projection(digest)
    assert projection["algorithm"] == digest.algorithm
    assert "".join(projection["hex_groups"]) == digest.hexdigest
    service._require_no_rejected_content(canonicalize({"hash": projection}), "TEST")


def test_history_observation_codec_preserves_the_verified_receipt_and_apply_hashes(
    workbench: WorkbenchEnv,
) -> None:
    parent = apply(workbench, run_to_proposal(workbench))
    child = draft(workbench, parent_session_id=parent["session_id"])
    payload = request(workbench, child)
    history = payload["conversation_history"]
    assert history["contract"] == "workbench-conversation-history/2"
    verification = history["turns"][0]["verification"]

    def decode(value: dict[str, Any]) -> str:
        return value["algorithm"] + ":" + "".join(value["hex_groups"])

    assert decode(verification["effect_receipt_hash"]) == parent["effect_receipt_hash"]
    assert decode(verification["apply_result"]["observed_hash"]) == parent["proposal_hash"]
    assert decode(verification["apply_result"]["expected_hash"]) == parent["proposal_hash"]
    assert decode(verification["receipt_observation"]["observed_hash"]) == parent["proposal_hash"]
    assert decode(verification["receipt_observation"]["before_hash"]) == parent["before_hash"]
    assert "lossless" in payload["history_hash_encoding"]


def test_a_hash_looking_local_message_is_still_rejected(
    workbench: WorkbenchEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = local_conversation(workbench)
    service = workbench.gateway._service
    digest_text = str(ContentHash("sha256", "a" * 20 + "0" * 12 + "b" * 32))
    fake = SavedConversationHistory(
        messages=({"sequence": 1, "role": "USER_TASK", "body": digest_text},),
        source_hash=hash_bytes(b"synthetic source"),
    )
    monkeypatch.setattr(service.conversations, "read_history", lambda *a, **k: fake)
    count = len(workbench.gateway.overview()["sessions"])
    with pytest.raises(HarnessError) as error:
        draft(workbench, conversation_id=local)
    assert error.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION
    assert digest_text not in str(error.value)
    assert len(workbench.gateway.overview()["sessions"]) == count
    assert workbench.runner.calls == []
