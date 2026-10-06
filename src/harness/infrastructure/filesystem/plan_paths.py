"""Plan入力と出力先を、初期化による書込みより先に検証する。"""

import os
import stat
from pathlib import Path

from harness.domain.errors import ErrorCode, HarnessError
from harness.infrastructure.filesystem.workspace_boundary import (
    FilesystemPolicy,
    verify_workspace_filesystem,
)


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise HarnessError(ErrorCode.SYMLINK_DENIED, "plan storage path is a symlink")


def validate_plan_paths(
    *,
    workspace: Path,
    artifact_root: Path,
    database: Path,
    policy: FilesystemPolicy,
    allow_test_filesystems: bool = False,
) -> None:
    verify_workspace_filesystem(workspace, policy, allow_test_filesystems=allow_test_filesystems)
    for target, is_directory in ((artifact_root, True), (database, False)):
        absolute = Path(os.path.abspath(target))
        # Missing末尾を辿る前にも字句拒否を適用する。
        if any(
            str(absolute) == prefix or str(absolute).startswith(prefix.rstrip("/") + "/")
            for prefix in policy.denied_mount_point_prefixes
        ):
            raise HarnessError(
                ErrorCode.WORKSPACE_ON_FOREIGN_FS_DENIED, "plan storage is on a denied path"
            )
        _reject_symlink(absolute)
        if absolute.exists():
            mode = absolute.stat(follow_symlinks=False).st_mode
            if (is_directory and not stat.S_ISDIR(mode)) or (
                not is_directory and not stat.S_ISREG(mode)
            ):
                raise HarnessError(ErrorCode.SPECIAL_FILE_DENIED, "invalid plan storage file type")
        parent = absolute if is_directory and absolute.exists() else absolute.parent
        while not parent.exists():
            _reject_symlink(parent)
            parent = parent.parent
        verify_workspace_filesystem(parent, policy, allow_test_filesystems=allow_test_filesystems)
        if not is_directory:
            for suffix in ("-wal", "-shm", "-journal"):
                sidecar = Path(str(absolute) + suffix)
                _reject_symlink(sidecar)
                if sidecar.exists() and not stat.S_ISREG(
                    sidecar.stat(follow_symlinks=False).st_mode
                ):
                    raise HarnessError(ErrorCode.SPECIAL_FILE_DENIED, "invalid database sidecar")
