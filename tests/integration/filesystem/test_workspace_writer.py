from __future__ import annotations

import os
from contextlib import nullcontext
from pathlib import Path

import pytest

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_bytes
from harness.infrastructure.filesystem import workspace_writer
from harness.infrastructure.filesystem.workspace_writer import WorkspaceWriter

pytestmark = pytest.mark.integration


def test_workspace_writer_commits_via_same_directory_temp_and_observed_hash(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_bytes(b"before\n")
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        writer = WorkspaceWriter(root_fd, commit_guard=_FilesystemTestGuard())
        prepared = writer.prepare(
            relative_path="notes.txt",
            effect_id="effect-1",
            expected_before_hash=hash_bytes(b"before\n"),
            replacement=b"after\n",
        )
        assert target.read_bytes() == b"before\n"
        observed = writer.commit(prepared)
        assert observed == hash_bytes(b"after\n")
        assert target.read_bytes() == b"after\n"
    finally:
        os.close(root_fd)


def test_workspace_writer_rejects_same_device_mount_id_crossing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一st_devのbind mountでもfdinfo上のmnt_id差異なら拒否する。"""
    nested = tmp_path / "mounted"
    nested.mkdir()
    (nested / "notes.txt").write_bytes(b"before\n")
    mount_ids = iter((101, 202))
    monkeypatch.setattr(workspace_writer, "read_fd_mount_id", lambda _fd: next(mount_ids))
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        writer = WorkspaceWriter(root_fd, commit_guard=_FilesystemTestGuard())
        with pytest.raises(HarnessError) as crossing:
            writer.prepare(
                relative_path="mounted/notes.txt",
                effect_id="effect-mount",
                expected_before_hash=hash_bytes(b"before\n"),
                replacement=b"after\n",
            )
        assert crossing.value.code is ErrorCode.MOUNT_CROSSING_DENIED
    finally:
        os.close(root_fd)


def test_workspace_writer_rejects_escape_symlink_and_base_hash_mismatch(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_bytes(b"before\n")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside\n")
    (tmp_path / "link.txt").symlink_to(outside)
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        writer = WorkspaceWriter(root_fd, commit_guard=_FilesystemTestGuard())
        with pytest.raises(HarnessError) as escape:
            writer.prepare(
                relative_path="../outside.txt",
                effect_id="effect-1",
                expected_before_hash=hash_bytes(b"outside\n"),
                replacement=b"changed\n",
            )
        assert escape.value.code is ErrorCode.PATH_OUTSIDE_CAPABILITY

        with pytest.raises(HarnessError) as symlink:
            writer.prepare(
                relative_path="link.txt",
                effect_id="effect-2",
                expected_before_hash=hash_bytes(b"outside\n"),
                replacement=b"changed\n",
            )
        assert symlink.value.code is ErrorCode.SYMLINK_DENIED

        with pytest.raises(HarnessError) as mismatch:
            writer.prepare(
                relative_path="notes.txt",
                effect_id="effect-3",
                expected_before_hash=hash_bytes(b"wrong\n"),
                replacement=b"changed\n",
            )
        assert mismatch.value.code is ErrorCode.ARTIFACT_CONTENT_CONFLICT
    finally:
        os.close(root_fd)


class _FilesystemTestGuard:
    """Filesystem-only tests; production SQL fencing is tested in workflow integration."""

    def authorize(self, **kwargs):
        return nullcontext()
