"""`ExecutableDigestPort` の Filesystem 実装。

宣言された実行体を**実際に読んで**SHA-256 を測る。測れなければ
`ExecutablePathMissing` で止める。Application 層はこの型を知らない。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from harness.domain.hashing import ContentHash
from harness.ports.executable_digest import ExecutablePathMissing

__all__ = ["FilesystemExecutableDigest"]

#: 一度に読む量。実行体は大きいことがあるので全部を Memory へ載せない。
_CHUNK_BYTES = 1024 * 1024


class FilesystemExecutableDigest:
    """絶対 Path の実行体を測る。"""

    def digest(self, absolute_path: str) -> ContentHash:
        path = Path(absolute_path)
        if not path.is_absolute():
            raise ExecutablePathMissing(f"executable path must be absolute: {absolute_path!r}")
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(_CHUNK_BYTES):
                    digest.update(chunk)
        except OSError as exc:
            # 読めなかったことを握り潰さない（不変条件#9）。
            raise ExecutablePathMissing(f"{absolute_path!r}: {exc.strerror}") from None
        return ContentHash.of_digest(digest.digest())
