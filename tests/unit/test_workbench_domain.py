"""`domain/workbench.py` の規則。**Filesystem も Process も触らない。**"""

from __future__ import annotations

import pytest

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.workbench import (
    WorkbenchLimits,
    WorkbenchState,
    build_request_document,
    classify_target,
    is_terminal,
    parse_state,
    request_payload_bytes,
    request_projection_hash,
    require_transition,
    validate_instruction,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "relative_path",
    [
        "hello.py",
        "src/app.js",
        "docs/notes.md",
        "a/b/c/d.ts",
        "データ.py",
    ],
)
def test_ordinary_source_files_are_eligible(relative_path: str) -> None:
    assert classify_target(relative_path).eligible


@pytest.mark.parametrize(
    ("relative_path", "reason"),
    [
        ("", "NOT_A_RELATIVE_PATH"),
        ("/etc/passwd", "NOT_A_RELATIVE_PATH"),
        ("a/../b.py", "UNSAFE_PATH_COMPONENT"),
        ("./a.py", "UNSAFE_PATH_COMPONENT"),
        ("a//b.py", "UNSAFE_PATH_COMPONENT"),
        ("a\\b.py", "BACKSLASH_IN_PATH"),
        ("a\nb.py", "CONTROL_CHARACTER_IN_PATH"),
        (".git/config", "DENIED_DIRECTORY"),
        (".ssh/config", "DENIED_DIRECTORY"),
        (".codex/config.toml", "DENIED_DIRECTORY"),
        (".claude/settings.json", "DENIED_DIRECTORY"),
        (".gemini/settings.json", "DENIED_DIRECTORY"),
        ("node_modules/pkg/index.js", "DENIED_DIRECTORY"),
        ("design-source/registries/errors.yaml", "DENIED_DIRECTORY"),
        (".env", "DENIED_FILE_NAME"),
        (".env.local", "DENIED_FILE_NAME"),
        ("AGENTS.md", "DENIED_FILE_NAME"),
        ("CLAUDE.md", "DENIED_FILE_NAME"),
        ("registry-snapshot.json", "DENIED_FILE_NAME"),
        ("keys/server.pem", "DENIED_CREDENTIAL_SUFFIX"),
        ("keys/id_rsa", "DENIED_FILE_NAME"),
        ("deploy.sh", "SUFFIX_NOT_IN_ALLOWLIST"),
        ("run.ps1", "SUFFIX_NOT_IN_ALLOWLIST"),
        ("image.png", "SUFFIX_NOT_IN_ALLOWLIST"),
        ("Makefile", "SUFFIX_NOT_IN_ALLOWLIST"),
    ],
)
def test_denied_paths_carry_their_reason(relative_path: str, reason: str) -> None:
    decision = classify_target(relative_path)
    assert decision.denied
    assert decision.reason == reason


def test_path_depth_and_length_are_bounded() -> None:
    assert classify_target("/".join(["a"] * 20) + "/x.py").reason == "PATH_TOO_DEEP"
    assert classify_target("a" * 600 + ".py").reason == "PATH_TOO_LONG"


def test_case_variation_does_not_defeat_the_credential_suffix_rule() -> None:
    assert classify_target("keys/server.PEM").reason == "DENIED_CREDENTIAL_SUFFIX"


def test_instruction_limits() -> None:
    limits = WorkbenchLimits()
    assert validate_instruction("コメントを足す", limits) == "コメントを足す".encode()
    for bad in ("", "a\x00b", "x" * (limits.max_instruction_bytes + 1)):
        with pytest.raises(HarnessError) as error:
            validate_instruction(bad, limits)
        assert error.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION


def test_instruction_is_not_trimmed_or_reshaped() -> None:
    """確認した内容と送る内容をずらさない。**整形しない。**"""
    raw = "  末尾に空白がある依頼です。  \n"
    assert validate_instruction(raw, WorkbenchLimits()) == raw.encode("utf-8")


def test_limits_reject_impossible_combinations() -> None:
    with pytest.raises(ValueError):
        WorkbenchLimits(max_source_bytes=0)
    with pytest.raises(ValueError):
        WorkbenchLimits(timeout_seconds=3601)
    with pytest.raises(ValueError):
        WorkbenchLimits(max_request_bytes=10)


def _document(text: str = "print(1)\n") -> dict[str, object]:
    return build_request_document(
        provider_id="codex",
        model_id="fake-model",
        relative_path="hello.py",
        source_text=text,
        instruction="コメントを足す",
        limits=WorkbenchLimits(),
    )


def test_request_payload_is_deterministic_and_bounded() -> None:
    first = request_payload_bytes(_document(), WorkbenchLimits())
    second = request_payload_bytes(_document(), WorkbenchLimits())
    assert first == second
    assert request_projection_hash(_document()) == request_projection_hash(_document())
    with pytest.raises(HarnessError):
        request_payload_bytes(_document("x" * 200_000), WorkbenchLimits())


def test_request_document_never_asks_the_provider_to_write() -> None:
    rules = " ".join(str(rule) for rule in _document()["response_rules"])
    assert "Do not use any tool" in rules
    assert "Do not run commands" in rules
    assert set(_document()) == {
        "contract",
        "output_contract",
        "provider_id",
        "model_id",
        "target_relative_path",
        "instruction",
        "current_file_text",
        "limits",
        "response_rules",
    }


def test_state_machine_refuses_skips_and_rewinds() -> None:
    assert (
        require_transition(WorkbenchState.DRAFTED, WorkbenchState.SEND_APPROVED)
        is WorkbenchState.SEND_APPROVED
    )
    for current, target in (
        (WorkbenchState.DRAFTED, WorkbenchState.PROPOSAL_READY),
        (WorkbenchState.SEND_APPROVED, WorkbenchState.DRAFTED),
        (WorkbenchState.PROPOSAL_READY, WorkbenchState.APPLIED),
        (WorkbenchState.APPLIED, WorkbenchState.PROPOSAL_READY),
        (WorkbenchState.SEND_UNKNOWN, WorkbenchState.SEND_PREPARED),
    ):
        with pytest.raises(HarnessError) as error:
            require_transition(current, target)
        assert error.value.code is ErrorCode.EVENT_ORDER_VIOLATION


def test_terminal_states_have_no_successor() -> None:
    for state in (
        WorkbenchState.APPLIED,
        WorkbenchState.SEND_FAILED,
        WorkbenchState.SEND_UNKNOWN,
        WorkbenchState.APPLY_FAILED,
        WorkbenchState.APPLY_UNKNOWN,
    ):
        assert is_terminal(state)
    assert not is_terminal(WorkbenchState.DRAFTED)


def test_unknown_stored_state_is_rejected() -> None:
    with pytest.raises(HarnessError) as error:
        parse_state("NOT_A_STATE")
    assert error.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION


def test_expiry_arithmetic_uses_the_canonical_basis() -> None:
    """期限は「いま + TTL」であって、遠い未来ではない。

    最初の実装は Unix epoch を基準にした暦計算を独自に持っていて、期限が
    約 1969 年先になっていた。**期限切れ検査が黙って効かなくなる形**なので、
    往復一致と既知の値をここで固定する。
    """
    from harness.application.workbench_service import _plus_seconds
    from harness.domain.timestamps import timestamp_from_seconds, timestamp_seconds

    assert _plus_seconds("2026-09-08T00:33:44Z", 3600) == "2026-09-08T01:33:44Z"
    assert _plus_seconds("2026-12-31T23:59:59Z", 60) == "2027-01-01T00:00:59Z"
    assert _plus_seconds("2028-02-28T23:00:00Z", 3600) == "2028-02-29T00:00:00Z"
    assert _plus_seconds("2027-02-28T23:00:00Z", 3600) == "2027-03-01T00:00:00Z"
    for moment in (
        "0001-01-01T00:00:00Z",
        "1970-01-01T00:00:00Z",
        "2000-02-29T12:34:56Z",
        "2026-09-08T00:33:44Z",
        "2100-03-01T00:00:00Z",
    ):
        assert timestamp_from_seconds(timestamp_seconds(moment)) == moment


def test_expiry_is_always_after_the_moment_it_was_derived_from() -> None:
    from harness.application.workbench_service import _plus_seconds
    from harness.domain.timestamps import timestamp_seconds

    now = "2026-09-08T00:00:00Z"
    for ttl in (60, 3600, 86400):
        expires = _plus_seconds(now, ttl)
        assert timestamp_seconds(expires) - timestamp_seconds(now) == ttl
