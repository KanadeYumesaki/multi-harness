"""Setup contracts with a synthetic Profile; real readiness is measured separately."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

import prepare_preview as tool


@pytest.fixture
def fixture_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    # Synthetic filesystem policy for portability; no production flag or registry edit.
    # Actual WSL/native boundary is separately measured by the operational acceptance run.
    def open_directory(path: Path, policy: object) -> tuple[int, None]:
        for part in (path, *path.parents):
            if part.is_symlink():
                raise OSError(errno.ELOOP, "symlink denied")
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW), None

    monkeypatch.setattr(tool, "open_and_verify_workspace", open_directory)


@pytest.fixture
def prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture_boundary: None) -> Path:
    runtime = tmp_path / "runtime-installed"
    runtime.mkdir()

    def profile(directory: Path, runtime_root: Path) -> dict[str, Any]:
        document = {
            "providers": {"claude": {"suggested_models": []}},
            "fixture": "SYNTHETIC_PROFILE_ONLY",
        }
        (directory / "runtime/cli-runtime-profile.json").write_text(json.dumps(document))
        return document

    monkeypatch.setattr(tool, "_profile", profile)
    destination = tmp_path / "demo"
    tool.prepare(destination, runtime)
    return destination


def test_prepare_creates_detached_worktree_and_separated_state(prepared: Path) -> None:
    document = tool._load(prepared / "preview.json")
    assert document["workspace"] == str(prepared / "worktree")
    assert document["database"] == str(prepared / "state/state.sqlite3")
    assert document["artifact_root"] == str(prepared / "state/cas")
    assert (prepared / "worktree/.git").is_file()
    assert not (prepared / "state/state.sqlite3").exists()  # migration belongs to the production UI
    assert sorted(p.name for p in (prepared / "worktree").iterdir()) == [
        ".git",
        "hello.py",
        "notes.md",
    ]
    assert not list(prepared.rglob("*.db"))
    assert os.stat(prepared).st_mode & 0o777 == 0o700
    assert os.stat(prepared / "preview.json").st_mode & 0o777 == 0o600
    assert tool._git(["symbolic-ref", "-q", "HEAD"], cwd=prepared / "seed", stage="observe")


def test_start_uses_production_cli_and_no_approval_skip(prepared: Path) -> None:
    argv = tool.start_arguments(prepared, 0)
    assert argv[1:5] == ["-m", "harness.presentation.cli", "ui", "--repo-root"]
    assert "--workspace" in argv and "--cli-runtime-profile" in argv
    assert not any(
        word in " ".join(argv) for word in ("--yes", "--force", "--skip", "mock", "auto-approve")
    )


@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "broken-symlink"])
def test_existing_destination_is_never_overwritten(tmp_path: Path, kind: str) -> None:
    target = tmp_path / "existing"
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"retain these bytes")
    if kind == "directory":
        target.mkdir()
    elif kind == "file":
        target.write_bytes(b"existing")
    else:
        target.symlink_to(sentinel if kind == "symlink" else tmp_path / "missing")
    with pytest.raises(tool.SetupFailure, match="DESTINATION_ALREADY_EXISTS"):
        tool.prepare(target, tmp_path)
    assert sentinel.read_bytes() == b"retain these bytes"
    assert target.is_symlink() if "symlink" in kind else target.exists()


@pytest.mark.parametrize("path", ["relative", "C:/state", "/workspace/../state"])
def test_ambiguous_paths_are_rejected_before_commands(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tool, "_run", lambda *a, **k: pytest.fail("command must not start"))
    with pytest.raises(tool.SetupFailure, match="ABSOLUTE_NATIVE_PATH_REQUIRED"):
        tool.prepare(Path(path), Path("/home"))


def test_inside_source_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool, "_run", lambda *a, **k: pytest.fail("command must not start"))
    with pytest.raises(tool.SetupFailure, match="STATE_INSIDE_SOURCE_DENIED"):
        tool.prepare(tool.ROOT / "new-preview", tool.ROOT)


def test_symlink_parent_is_rejected_before_creation(tmp_path: Path, fixture_boundary: None) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(OSError):
        tool.prepare(link / "new", tmp_path)
    assert not (actual / "new").exists()


def test_failed_discovery_retains_partial_directory_and_classified_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture_boundary: None
) -> None:
    def fail(directory: Path, runtime_root: Path) -> dict[str, Any]:
        raise tool.SetupFailure("NO_OFFICIAL_CLI_FOUND", "official-cli-discovery")

    monkeypatch.setattr(tool, "_profile", fail)
    destination = tmp_path / "failed"
    with pytest.raises(tool.SetupFailure, match="NO_OFFICIAL_CLI_FOUND"):
        tool.prepare(destination, tmp_path)
    failure = tool._load(destination / "preparation-failed.json")
    assert failure == {
        "status": "FAILED",
        "code": "NO_OFFICIAL_CLI_FOUND",
        "stage": "official-cli-discovery",
    }
    assert (destination / "worktree/hello.py").is_file()
    assert not (destination / "preview.json").exists()
    with pytest.raises(tool.SetupFailure, match="DESTINATION_ALREADY_EXISTS"):
        tool.prepare(destination, tmp_path)


def test_git_and_profile_processes_have_no_shell_or_secret_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-redacted")
    monkeypatch.setenv("GIT_SSH_COMMAND", "unexpected command")
    monkeypatch.setenv("GIT_TRACE", "1")
    observed: dict[str, Any] = {}

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        observed.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, b"ok", b"")

    monkeypatch.setattr(tool.subprocess, "run", run)
    assert tool._git(["status", "--short"], cwd=tmp_path, stage="test") == b"ok"
    assert observed.get("shell", False) is False
    assert observed["timeout"] == 30 and observed["stdin"] == subprocess.DEVNULL
    assert observed["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert observed["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert not {"ANTHROPIC_API_KEY", "GIT_SSH_COMMAND", "GIT_TRACE"} & observed["env"].keys()
    assert "core.hooksPath=/dev/null" in observed["argv"]


@pytest.mark.parametrize("failure", ["stderr", "timeout", "unavailable"])
def test_command_failures_are_classified_without_external_payload(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 30, stderr=b"external-sensitive-body")
        if failure == "unavailable":
            raise OSError("external-sensitive-body")
        return subprocess.CompletedProcess(argv, 2, b"", b"external-sensitive-body")

    monkeypatch.setattr(tool.subprocess, "run", run)
    with pytest.raises(tool.SetupFailure) as caught:
        tool._run(["/synthetic-command"], cwd=tmp_path, stage="synthetic")
    assert "external-sensitive-body" not in str(caught.value)
    assert caught.value.stage == "synthetic"


@pytest.mark.parametrize("change", ["extra", "workspace", "database", "profile"])
def test_modified_metadata_or_profile_never_starts(prepared: Path, change: str) -> None:
    file = prepared / "preview.json"
    document = tool._load(file)
    if change == "extra":
        document["approve"] = True
    elif change == "workspace":
        document["workspace"] = str(prepared.parent / "different-workspace")
    elif change == "database":
        document["database"] = str(prepared / "worktree/state.sqlite3")
    else:
        (prepared / "runtime/cli-runtime-profile.json").write_bytes(b"changed")
    if change != "profile":
        file.write_text(json.dumps(document))
    with pytest.raises(tool.SetupFailure):
        tool.start_arguments(prepared, 0)


@pytest.mark.parametrize(
    "content",
    [b'{"contract":1,"contract":2}', b"[]", b"{", b"\xff", b"x" * (tool.MAX_METADATA_BYTES + 1)],
)
def test_malformed_setup_files_are_rejected(tmp_path: Path, content: bytes) -> None:
    file = tmp_path / "metadata.json"
    file.write_bytes(content)
    with pytest.raises(tool.SetupFailure):
        tool._load(file)


def test_metadata_symlink_is_rejected(prepared: Path) -> None:
    profile = prepared / "runtime/cli-runtime-profile.json"
    target = prepared / "saved-profile.json"
    profile.rename(target)
    profile.symlink_to(target)
    with pytest.raises(OSError):
        tool.start_arguments(prepared, 0)


def test_metadata_publication_does_not_overwrite_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "preview.json"
    target.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        tool._save_new(target, {"new": True})
    assert target.read_bytes() == b"original"
    assert (tmp_path / "preview.json.pending").is_file()


def test_profile_reader_handles_normal_access_time_changes(prepared: Path) -> None:
    profile = prepared / "runtime/cli-runtime-profile.json"
    os.utime(profile, (1, 2))
    raw = tool._read_file(profile)
    assert (
        hashlib.sha256(raw).hexdigest() == tool._load(prepared / "preview.json")["profile_sha256"]
    )


def test_start_rejects_invalid_port_without_exec(
    prepared: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(tool.os, "execve", lambda *a: pytest.fail("must not exec"))
    assert tool.main(["start", "--directory", str(prepared), "--port", "65536"]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "INVALID_LOCAL_PORT"


def test_operator_start_is_explicit_and_preserves_metadata(prepared: Path) -> None:
    before = (prepared / "preview.json").read_bytes()
    profile_before = (prepared / "runtime/cli-runtime-profile.json").read_bytes()
    assert "--operator-auth-session" not in tool.start_arguments(prepared, 0)
    identity = "local-uid:" + str(os.getuid())
    argv = tool.start_arguments(prepared, 0, operator_auth_session=identity)
    assert argv[-2:] == ["--operator-auth-session", identity]
    assert (prepared / "preview.json").read_bytes() == before
    assert (prepared / "runtime/cli-runtime-profile.json").read_bytes() == profile_before
    assert not (prepared / "state/state.sqlite3").exists()


@pytest.mark.parametrize("identity", ["local-uid:invalid", "local-uid:999999999", "other:session"])
def test_operator_identity_is_checked_before_reading_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: str
) -> None:
    monkeypatch.setattr(tool, "_load", lambda *a: pytest.fail("must reject identity first"))
    with pytest.raises(tool.HarnessError):
        tool.start_arguments(tmp_path / "absent", 0, operator_auth_session=identity)
    assert not (tmp_path / "absent").exists()


def test_main_passes_explicit_operator_identity_without_automatic_decision(
    prepared: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, Any] = {}

    def execve(executable: str, argv: list[str], environment: dict[str, str]) -> None:
        observed.update(executable=executable, argv=argv, environment=environment)

    monkeypatch.setattr(tool.os, "execve", execve)
    monkeypatch.setattr(tool.os, "chdir", lambda *a: None)
    identity = "local-uid:" + str(os.getuid())
    assert (
        tool.main(["start", "--directory", str(prepared), "--operator-auth-session", identity]) == 0
    )
    assert observed["argv"][-2:] == ["--operator-auth-session", identity]
    assert not any(word in observed["argv"] for word in ("--yes", "--auto-approve", "--force"))
    assert not (prepared / "state/state.sqlite3").exists()
