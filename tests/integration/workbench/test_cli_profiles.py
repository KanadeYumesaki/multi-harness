"""Provider profile。**実行前制限と Fail-Closed 条件を組み立て段で確かめる。**

ここで測るのは argv・env・実行体同一性・Fail-Closed 条件である。Kernel 境界が
実際に効くかどうかは `test_boundary_gate.py` が実測する。だから能力検証は代役
（`StubBoundaryProbe`）にしてある。**承認や制限を飛ばしているのではない。**
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from harness.domain.errors import ErrorCode, HarnessError
from harness.infrastructure.provider.cli_profiles import (
    CLI_RUNTIME_CONTRACT,
    FORCED_POLICY_PATHS,
    CliRuntimeProfiles,
    load_runtime_manifest,
)
from harness.infrastructure.provider.cli_response import parse_cli_response

from .conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "tests" / "support"))
from workbench_fixtures import StubBoundaryProbe

pytestmark = pytest.mark.integration


def _profile(tmp_path: Path, provider: str = "codex") -> CliRuntimeProfiles:
    executable = tmp_path / "bin" / "cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    document = {
        "contract": CLI_RUNTIME_CONTRACT,
        "home": str(tmp_path / "home"),
        "neutral_workdir": str(tmp_path / "state" / "cwd"),
        "state_dir": str(tmp_path / "state"),
        "cli_runtime_root": str(tmp_path / "bin"),
        "providers": {
            provider: {
                "display_name": provider,
                "package": "test/pkg",
                "package_version": "1.2.3",
                "argv_prefix": [str(executable)],
                "suggested_models": [],
            }
        },
    }
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    (tmp_path / "home").mkdir(exist_ok=True)
    return CliRuntimeProfiles(
        manifest=load_runtime_manifest(path), boundary_probe=StubBoundaryProbe()
    )


@pytest.mark.parametrize(
    ("provider", "required"),
    [
        (
            "codex",
            (
                "--sandbox",
                "read-only",
                "--ignore-user-config",
                "--ignore-rules",
                "--ephemeral",
                "--skip-git-repo-check",
                "tools.web_search=false",
                "mcp_servers={}",
                "hooks={}",
                "-",
            ),
        ),
        (
            "claude",
            (
                "--print",
                "--output-format",
                "json",
                "--restricted",
                "--tools",
                "--strict-mcp-config",
                "--disable-slash-commands",
                "--no-session-persistence",
                "--permission-mode",
                "manual",
                "--permission-prompts",
                "none",
            ),
        ),
        (
            "gemini",
            (
                "--output-format",
                "json",
                "--approval-mode",
                "plan",
                "--skip-trust",
                "--extensions",
                "none",
            ),
        ),
    ],
)
def test_each_provider_carries_its_verified_restrictions(
    tmp_path: Path, provider: str, required: tuple[str, ...]
) -> None:
    spec = _profile(tmp_path, provider).resolve(provider_id=provider, model_id="a-model")
    for flag in required:
        assert flag in spec.argv, f"{provider} argv is missing {flag}"
    assert spec.argv[-1] != "a-model" or provider != "codex"
    assert "a-model" in spec.argv


def test_gemini_gets_a_system_settings_file_that_empties_the_tool_set(tmp_path: Path) -> None:
    spec = _profile(tmp_path, "gemini").resolve(provider_id="gemini", model_id="a-model")
    settings_path = Path(spec.env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"])
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["tools"]["core"] == []
    assert settings["mcpServers"] == {}


def test_environment_is_built_not_inherited(tmp_path: Path) -> None:
    spec = _profile(tmp_path).resolve(provider_id="codex", model_id="a-model")
    assert set(spec.env) == {"HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "NO_COLOR", "CI"}
    assert spec.env["PATH"] == "/usr/bin:/bin"
    assert "HOME" not in spec.runtime_projection["env_values_non_secret"]


def test_model_shape_is_validated_and_never_invented(tmp_path: Path) -> None:
    profiles = _profile(tmp_path)
    assert profiles.manifest.providers[0].suggested_models == ()
    for bad in ("", "-leading-dash", "a model", "x" * 200, "m\nodel"):
        with pytest.raises(HarnessError) as error:
            profiles.resolve(provider_id="codex", model_id=bad)
        assert error.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION


def test_a_changed_executable_changes_the_runtime_identity(tmp_path: Path) -> None:
    profiles = _profile(tmp_path)
    first = profiles.resolve(provider_id="codex", model_id="a-model")
    profiles.verify(first)
    (tmp_path / "bin" / "cli").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    with pytest.raises(HarnessError) as error:
        profiles.verify(first)
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH


def test_a_non_empty_working_directory_blocks_the_provider(tmp_path: Path) -> None:
    profiles = _profile(tmp_path)
    spec = profiles.resolve(provider_id="codex", model_id="a-model")
    profiles.verify(spec)
    (tmp_path / "state" / "cwd" / "leftover.txt").write_text("x", encoding="utf-8")
    with pytest.raises(HarnessError) as error:
        profiles.verify(spec)
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
    assert not profiles.statuses()[0].profile_verified


def test_forced_environment_policy_files_are_checked_every_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """環境側の強制設定が現れたら **Provider を有効にしない**。"""
    profiles = _profile(tmp_path)
    spec = profiles.resolve(provider_id="codex", model_id="a-model")
    profiles.verify(spec)
    forced = tmp_path / "managed-settings.json"
    forced.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "harness.infrastructure.provider.cli_profiles.FORCED_POLICY_PATHS", (str(forced),)
    )
    with pytest.raises(HarnessError) as error:
        profiles.verify(spec)
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
    status = profiles.statuses()[0]
    assert status.profile_verified is False
    assert status.blocking_reason is not None
    assert "強制設定" in status.blocking_reason


def test_the_declared_forced_policy_paths_are_the_official_locations() -> None:
    assert "/etc/claude-code/managed-settings.json" in FORCED_POLICY_PATHS
    assert "/etc/gemini-cli/settings.json" in FORCED_POLICY_PATHS


def test_login_is_never_inferred(tmp_path: Path) -> None:
    status = _profile(tmp_path).statuses()[0]
    assert status.login_state == "UNVERIFIED"
    assert "login" in status.login_hint or "認証" in status.login_hint


def test_profile_rejects_relative_paths_and_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "contract": CLI_RUNTIME_CONTRACT,
                "home": "relative/home",
                "neutral_workdir": "/var/empty/x",
                "state_dir": "/var/empty/y",
                "cli_runtime_root": "/var/empty/z",
                "providers": {"codex": {"argv_prefix": ["/bin/true"]}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(HarnessError):
        load_runtime_manifest(path)
    path.write_text(
        json.dumps(
            {
                "contract": CLI_RUNTIME_CONTRACT,
                "home": "/home/x",
                "neutral_workdir": "/var/empty/x",
                "state_dir": "/var/empty/y",
                "cli_runtime_root": "/var/empty/z",
                "providers": {"codex": {"argv_prefix": ["relative/cli"]}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(HarnessError):
        load_runtime_manifest(path)


def test_the_real_claude_not_logged_in_envelope_is_refused() -> None:
    """実測した「未ログイン」応答。`subtype` は success でも `is_error` が真である。"""
    observed = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "permission_denials": [],
        "result": "Not logged in · Please run /login",
        "terminal_reason": "api_error",
    }
    with pytest.raises(HarnessError):
        parse_cli_response("claude", json.dumps(observed).encode("utf-8"), maximum_bytes=128 * 1024)


def test_profile_without_a_cli_runtime_root_is_refused(tmp_path: Path) -> None:
    """境界の基準になる Directory が無い profile は受け付けない。

    ここを省けるようにすると、どこまでを CLI の領域として許すのかが決まらない。
    """
    path = tmp_path / "no-root.json"
    path.write_text(
        json.dumps(
            {
                "contract": CLI_RUNTIME_CONTRACT,
                "home": "/home/x",
                "neutral_workdir": "/var/empty/x",
                "state_dir": "/var/empty/y",
                "providers": {"codex": {"argv_prefix": ["/bin/true"]}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(HarnessError) as error:
        load_runtime_manifest(path)
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
