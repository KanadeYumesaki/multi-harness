"""Kernel liveness using flock on the existing database parent directory.

Active operations take shared locks. Reconciliation takes one exclusive fence,
which cannot succeed while any operation in that directory is alive. No lock
file, network module, or database descriptor is created. Opening/closing the DB
itself would release SQLite's process-owned POSIX locks, even when an independent
OFD lock was used, so it must never be used as our lock carrier.

Fork descendants retain the shared open-file description. A different boot or
DB/directory identity is UNKNOWN. Other DBs in the same directory conservatively
block reconciliation too; use a dedicated state directory for each database.
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

from harness.domain.errors import ErrorCode, HarnessError
from harness.ports.operation_control import OwnerClaim


class LinuxOperationOwner:
    def __init__(self, database: Path) -> None:
        self.database = database

    def hold(
        self, token: str, expected_scope: str | None = None
    ) -> AbstractContextManager[OwnerClaim]:
        """Shared live claim, or an exclusive probe when checking a recorded scope.

        Tokens remain unique DB reservation identities. The kernel fence deliberately
        covers *all* holders in the directory, not an inferred per-token process count.
        """
        return self._claim(exclusive=expected_scope is not None, expected_scope=expected_scope)

    def fence(self) -> AbstractContextManager[OwnerClaim]:
        """Hold one exclusive fence for a complete review/transaction."""
        return self._claim(exclusive=True, expected_scope=None)

    @contextmanager
    def _claim(self, *, exclusive: bool, expected_scope: str | None) -> Iterator[OwnerClaim]:
        descriptor = None
        try:
            descriptor = os.open(
                self.database.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
            directory = os.fstat(descriptor)
            database = os.stat(self.database.name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISREG(database.st_mode):
                raise OSError(errno.EINVAL, "operation owner requires a regular database")
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            scope = (
                f"linux-flock-owner/1:{boot}:{directory.st_dev}:{directory.st_ino}:"
                f"{database.st_dev}:{database.st_ino}"
            )
            if expected_scope is not None and expected_scope != scope:
                claim = OwnerClaim("UNKNOWN", scope)
            else:
                try:
                    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                    fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
                    claim = OwnerClaim("ACQUIRED", scope)
                except OSError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EACCES):
                        raise
                    claim = OwnerClaim("ACTIVE", scope)
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise HarnessError(
                ErrorCode.STORAGE_WRITE_FAILED, "operation owner lock unavailable"
            ) from exc
        try:
            yield claim
        finally:
            if descriptor is not None:
                os.close(descriptor)
