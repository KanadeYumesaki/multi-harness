"""Native Linux worktree boundary for the offline workflow."""

from __future__ import annotations

import os
import stat
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash, hash_bytes, hash_canonical
from harness.domain.input_read import CapabilityScope, ReadDenial, validate_relative_path
from harness.infrastructure.filesystem.safe_reader import CapabilityBroker, SafeInputReader
from harness.infrastructure.filesystem.workspace_boundary import (
    FilesystemPolicy,
    open_and_verify_workspace,
    read_fd_mount_id,
)
from harness.infrastructure.filesystem.workspace_writer import (
    PreparedWorkspaceWrite,
    WorkspaceWriter,
)
from harness.ports.storage_commit import StorageCommitGuardPort


class LocalWorktree:
    def __init__(self, root: Path, policy: FilesystemPolicy) -> None:
        self.root = Path(os.path.abspath(root))
        self._policy = policy
        self._root_fd, self._attestation = open_and_verify_workspace(self.root, policy)
        self._identity = os.fstat(self._root_fd)
        try:
            info = (self.root / ".git").lstat()
            if not stat.S_ISREG(info.st_mode):
                raise HarnessError(
                    ErrorCode.PATH_OUTSIDE_CAPABILITY, "an isolated Git worktree is required"
                )
            actual = self._git(["rev-parse", "--show-toplevel"]).decode().strip()
            if actual != str(self.root):
                raise HarnessError(
                    ErrorCode.PATH_OUTSIDE_CAPABILITY, "worktree root does not match"
                )
        except BaseException:
            os.close(self._root_fd)
            raise

    def close(self) -> None:
        os.close(self._root_fd)

    def _git(self, args: list[str]) -> bytes:
        if args not in (["rev-parse", "HEAD"], ["rev-parse", "--show-toplevel"]):
            raise ValueError("Git inspection operation is not allowed")
        result = subprocess.run(  # noqa: S603 - fixed executable and allowlisted read-only argv
            [
                "/usr/bin/git",
                "--no-optional-locks",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            cwd=self.root,
            env={"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
            capture_output=True,
            timeout=15,
            check=False,
        )
        if result.returncode:
            raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "Git worktree inspection failed")
        return result.stdout

    def identity(self) -> str:
        current = self.root.lstat()
        if (current.st_dev, current.st_ino) != (self._identity.st_dev, self._identity.st_ino):
            raise HarnessError(ErrorCode.PATH_OUTSIDE_CAPABILITY, "worktree root was replaced")
        return str(
            hash_canonical(
                {
                    "path": str(self.root),
                    "dev": current.st_dev,
                    "ino": current.st_ino,
                    "mount_id": self._attestation.mount_id,
                    "head": self._git(["rev-parse", "HEAD"]).decode().strip(),
                },
                artifact_type="local-worktree-identity",
                schema_major=1,
            )
        )

    def snapshot(self) -> dict[str, str]:
        self.identity()
        paths: list[str] = []
        entries_seen = 0
        deadline = time.monotonic() + 15.0

        def walk(fd: int, prefix: str, depth: int) -> None:
            nonlocal entries_seen
            if depth > 32 or time.monotonic() > deadline:
                raise HarnessError(
                    ErrorCode.PATH_OUTSIDE_CAPABILITY, "worktree depth exceeds bound"
                )
            before = os.fstat(fd)
            if read_fd_mount_id(fd) != self._attestation.mount_id:
                raise HarnessError(ErrorCode.MOUNT_CROSSING_DENIED, "snapshot mount changed")
            names: list[str] = []
            with os.scandir(fd) as entries:
                for entry in entries:
                    entries_seen += 1
                    if entries_seen > 4096 or time.monotonic() > deadline:
                        raise HarnessError(
                            ErrorCode.PATH_OUTSIDE_CAPABILITY,
                            "snapshot enumeration exceeded bounds",
                        )
                    names.append(entry.name)
            for name in sorted(names):
                if not prefix and name == ".git":
                    continue
                path = prefix + name
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise HarnessError(ErrorCode.SYMLINK_DENIED, "worktree contains symlink")
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        if os.fstat(child).st_dev != self._identity.st_dev:
                            raise HarnessError(
                                ErrorCode.MOUNT_CROSSING_DENIED, "worktree mount changed"
                            )
                        walk(child, path + "/", depth + 1)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    paths.append(path)
                    if len(paths) > 4096:
                        raise HarnessError(
                            ErrorCode.PATH_OUTSIDE_CAPABILITY, "worktree file count exceeds bound"
                        )
                else:
                    raise HarnessError(
                        ErrorCode.SPECIAL_FILE_DENIED, "worktree contains special file or hardlink"
                    )

            after = os.fstat(fd)
            if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise HarnessError(ErrorCode.PATH_OUTSIDE_CAPABILITY, "snapshot directory changed")

        walk(self._root_fd, "", 0)
        result: dict[str, str] = {}
        total = 0
        for path in sorted(paths):
            if time.monotonic() > deadline:
                raise HarnessError(
                    ErrorCode.PATH_OUTSIDE_CAPABILITY, "snapshot read exceeded time bound"
                )
            payload = self.read(path)
            total += len(payload)
            if total > 64 * 1024 * 1024:
                raise HarnessError(
                    ErrorCode.PATH_OUTSIDE_CAPABILITY, "worktree byte count exceeds bound"
                )
            result[path] = str(hash_bytes(payload))
        self.identity()
        return result

    def read(self, relative_path: str) -> bytes:
        parts = validate_relative_path(relative_path)
        if parts is None or parts[0] == ".git":
            raise HarnessError(ErrorCode.PATH_OUTSIDE_CAPABILITY, "invalid worktree input path")
        self.identity()
        with CapabilityBroker(self._policy) as broker:
            broker.issue("workflow-read", self.root, CapabilityScope((relative_path,)))
            result = SafeInputReader(broker).open_read("workflow-read", relative_path)
            if isinstance(result, ReadDenial):
                raise HarnessError(result.error_code, "worktree input read denied")
            return result[0]

    def prepare(
        self,
        *,
        relative_path: str,
        effect_id: str,
        before_hash: ContentHash,
        replacement: bytes,
        guard: StorageCommitGuardPort,
    ) -> dict[str, Any]:
        self.identity()
        writer = WorkspaceWriter(self._root_fd, commit_guard=guard)
        try:
            prepared = writer.prepare(
                relative_path=relative_path,
                effect_id=effect_id,
                expected_before_hash=before_hash,
                replacement=replacement,
            )
            result = asdict(prepared)
            result["expected_before_hash"] = str(prepared.expected_before_hash)
            result["expected_after_hash"] = str(prepared.expected_after_hash)
            return result
        finally:
            writer.close()

    def commit(self, prepared: dict[str, Any], guard: StorageCommitGuardPort) -> ContentHash:
        self.identity()
        fields = dict(prepared)
        expected_snapshot = dict(fields.pop("workspace_snapshot"))
        parent = fields["relative_path"].rsplit("/", 1)
        temporary_path = (parent[0] + "/" if len(parent) == 2 else "") + fields["temp_name"]
        expected_snapshot[temporary_path] = fields["expected_after_hash"]
        if self.snapshot() != expected_snapshot:
            raise HarnessError(
                ErrorCode.APPROVAL_INVALIDATED, "worktree changed before final storage commit"
            )
        fields["expected_before_hash"] = ContentHash.parse(fields["expected_before_hash"])
        fields["expected_after_hash"] = ContentHash.parse(fields["expected_after_hash"])
        writer = WorkspaceWriter(self._root_fd, commit_guard=guard)
        try:
            return writer.commit(PreparedWorkspaceWrite(**fields))
        finally:
            writer.close()
