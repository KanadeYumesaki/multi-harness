#!/usr/bin/env python3
"""Create a new synthetic Git worktree fixture. Never overwrite an existing destination."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from harness.infrastructure.filesystem.workspace_boundary import (  # noqa: E402
    FilesystemPolicy,
    open_and_verify_workspace,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    destination = Path(os.path.abspath(args.directory))
    if destination.exists() or destination.is_symlink():
        parser.error("destination already exists; use a new directory")
    fd, _ = open_and_verify_workspace(destination.parent, FilesystemPolicy.load(ROOT))
    os.close(fd)
    destination.mkdir(mode=0o700)
    seed = destination / "seed"
    seed.mkdir()
    docs = seed / "docs"
    (docs / "refs").mkdir(parents=True)
    (docs / "task.md").write_bytes((ROOT / "examples/offline-workflow/task.md").read_bytes())
    (docs / "out.md").write_text("Synthetic initial file.\n", encoding="utf-8")
    (docs / "refs/facts.txt").write_text("The demo uses a blue greeting.\n" * 200, encoding="utf-8")
    declaration = json.loads(
        (ROOT / "examples/offline-workflow/declaration.template.json").read_text()
    )
    declaration["runtime_envelope"]["executable_path"] = str(Path(sys.executable).resolve())
    declaration["runtime_envelope"]["executable_sha256"] = (
        "sha256:" + hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
    )
    now = datetime.now(UTC)
    declaration["token_profile"].update(
        retrieved_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=(now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    (docs / "declaration.json").write_text(
        json.dumps(declaration, ensure_ascii=False, indent=2) + "\n"
    )
    for operation in (
        ["init", "-b", "codex/offline-demo"],
        ["add", "docs"],
        ["commit", "-m", "chore: create synthetic offline demo"],
        ["worktree", "add", "--detach", str(destination / "worktree")],
    ):
        subprocess.run(  # noqa: S603 - fixed Git argv on a new synthetic fixture
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
            cwd=seed,
            env={"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
            capture_output=True,
            timeout=15,
            check=True,
        )
    print(
        json.dumps(
            {
                "directory": str(destination),
                "workspace": str(destination / "worktree"),
                "database": str(destination / "harness.db"),
                "artifact_root": str(destination / "artifacts"),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
