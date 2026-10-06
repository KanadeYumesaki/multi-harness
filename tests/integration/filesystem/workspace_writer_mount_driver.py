"""WorkspaceWriterの実bind mount越境試験を隔離Mount名前空間内で実行する。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]) / "src"))

from harness.domain.errors import HarnessError
from harness.domain.hashing import hash_bytes
from harness.infrastructure.filesystem.workspace_writer import WorkspaceWriter

_BEFORE = b"before\n"
_AFTER = b"after\n"


def _mount(*arguments: str) -> None:
    executable = shutil.which("mount")
    if executable is None:
        raise RuntimeError("mount(8) が見つからない")
    result = subprocess.run(  # noqa: S603 - fixed argv, shell disabled
        [executable, *arguments], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"mount {arguments} failed: {result.stderr.strip()}")


def _prepare_and_commit(writer: WorkspaceWriter, path: str) -> bool:
    prepared = writer.prepare(
        relative_path=path,
        effect_id=f"effect-{path}",
        expected_before_hash=hash_bytes(_BEFORE),
        replacement=_AFTER,
    )
    return writer.commit(prepared) == hash_bytes(_AFTER)


def main() -> int:
    base = Path(sys.argv[2])
    workspace = base / "workspace"
    mounted = workspace / "mounted"
    outside = base / "outside"
    workspace.mkdir()
    mounted.mkdir()
    outside.mkdir()
    (workspace / "normal.txt").write_bytes(_BEFORE)
    (outside / "notes.txt").write_bytes(_BEFORE)

    _mount("--bind", str(outside), str(mounted))
    same_device = os.stat(mounted).st_dev == os.stat(workspace).st_dev
    root_fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    writer = WorkspaceWriter(root_fd, commit_guard=_FilesystemTestGuard())
    try:
        error_code: str | None = None
        try:
            _prepare_and_commit(writer, "mounted/notes.txt")
        except HarnessError as exc:
            error_code = exc.code.value
        normal_committed = _prepare_and_commit(writer, "normal.txt")
    finally:
        writer.close()
        os.close(root_fd)

    print(
        json.dumps(
            {
                "same_device": same_device,
                "crossing_error": error_code,
                "normal_committed": normal_committed,
                "outside_content": (outside / "notes.txt").read_bytes().decode("utf-8"),
                "normal_content": (workspace / "normal.txt").read_bytes().decode("utf-8"),
            }
        )
    )
    return 0


class _FilesystemTestGuard:
    """Filesystem-only tests; production SQL fencing is tested in workflow integration."""

    def authorize(self, **kwargs):
        return nullcontext()


if __name__ == "__main__":
    raise SystemExit(main())
