"""Final storage authorization; the context keeps the fence stable through replace."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol

from harness.domain.hashing import ContentHash


class StorageCommitGuardPort(Protocol):
    def authorize(
        self,
        *,
        effect_id: str,
        relative_path: str,
        before_hash: ContentHash,
        after_hash: ContentHash,
    ) -> AbstractContextManager[None]: ...
