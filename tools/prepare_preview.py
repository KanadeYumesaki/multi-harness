#!/usr/bin/env python3
"""Prepare a fresh, synthetic worktree for the real CLI Workbench; never send a request."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from harness.domain.errors import HarnessError  # noqa: E402
from harness.infrastructure.crypto.local_authority import LocalAuthority  # noqa: E402
from harness.infrastructure.filesystem.workspace_boundary import (  # noqa: E402
    FilesystemPolicy,
    open_and_verify_workspace,
)

CONTRACT = "real-cli-preview-setup/1"
MAX_METADATA_BYTES = 131072
DEMO_FILES = {
    "hello.py": 'def greet(name: str) -> str:\n    return f"Hello, {name}!"\n',
    "notes.md": "# Synthetic preview\n\nThis file contains no user data.\n",
}


class SetupFailure(RuntimeError):
    """A classified failure. External stderr and credentials are never part of the message."""

    def __init__(self, code: str, stage: str) -> None:
        super().__init__(code)
        self.code, self.stage = code, stage


def _absolute(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise SetupFailure("ABSOLUTE_NATIVE_PATH_REQUIRED", "paths")
    return Path(os.path.abspath(path))  # lexical only; do not resolve symlinks


def _verify_directory(path: Path) -> None:
    fd, _ = open_and_verify_workspace(path, FilesystemPolicy.load(ROOT))
    os.close(fd)


def _environment(*, home: Path | None = None) -> dict[str, str]:
    # Ignore inherited Git hooks/config, tracing, keys and Python startup settings.
    result = {
        "PATH": os.defpath,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    if home is not None:
        result["HOME"] = str(home)
    return result


def _run(argv: list[str], *, cwd: Path, stage: str, home: Path | None = None) -> bytes:
    try:
        result = subprocess.run(  # noqa: S603 - fixed git/Python argv; validated native paths
            argv,
            cwd=cwd,
            env=_environment(home=home),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise SetupFailure("PREVIEW_COMMAND_TIMEOUT", stage) from error
    except OSError as error:
        raise SetupFailure("PREVIEW_COMMAND_UNAVAILABLE", stage) from error
    if result.returncode != 0:
        raise SetupFailure("PREVIEW_COMMAND_REJECTED", stage)
    return result.stdout


def _git(operation: list[str], *, cwd: Path, stage: str) -> bytes:
    return _run(
        [
            "/usr/bin/git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=Harness Demo",
            "-c",
            "user.email=demo@example.invalid",
            *operation,
        ],
        cwd=cwd,
        stage=stage,
    )


def _read_file(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_METADATA_BYTES:
            raise SetupFailure("INVALID_SETUP_FILE", "metadata")
        with os.fdopen(os.dup(fd), "rb") as handle:
            raw = handle.read(MAX_METADATA_BYTES + 1)
        after = os.fstat(fd)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if len(raw) > MAX_METADATA_BYTES or any(
            getattr(before, name) != getattr(after, name) for name in fields
        ):
            raise SetupFailure("SETUP_FILE_CHANGED", "metadata")
        return raw
    finally:
        os.close(fd)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SetupFailure("DUPLICATE_SETUP_KEY", "metadata")
        result[key] = value
    return result


def _load(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(_read_file(path).decode("utf-8"), object_pairs_hook=_pairs)
    except (ValueError, UnicodeError) as error:
        raise SetupFailure("INVALID_SETUP_JSON", "metadata") from error
    if not isinstance(document, dict):
        raise SetupFailure("INVALID_SETUP_JSON", "metadata")
    return document


def _save_new(path: Path, document: dict[str, Any]) -> None:
    """Publish new operational metadata atomically without replacing an existing entry.

    This is setup metadata, not a CAS Artifact or Runtime Evidence. A hard-link publication
    rejects concurrent creation; it never replaces another writer's file. No cleanup of an
    incomplete environment is attempted: its files remain available for diagnosis.
    """
    raw = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
    temporary = path.with_name(path.name + ".pending")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        # Pending bytes are retained if publication fails. Never overwrite or erase them.
        raise


def _paths(directory: Path) -> dict[str, str]:
    return {
        "directory": str(directory),
        "workspace": str(directory / "worktree"),
        "database": str(directory / "state/state.sqlite3"),
        "artifact_root": str(directory / "state/cas"),
        "cli_runtime_profile": str(directory / "runtime/cli-runtime-profile.json"),
    }


def _profile(directory: Path, runtime_root: Path) -> dict[str, Any]:
    _run(
        [
            sys.executable,
            "-m",
            "harness.presentation.cli",
            "workbench-profile",
            "--out",
            str(directory / "runtime/cli-runtime-profile.json"),
            "--state-dir",
            str(directory / "runtime"),
            "--cli-runtime-root",
            str(runtime_root),
        ],
        cwd=ROOT,
        stage="official-cli-discovery",
        home=Path.home(),
    )
    profile = _load(directory / "runtime/cli-runtime-profile.json")
    if not isinstance(profile.get("providers"), dict) or not profile["providers"]:
        raise SetupFailure("NO_OFFICIAL_CLI_FOUND", "official-cli-discovery")
    return profile


def prepare(directory: Path, runtime_root: Path) -> dict[str, Any]:
    directory, runtime_root = _absolute(directory), _absolute(runtime_root)
    if directory.is_relative_to(ROOT):
        raise SetupFailure("STATE_INSIDE_SOURCE_DENIED", "paths")
    if directory.exists() or directory.is_symlink():
        raise SetupFailure("DESTINATION_ALREADY_EXISTS", "paths")
    _verify_directory(ROOT)
    _verify_directory(runtime_root)
    parent_fd, _ = open_and_verify_workspace(directory.parent, FilesystemPolicy.load(ROOT))
    try:
        os.mkdir(directory.name, mode=0o700, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    _verify_directory(directory)
    try:
        for name in ("seed", "state", "state/cas", "runtime", "runtime/workbench-cwd"):
            (directory / name).mkdir(mode=0o700)
        seed = directory / "seed"
        for name, text in DEMO_FILES.items():
            (seed / name).write_text(text, encoding="utf-8", newline="\n")
        for stage, operation in (
            ("git-init", ["init", "-b", "codex/preview-seed"]),
            ("git-add", ["add", "--", *sorted(DEMO_FILES)]),
            ("git-commit", ["commit", "-m", "chore: create synthetic CLI preview"]),
            ("git-worktree", ["worktree", "add", "--detach", str(directory / "worktree")]),
        ):
            _git(operation, cwd=seed, stage=stage)
        _verify_directory(directory / "worktree")
        profile = _profile(directory, runtime_root)
        document = {
            "contract": CONTRACT,
            **_paths(directory),
            "profile_sha256": hashlib.sha256(
                _read_file(directory / "runtime/cli-runtime-profile.json")
            ).hexdigest(),
            "providers": sorted(profile["providers"]),
            "scope": "SYNTHETIC_WORKTREE_REAL_CLI_PROFILE_NO_PROVIDER_REQUEST",
        }
        _save_new(directory / "preview.json", document)
        return document
    except (SetupFailure, HarnessError, OSError) as error:
        code = error.code if isinstance(error, SetupFailure) else "PREVIEW_PREPARATION_FAILED"
        stage = error.stage if isinstance(error, SetupFailure) else "setup"
        _save_new(
            directory / "preparation-failed.json",
            {"status": "FAILED", "code": code, "stage": stage},
        )
        raise


def start_arguments(
    directory: Path, port: int, *, operator_auth_session: str | None = None
) -> list[str]:
    # Explicit local identity opt-in only; each maintenance action still needs approval.
    if operator_auth_session is not None:
        LocalAuthority().subject(operator_auth_session)
    directory = _absolute(directory)
    _verify_directory(directory)
    document = _load(directory / "preview.json")
    expected = {"contract", *_paths(directory), "profile_sha256", "providers", "scope"}
    if set(document) != expected or document["contract"] != CONTRACT:
        raise SetupFailure("UNSUPPORTED_SETUP_METADATA", "metadata")
    if any(document[key] != value for key, value in _paths(directory).items()):
        raise SetupFailure("SETUP_PATH_MISMATCH", "metadata")
    for child in ("seed", "worktree", "state", "state/cas", "runtime", "runtime/workbench-cwd"):
        _verify_directory(directory / child)
    profile = _read_file(directory / "runtime/cli-runtime-profile.json")
    if hashlib.sha256(profile).hexdigest() != document["profile_sha256"]:
        raise SetupFailure("SETUP_PROFILE_CHANGED", "metadata")
    common = _git(
        ["rev-parse", "--git-common-dir"], cwd=directory / "worktree", stage="worktree-identity"
    )
    observed_common = Path(os.path.abspath(directory / "worktree" / common.decode().strip()))
    if observed_common != directory / "seed/.git":
        raise SetupFailure("WORKTREE_IDENTITY_CHANGED", "metadata")
    launch = [
        sys.executable,
        "-m",
        "harness.presentation.cli",
        "ui",
        "--repo-root",
        str(ROOT),
        "--database",
        document["database"],
        "--artifact-root",
        document["artifact_root"],
        "--workspace",
        document["workspace"],
        "--cli-runtime-profile",
        document["cli_runtime_profile"],
        "--workspace-label",
        "synthetic-cli-preview",
        "--port",
        str(port),
    ]
    if operator_auth_session is not None:
        launch.extend(["--operator-auth-session", operator_auth_session])
    return launch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="新しい隔離Worktreeと実CLI Profileを作る（送信しない）")
    init.add_argument("--directory", type=Path, required=True)
    init.add_argument(
        "--cli-runtime-root",
        type=Path,
        default=Path.home() / ".local/share/fde-harness/cli-runtime",
    )
    start = commands.add_parser(
        "start", help="本番UIを127.0.0.1に起動する。送信・適用は画面で別承認"
    )
    start.add_argument("--directory", type=Path, required=True)
    start.add_argument("--port", type=int, default=0)
    start.add_argument(
        "--operator-auth-session",
        help="受付と復旧を有効にする現在OS利用者のlocal-uid。各操作は画面で別承認",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            print(
                json.dumps(
                    {
                        "status": "PREPARED_NOT_PROVIDER_VERIFIED",
                        **prepare(args.directory, args.cli_runtime_root),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if not 0 <= args.port <= 65535:
            raise SetupFailure("INVALID_LOCAL_PORT", "arguments")
        launch = start_arguments(
            args.directory, args.port, operator_auth_session=args.operator_auth_session
        )
        os.chdir(ROOT)
        # Replace this process: no extra UI worker, shell, retry or background PID controller.
        os.execve(  # noqa: S606 - replace with fixed Python/production UI argv, no shell
            sys.executable, launch, _environment(home=Path.home())
        )
        return 0
    except (SetupFailure, HarnessError, OSError) as error:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "code": error.code if isinstance(error, SetupFailure) else type(error).__name__,
                    "stage": error.stage if isinstance(error, SetupFailure) else "native-setup",
                },
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
