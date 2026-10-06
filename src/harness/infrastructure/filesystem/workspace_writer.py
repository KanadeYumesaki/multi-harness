"""MVP0-A Local File CommitのFilesystem側実装。

Operation JournalのDurable PrepareとLedger EventはApplication層が同一DB Transactionで
先に確定する。本ModuleはDirectory FDを基準にしたStorage操作だけを担当し、Shellや
Path.resolve()を使用しない。
"""

from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass
from typing import Final

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash, hash_bytes
from harness.infrastructure.filesystem.workspace_boundary import read_fd_mount_id
from harness.ports.storage_commit import StorageCommitGuardPort

__all__ = ["PreparedWorkspaceWrite", "WorkspaceWriter"]

_MAX_IO_CHUNK: Final[int] = 1024 * 1024


@dataclass(frozen=True, slots=True)
class PreparedWorkspaceWrite:
    relative_path: str
    effect_id: str
    temp_name: str
    expected_before_hash: ContentHash
    expected_after_hash: ContentHash
    parent_dev: int
    parent_ino: int
    parent_mount_id: int
    target_dev: int
    target_ino: int


class WorkspaceWriter:
    """Root Directory FDへ束縛された同一Filesystem書込み器。"""

    def __init__(self, root_fd: int, *, commit_guard: StorageCommitGuardPort) -> None:
        self._commit_guard = commit_guard
        self._root_fd = os.dup(root_fd)
        root_stat = os.fstat(self._root_fd)
        if not stat.S_ISDIR(root_stat.st_mode):
            os.close(self._root_fd)
            raise ValueError("root_fd must identify a directory")
        self._root_dev = root_stat.st_dev
        self._root_ino = root_stat.st_ino
        self._root_mount_id = read_fd_mount_id(self._root_fd)

    def close(self) -> None:
        os.close(self._root_fd)

    def prepare(
        self,
        *,
        relative_path: str,
        effect_id: str,
        expected_before_hash: ContentHash,
        replacement: bytes,
    ) -> PreparedWorkspaceWrite:
        """同一Parent DirectoryへTempを書込み、File fsyncまで完了する。"""
        parts = _path_parts(relative_path)
        if not effect_id:
            raise ValueError("effect_id must not be empty")
        parent_fd = self._open_parent(parts[:-1])
        try:
            parent_stat = os.fstat(parent_fd)
            parent_mount_id = read_fd_mount_id(parent_fd)
            if parent_mount_id != self._root_mount_id:
                raise HarnessError(
                    ErrorCode.MOUNT_CROSSING_DENIED, "parent is on a different mount"
                )
            target_fd, target_stat = _open_regular(parts[-1], parent_fd)
            try:
                observed = _read_hash(target_fd)
            finally:
                os.close(target_fd)
            if observed != expected_before_hash:
                raise HarnessError(
                    ErrorCode.ARTIFACT_CONTENT_CONFLICT, "target base hash changed before prepare"
                )
            expected_after_hash = hash_bytes(replacement)
            temp_name = _temp_name(effect_id)
            temp_fd = _create_temp(temp_name, parent_fd)
            try:
                _write_all(temp_fd, replacement)
                os.fsync(temp_fd)
            except BaseException:
                os.close(temp_fd)
                raise
            os.close(temp_fd)
            return PreparedWorkspaceWrite(
                relative_path=relative_path,
                effect_id=effect_id,
                temp_name=temp_name,
                expected_before_hash=expected_before_hash,
                expected_after_hash=expected_after_hash,
                parent_dev=parent_stat.st_dev,
                parent_ino=parent_stat.st_ino,
                parent_mount_id=parent_mount_id,
                target_dev=target_stat.st_dev,
                target_ino=target_stat.st_ino,
            )
        finally:
            os.close(parent_fd)

    def commit(self, prepared: PreparedWorkspaceWrite) -> ContentHash:
        """IdentityとBase Hashを再確認後にAtomic Replaceし、再読込みHashを返す。"""
        parts = _path_parts(prepared.relative_path)
        root_stat = os.fstat(self._root_fd)
        if (root_stat.st_dev, root_stat.st_ino) != (self._root_dev, self._root_ino):
            raise HarnessError(ErrorCode.PATH_OUTSIDE_CAPABILITY, "workspace root identity changed")
        parent_fd = self._open_parent(parts[:-1])
        try:
            parent_stat = os.fstat(parent_fd)
            parent_mount_id = read_fd_mount_id(parent_fd)
            if (
                parent_mount_id != self._root_mount_id
                or parent_mount_id != prepared.parent_mount_id
            ):
                raise HarnessError(
                    ErrorCode.MOUNT_CROSSING_DENIED, "target parent mount changed before commit"
                )
            if (parent_stat.st_dev, parent_stat.st_ino) != (
                prepared.parent_dev,
                prepared.parent_ino,
            ):
                raise HarnessError(
                    ErrorCode.ARTIFACT_CONTENT_CONFLICT,
                    "target parent identity changed before commit",
                )
            target_fd, target_stat = _open_regular(parts[-1], parent_fd)
            try:
                before = _read_hash(target_fd)
            finally:
                os.close(target_fd)
            if (target_stat.st_dev, target_stat.st_ino) != (
                prepared.target_dev,
                prepared.target_ino,
            ):
                raise HarnessError(
                    ErrorCode.ARTIFACT_CONTENT_CONFLICT, "target identity changed before commit"
                )
            if before != prepared.expected_before_hash:
                raise HarnessError(
                    ErrorCode.ARTIFACT_CONTENT_CONFLICT, "target base hash changed before commit"
                )
            temp_fd, temp_stat = _open_regular(prepared.temp_name, parent_fd)
            try:
                if temp_stat.st_nlink != 1 or _read_hash(temp_fd) != prepared.expected_after_hash:
                    raise HarnessError(ErrorCode.ARTIFACT_CONTENT_CONFLICT, "prepared temp changed")
            finally:
                os.close(temp_fd)
            with self._commit_guard.authorize(
                effect_id=prepared.effect_id,
                relative_path=prepared.relative_path,
                before_hash=prepared.expected_before_hash,
                after_hash=prepared.expected_after_hash,
            ):
                return self._replace_and_observe(prepared, parent_fd, parts[-1])
        finally:
            os.close(parent_fd)

    @staticmethod
    def _replace_and_observe(
        prepared: PreparedWorkspaceWrite, parent_fd: int, name: str
    ) -> ContentHash:
        try:
            os.replace(prepared.temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError as exc:
            raise HarnessError(
                ErrorCode.EFFECT_UNKNOWN, f"atomic replace failed: {exc.strerror}"
            ) from None
        observed_fd, _ = _open_regular(name, parent_fd)
        try:
            observed = _read_hash(observed_fd)
        finally:
            os.close(observed_fd)
        if observed != prepared.expected_after_hash:
            raise HarnessError(
                ErrorCode.EFFECT_UNKNOWN, "observed target hash differs after atomic replace"
            )
        return observed

    def _open_parent(self, parts: tuple[str, ...]) -> int:
        fd = os.dup(self._root_fd)
        try:
            for part in parts:
                try:
                    next_fd = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=fd,
                    )
                except OSError as exc:
                    if _is_symlink(fd, part):
                        raise HarnessError(
                            ErrorCode.SYMLINK_DENIED, "path component is a symlink"
                        ) from None
                    if exc.errno == errno.EXDEV:
                        raise HarnessError(
                            ErrorCode.MOUNT_CROSSING_DENIED, "mount crossing is denied"
                        ) from None
                    raise HarnessError(
                        ErrorCode.PATH_OUTSIDE_CAPABILITY, "cannot open parent directory"
                    ) from None
                os.close(fd)
                fd = next_fd
                if read_fd_mount_id(fd) != self._root_mount_id:
                    raise HarnessError(ErrorCode.MOUNT_CROSSING_DENIED, "parent walk crossed mount")
            parent_stat = os.fstat(fd)
            if parent_stat.st_dev != self._root_dev or read_fd_mount_id(fd) != self._root_mount_id:
                raise HarnessError(ErrorCode.MOUNT_CROSSING_DENIED, "parent is on another mount")
            return fd
        except BaseException:
            os.close(fd)
            raise


def _path_parts(relative_path: str) -> tuple[str, ...]:
    if not relative_path or relative_path.startswith("/") or "\x00" in relative_path:
        raise HarnessError(
            ErrorCode.PATH_OUTSIDE_CAPABILITY, "path must be a non-empty relative path"
        )
    parts = tuple(relative_path.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise HarnessError(ErrorCode.PATH_OUTSIDE_CAPABILITY, "path contains an unsafe component")
    if any(any(ord(char) < 32 for char in part) for part in parts):
        raise HarnessError(ErrorCode.PATH_OUTSIDE_CAPABILITY, "path contains a control character")
    return parts


def _open_regular(name: str, parent_fd: int) -> tuple[int, os.stat_result]:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    except OSError as exc:
        if _is_symlink(parent_fd, name):
            raise HarnessError(ErrorCode.SYMLINK_DENIED, "target is a symlink") from None
        raise HarnessError(
            ErrorCode.PATH_OUTSIDE_CAPABILITY, "target is not an openable regular file"
        ) from exc
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise HarnessError(ErrorCode.PATH_OUTSIDE_CAPABILITY, "target is not a regular file")
    return fd, info


def _is_symlink(parent_fd: int, name: str) -> bool:
    try:
        return stat.S_ISLNK(os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _temp_name(effect_id: str) -> str:
    return f".harness-{hash_bytes(effect_id.encode('utf-8')).hexdigest[:32]}.tmp"


def _create_temp(name: str, parent_fd: int) -> int:
    try:
        return os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
    except FileExistsError as exc:
        raise HarnessError(
            ErrorCode.ARTIFACT_CONTENT_CONFLICT, "prepared temp object already exists"
        ) from exc
    except OSError as exc:
        raise HarnessError(
            ErrorCode.EFFECT_UNKNOWN, f"cannot create prepared temp object: {exc.strerror}"
        ) from None


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            count = os.write(fd, view[:_MAX_IO_CHUNK])
        except OSError as exc:
            raise HarnessError(
                ErrorCode.EFFECT_UNKNOWN, f"temp write failed: {exc.strerror}"
            ) from None
        if count <= 0:
            raise HarnessError(ErrorCode.EFFECT_UNKNOWN, "temp write made no progress")
        view = view[count:]


def _read_hash(fd: int) -> ContentHash:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, _MAX_IO_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > 16 * 1024 * 1024:
            raise HarnessError(ErrorCode.PATH_OUTSIDE_CAPABILITY, "file exceeds write read limit")
        chunks.append(chunk)
    return hash_bytes(b"".join(chunks))
