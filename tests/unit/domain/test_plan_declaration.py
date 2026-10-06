"""Plan 宣言の検査規則と、Task 入力の形式判定。

ここで固定するのは次の3点である。

1. 足りない宣言を既定値で補わない。補えば、宣言していない Runtime で Plan が立つ。
2. Semantic Hash へ採番 ID と時刻を入れない（不変条件#4）。
3. 読めない入力を「空だった」へ倒さない。
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.plan_declaration import (
    InvocationDeclaration,
    parse_plan_declaration,
)
from harness.domain.task_input import TaskFormat, classify_task_bytes, decode_task_text

pytestmark = pytest.mark.unit

_EXECUTABLE_SHA = "sha256:" + "a" * 64

_DECLARATION: dict[str, Any] = {
    "planner_algorithm_version": "harness-planner/1.0",
    "planner_identity": "harness-planner/mvp0a",
    "authority_scope": "ACTION_EXECUTION",
    "intent": {
        "action_type": "LOCAL_FILE_PROPOSAL",
        "data_classification": "SYNTHETIC",
        "trust_level": "UNTRUSTED_INPUT",
        "risk_level": "LOW",
        "maximum_attempts": 1,
        "timeout_seconds": 60,
    },
    "runtime_envelope": {
        "runtime_spec_version": "1.0.0",
        "runtime_type": "MOCK_ADAPTER",
        "launcher_version": "harness-launcher/1.0",
        "executable_path": "/usr/bin/python3",
        "executable_sha256": _EXECUTABLE_SHA,
        "argv": ["harness-mock-provider", "--offline"],
        "working_directory": "/workspace",
        "workspace_id": "ws-1",
        "os_boundary": "LINUX",
        "provider": "mock",
        "adapter": "mock-adapter/1.0",
        "auth_route": "NONE",
    },
    "invocation": {
        "invocation_mode": "MOCK",
        "provider": "mock",
        "billing_mode": "FREE",
        "expected_effect": "NONE",
    },
    "token_profile": {
        "snapshot_id": "profile-1",
        "provider": "mock",
        "model": "mock-model",
        "tokenizer_name": "harness-utf8-byte-upper-bound",
        "tokenizer_version": "1.0.0",
        "counting_adapter_version": "harness-token-counter/2",
        "context_limit": 4096,
        "maximum_output_limit": 1024,
        "estimate_assurance": "CONSERVATIVE",
        "overheads": {
            "system_message_overhead": 3,
            "developer_message_overhead": 0,
            "tool_definition_overhead": 0,
            "per_message_overhead": 2,
            "structured_output_overhead": 0,
            "streaming_frame_overhead": 0,
            "retry_fallback_reservation": 0,
        },
        "retrieved_at": "2026-09-05T00:00:00Z",
        "expires_at": "2027-09-05T00:00:00Z",
    },
    "token_budget": {
        "policy_id": "budget-1",
        "total_tokens": 4000,
        "reserved_output_tokens": 16,
        "reserved_tool_tokens": 0,
    },
    "actions": [
        {
            "action_type": "LOCAL_FILE_WRITE",
            "normalized_inputs": {"source": "task"},
            "normalized_outputs": {"target_relative_path": "docs/out.md"},
        }
    ],
}


def _document(**overrides: Any) -> dict[str, Any]:
    document = copy.deepcopy(_DECLARATION)
    for dotted, value in overrides.items():
        section, _, field = dotted.partition("__")
        if field:
            document[section][field] = value
        else:
            document[section] = value
    return document


def test_a_complete_declaration_parses() -> None:
    declaration = parse_plan_declaration(_document())
    assert declaration.invocation.invocation_mode == "MOCK"
    assert declaration.runtime_envelope.argv == ("harness-mock-provider", "--offline")
    assert len(declaration.actions) == 1


@pytest.mark.parametrize(
    "section", ["intent", "runtime_envelope", "invocation", "token_profile", "token_budget"]
)
def test_a_missing_section_is_rejected(section: str) -> None:
    """欠けた宣言を既定値で補わない。"""
    document = copy.deepcopy(_DECLARATION)
    del document[section]
    with pytest.raises(HarnessError) as caught:
        parse_plan_declaration(document)
    assert caught.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION


def test_an_unknown_top_level_key_is_rejected() -> None:
    """綴り違いを黙って捨てない。捨てると宣言が効いていないことに気付けない。"""
    with pytest.raises(HarnessError):
        parse_plan_declaration(_document(endpoint="https://example.invalid"))


def test_a_shell_string_argv_is_rejected() -> None:
    """`argv` を文字列で書けないこと（不変条件#8）。"""
    with pytest.raises(HarnessError) as caught:
        parse_plan_declaration(_document(runtime_envelope__argv="mock-provider < input"))
    assert "shell string" in str(caught.value)


def test_a_relative_executable_path_is_rejected() -> None:
    with pytest.raises(HarnessError):
        parse_plan_declaration(_document(runtime_envelope__executable_path="python3"))


def test_an_unknown_os_boundary_is_rejected() -> None:
    with pytest.raises(HarnessError):
        parse_plan_declaration(_document(runtime_envelope__os_boundary="WINDOWS"))


def test_unknown_billing_mode_is_rejected() -> None:
    """`billing_mode=UNKNOWN` では Runtime GO 不可（§7 制約）。"""
    with pytest.raises(HarnessError):
        parse_plan_declaration(_document(invocation__billing_mode="UNKNOWN"))


def test_an_effect_bearing_invocation_is_rejected_while_planning() -> None:
    """CC-03 は作用を起こさない。起こす宣言をここで受理しない。"""
    with pytest.raises(HarnessError):
        parse_plan_declaration(_document(invocation__expected_effect="WORKSPACE_WRITE"))


def test_external_invocation_mode_is_rejected() -> None:
    with pytest.raises(HarnessError):
        parse_plan_declaration(_document(invocation__invocation_mode="EXTERNAL"))


def test_empty_actions_are_rejected() -> None:
    with pytest.raises(HarnessError):
        parse_plan_declaration(_document(actions=[]))


def test_expired_token_profile_bounds_are_rejected() -> None:
    """`expires_at <= retrieved_at` を Domain の不変条件のまま弾くこと。"""
    document = _document()
    document["token_profile"]["expires_at"] = document["token_profile"]["retrieved_at"]
    with pytest.raises(HarnessError):
        parse_plan_declaration(document)


def test_semantic_hashes_ignore_numbered_ids_and_timestamps() -> None:
    """Semantic Hash に採番 ID と時刻が入っていないこと（§3.7.1）。

    `InvocationDeclaration` は `invocation_id` を持たない。持たなければ
    混入しようがない。ここでは導出値だけが Hash を動かすことを示す。
    """
    declaration = parse_plan_declaration(_document())
    from harness.domain.hashing import hash_canonical

    def _hash(bundle_marker: str) -> str:
        return str(
            declaration.invocation.semantic_hash(
                context_bundle_hash=hash_canonical(
                    {"bundle": bundle_marker}, artifact_type="test-value", schema_major=1
                ),
                message_role_manifest_hash=hash_canonical(
                    {"roles": 1}, artifact_type="test-value", schema_major=1
                ),
                control_data_policy_hash=hash_canonical(
                    {"policy": 1}, artifact_type="test-value", schema_major=1
                ),
                token_profile_snapshot_hash=declaration.token_profile.snapshot_hash,
                instruction_hash=declaration.intent.semantic_hash,
                request_artifact_hash=hash_canonical(
                    {"artifact": 1}, artifact_type="test-value", schema_major=1
                ),
            )
        )

    assert _hash("a") == _hash("a")
    assert _hash("a") != _hash("b")


def test_invocation_declaration_rejects_unknown_vocabulary_directly() -> None:
    """値オブジェクト単体でも語彙外を拒むこと。Parser 経由だけの防御にしない。"""
    with pytest.raises(HarnessError):
        InvocationDeclaration(
            invocation_mode="MOCK",
            provider="mock",
            billing_mode="COMPLIMENTARY",
            expected_effect="NONE",
        )


# -- Task 入力の形式判定 -----------------------------------------------------


@pytest.mark.parametrize(
    ("relative_path", "payload", "expected"),
    [
        ("docs/task.md", b"# title\n", TaskFormat.MARKDOWN),
        ("docs/task.markdown", b"# title\n", TaskFormat.MARKDOWN),
        ("docs/task.txt", b"plain\n", TaskFormat.TEXT),
        ("docs/task.json", b'{"a": 1}\n', TaskFormat.JSON),
        ("docs/task.py", b"print('data')\n", TaskFormat.SOURCE),
        ("docs/task.sql", b"select 1;\n", TaskFormat.SOURCE),
    ],
)
def test_supported_formats_are_classified(
    relative_path: str, payload: bytes, expected: TaskFormat
) -> None:
    assert classify_task_bytes(relative_path, payload) is expected


def test_an_unknown_suffix_is_not_treated_as_text() -> None:
    with pytest.raises(HarnessError) as caught:
        classify_task_bytes("docs/task.bin", b"bytes\n")
    assert caught.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION


def test_invalid_json_is_rejected_even_with_a_json_suffix() -> None:
    with pytest.raises(HarnessError):
        classify_task_bytes("docs/task.json", b'{"a": \n')


def test_invalid_utf8_is_rejected_for_every_format() -> None:
    """置換文字で埋めて「読めた」ことにしない。"""
    with pytest.raises(HarnessError) as caught:
        classify_task_bytes("docs/task.md", b"# broken \xff\xfe\n")
    assert caught.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION


def test_decode_task_text_reports_where_it_failed() -> None:
    with pytest.raises(HarnessError) as caught:
        decode_task_text(b"\xff", where="task input 'x.md'")
    assert "x.md" in str(caught.value)


def test_source_classification_does_not_imply_execution() -> None:
    """Source と判定しても、返るのは形式の名前だけであること。

    実行可否や Role を返さない。返せば、拡張子が権限を決めることになる。
    """
    result = classify_task_bytes("docs/task.sh", b"rm -rf /\n")
    assert result is TaskFormat.SOURCE
    assert isinstance(result.value, str)
