"""Artifact Bytes の耐久書込み（§1.14、不変条件#13）。

    Bytesを同一FilesystemのTempへwrite
      → File fsync
      → Atomic Rename／Replace
      → Parent Directory fsync
      → SQLiteへArtifactManifest登録

この順序は「途中で電源が落ちても、Manifestが指すBytesが必ず存在する」ことを
目的とする。順序を1つでも入れ替えると次が起きる。

* Directory fsync を省く : Rename自体が失われ、Manifestが指す先が消える
* File fsync を省く      : Renameは残るが中身が空になる
* Manifest先行登録       : Manifestが存在するのにBytesが無い状態が正常系で発生する

Temp を `/tmp` へ置かないのも同じ理由である。Filesystemを跨ぐRenameはAtomicでなく、
実体はコピー＋削除になる。
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from harness.domain.artifact import (
    ArtifactMetadata,
    ArtifactVerification,
    VerificationOutcome,
    cas_object_segments,
    cas_temp_segments,
)
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash, hash_bytes

__all__ = ["FilesystemArtifactCas"]


class FilesystemArtifactCas:
    """CAS RootをまたがないContent-addressed Store。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    # ------------------------------------------------------------------
    # Path
    # ------------------------------------------------------------------

    def object_path(self, content_hash: ContentHash) -> Path:
        return self._root.joinpath(*cas_object_segments(content_hash))

    def relative_object_path(self, content_hash: ContentHash) -> str:
        return "/".join(cas_object_segments(content_hash))

    # ------------------------------------------------------------------
    # write
    # ------------------------------------------------------------------

    def write_bytes(self, data: bytes, metadata: ArtifactMetadata) -> ContentHash:
        """§1.14の順序でBytesを永続化し、Content Hashを返す。

        同一Hashの既存Bytesがある場合は再Hashして照合する。
        不一致なら`ARTIFACT_CONTENT_CONFLICT`で停止する。
        """
        if metadata.size_bytes != len(data):
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT,
                f"metadata.size_bytes={metadata.size_bytes} does not match "
                f"actual {len(data)} bytes",
            )

        content_hash = hash_bytes(data)
        final_path = self.object_path(content_hash)

        if final_path.exists():
            self._reject_on_conflict(final_path, content_hash)
            return content_hash

        temp_path = self._root.joinpath(
            *cas_temp_segments(content_hash, attempt_token=secrets.token_hex(8))
        )
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # 1. Temp write + 2. File fsync
            # rename前にHashを読み戻すため O_RDWR で開く。O_WRONLY だと
            # 検証のreadが EBADF で落ちる。
            descriptor = os.open(temp_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                _write_all(descriptor, data)
                os.fsync(descriptor)
                # rename直前に実体を確認する。ループの戻り値だけでは、
                # 書込みが黙って部分的に失われる経路（quota、ENOSPC後の
                # 部分書込み）を検出できない。最後に実Fileを見る。
                _verify_before_rename(descriptor, data, content_hash)
            finally:
                os.close(descriptor)

            # 既存を上書きしない。競合Putが先に確定していれば内容を照合する。
            if final_path.exists():
                self._reject_on_conflict(final_path, content_hash)
                return content_hash

            # 3. Atomic Rename（同一Filesystem内）
            os.replace(temp_path, final_path)

            # 4. Parent Directory fsync。これが無いとRename自体が失われ得る。
            self._fsync_directory(final_path.parent)
        finally:
            # 失敗時にTempを残さない。残すと孤児Bytesと区別できなくなる。
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

        return content_hash

    def _reject_on_conflict(self, path: Path, expected: ContentHash) -> None:
        observed = hash_bytes(path.read_bytes())
        if observed != expected:
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT,
                f"existing object at {self.relative_object_path(expected)} hashes to "
                f"{observed}, expected {expected}",
            )

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    # ------------------------------------------------------------------
    # read / verify
    # ------------------------------------------------------------------

    def read_bytes(self, content_hash: ContentHash) -> bytes:
        path = self.object_path(content_hash)
        if not path.is_file():
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT,
                f"artifact bytes missing for {content_hash}",
            )
        data = path.read_bytes()
        observed = hash_bytes(data)
        if observed != content_hash:
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT,
                f"stored bytes hash to {observed}, expected {content_hash}",
            )
        return data

    def verify(self, content_hash: ContentHash, *, manifest_exists: bool) -> ArtifactVerification:
        """§1.14 のRepair方針に対応する判定を返す。

        例外を投げず判定だけを返す。停止するかは呼出側が決める。
        """
        path = self.object_path(content_hash)
        if not path.is_file():
            outcome = (
                VerificationOutcome.BYTES_MISSING if manifest_exists else VerificationOutcome.OK
            )
            return ArtifactVerification(outcome=outcome, content_hash=content_hash)

        observed = hash_bytes(path.read_bytes())
        if observed != content_hash:
            return ArtifactVerification(
                outcome=VerificationOutcome.CONTENT_MISMATCH,
                content_hash=content_hash,
                observed_hash=observed,
            )
        if not manifest_exists:
            # Bytesだけがある。§1.14「孤児としてGC候補」
            return ArtifactVerification(
                outcome=VerificationOutcome.ORPHAN_BYTES,
                content_hash=content_hash,
                observed_hash=observed,
            )
        return ArtifactVerification(
            outcome=VerificationOutcome.OK,
            content_hash=content_hash,
            observed_hash=observed,
        )

    def delete_payload(self, content_hash: ContentHash) -> bool:
        """GC用。Bytesだけを消す。Manifestは呼出側が保持する。"""
        path = self.object_path(content_hash)
        if not path.is_file():
            return False
        path.unlink()
        self._fsync_directory(path.parent)
        return True


# ---------------------------------------------------------------------------
# 短縮write対策
# ---------------------------------------------------------------------------


def _write_all(descriptor: int, data: bytes) -> None:
    """全量書けるまで繰り返す。

    POSIXの`write(2)`は**要求より少ないBytesを書いて正常終了してよい**。
    Pipe・Socket・一部Filesystem・Signal割込みで実際に起きる。
    戻り値を捨てると「書けた」と誤認したまま fsync → rename まで進み、
    Content Hashが指すPathへ中身の欠けたFileを置くことになる。

    書けた分だけ`memoryview`を進める。同じ先頭を書き直すと内容が壊れる。
    1Byteも進まなくなったら諦める。無限ループにしない。
    """
    view = memoryview(data)
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, view[offset:])
        if written <= 0:
            raise HarnessError(
                ErrorCode.STORAGE_WRITE_FAILED,
                f"write stalled after {offset} of {len(data)} bytes",
            )
        offset += written


def _read_all(descriptor: int, size: int) -> bytes:
    """短縮readにも耐える。`read(2)`も要求より少なく返してよい。"""
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _verify_before_rename(
    descriptor: int, expected_data: bytes, expected_hash: ContentHash
) -> None:
    """rename手前でSizeとHashを再検証する。

    `_write_all` を信じ切らないための2段目である。ここで止めれば、
    Content Hashが指すPathへ中身の欠けたFileが置かれることはない。
    """
    size = os.fstat(descriptor).st_size
    if size != len(expected_data):
        raise HarnessError(
            ErrorCode.STORAGE_WRITE_FAILED,
            f"temp object size {size} does not match {len(expected_data)} bytes before rename",
        )
    os.lseek(descriptor, 0, os.SEEK_SET)
    observed = hash_bytes(_read_all(descriptor, size))
    if observed != expected_hash:
        # 値そのものは載せない（不変条件#7）。Hashだけで十分に特定できる。
        raise HarnessError(
            ErrorCode.ARTIFACT_CONTENT_CONFLICT,
            f"temp object hashes to {observed} but {expected_hash} was expected before rename",
        )
