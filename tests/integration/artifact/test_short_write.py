"""短縮writeに対する耐性（§1.14、不変条件#13）。

## 何が問題だったか

    os.write(descriptor, data)   # 戻り値を捨てている

POSIXの`write(2)`は**要求より少ないBytesを書いて正常終了してよい**。
Pipe・Socket・一部Filesystem・Signal割込みで実際に起きる。戻り値を見なければ
「書けた」と誤認したまま fsync → rename まで進み、**Content Hashが指すPathへ
中身の欠けたFileを置く**。

Content-addressedの前提が壊れる。Pathを信じて読んだ側は、Hashが一致すると
思っている内容と違うBytesを受け取る。しかも短縮は平常時にはまず起きないため、
試験を書かなければ何年も潜伏する。

## 対策は2重にする

1. write-allループ。書けた分だけ進めて、全量書くまで繰り返す
2. rename直前にSizeとHashを再検証する

1だけでも足りるが、2を置くのはループの実装を信じ切らないためである。
Filesystemの都合で書込みが黙って失敗する経路（quota、ENOSPC後の部分書込み）
まで含めて、renameの手前で最後に実体を確認する。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from harness.domain.artifact import ArtifactMetadata
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_bytes
from harness.infrastructure.artifact.filesystem_cas import FilesystemArtifactCas

pytestmark = pytest.mark.integration

PAYLOAD = b"0123456789" * 64  # 640 bytes


def metadata() -> ArtifactMetadata:
    return ArtifactMetadata(
        media_type="text/plain",
        size_bytes=len(PAYLOAD),
        data_classification="INTERNAL",
        trust_level="VERIFIED_INTERNAL",
    )


@pytest.fixture
def cas(tmp_path: Path) -> FilesystemArtifactCas:
    return FilesystemArtifactCas(tmp_path / "cas")


def install_short_write(
    monkeypatch: pytest.MonkeyPatch, *, chunk: int, fail_after: int | None = None
) -> list[int]:
    """`os.write` を短縮writeへ差し替える。

    `fail_after` 回書いたあとは0Byteを返し続ける（それ以上進めない状況）。
    """
    real_write = os.write
    calls: list[int] = []

    def short_write(fd: int, payload: Any) -> int:
        if fail_after is not None and len(calls) >= fail_after:
            calls.append(0)
            return 0
        written = real_write(fd, payload[:chunk])
        calls.append(written)
        return written

    monkeypatch.setattr(os, "write", short_write)
    return calls


# ---------------------------------------------------------------------------
# 短縮writeでも全量書く
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("chunk", [1, 7, 64, 639])
def test_short_write_still_stores_the_whole_payload(
    cas: FilesystemArtifactCas, monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    """1回のwriteが要求より少なく書いても、最終的に全量が入る。"""
    calls = install_short_write(monkeypatch, chunk=chunk)
    content_hash = cas.write_bytes(PAYLOAD, metadata())

    assert len(calls) > 1, "短縮writeが注入できていない"
    stored = cas.object_path(content_hash).read_bytes()
    assert stored == PAYLOAD
    assert hash_bytes(stored) == content_hash


def test_write_all_loop_advances_the_buffer(
    cas: FilesystemArtifactCas, monkeypatch: pytest.MonkeyPatch
) -> None:
    """書けた分だけ進める。同じ先頭を書き直すと内容が壊れる。"""
    install_short_write(monkeypatch, chunk=100)
    content_hash = cas.write_bytes(PAYLOAD, metadata())
    assert cas.object_path(content_hash).read_bytes() == PAYLOAD


# ---------------------------------------------------------------------------
# 書き切れない場合は置かない
# ---------------------------------------------------------------------------


def test_stalled_write_does_not_publish_a_truncated_object(
    cas: FilesystemArtifactCas, monkeypatch: pytest.MonkeyPatch
) -> None:
    """途中で1Byteも進まなくなったら、**Objectを置かない**。

    ここで諦めて rename すると、Content Hashが指すPathに中身の欠けた
    Fileが置かれる。Content-addressedの前提が壊れる。
    """
    install_short_write(monkeypatch, chunk=32, fail_after=3)

    with pytest.raises(HarnessError) as error:
        cas.write_bytes(PAYLOAD, metadata())
    assert error.value.code is ErrorCode.STORAGE_WRITE_FAILED

    objects = cas.root / "objects"
    stored = [path for path in objects.rglob("*") if path.is_file()] if objects.exists() else []
    assert stored == [], "書き切れていないのにObjectが置かれた"


def test_no_temp_file_remains_after_a_stalled_write(
    cas: FilesystemArtifactCas, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失敗時にTempを残さない。残すと孤児Bytesと区別できない。"""
    install_short_write(monkeypatch, chunk=32, fail_after=3)
    with pytest.raises(HarnessError):
        cas.write_bytes(PAYLOAD, metadata())

    incoming = cas.root / "incoming"
    assert not incoming.exists() or not any(incoming.iterdir())


# ---------------------------------------------------------------------------
# rename直前の再検証
# ---------------------------------------------------------------------------


def test_size_mismatch_before_rename_is_detected(
    cas: FilesystemArtifactCas, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ループを信じ切らず、rename手前で実体を確認する。

    書込みが黙って部分的に失われる経路（quota、ENOSPC後の部分書込み）は
    ループの戻り値だけでは検出できない。最後に実Fileを見る。
    """
    real_fsync = os.fsync

    def truncate_then_fsync(fd: int) -> None:
        # fsyncの直前でFileを削り、書けたはずのBytesが減った状況を作る。
        os.ftruncate(fd, 10)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", truncate_then_fsync)

    with pytest.raises(HarnessError) as error:
        cas.write_bytes(PAYLOAD, metadata())
    assert error.value.code is ErrorCode.STORAGE_WRITE_FAILED
    assert "size" in str(error.value).lower()


def test_content_mismatch_before_rename_is_detected(
    cas: FilesystemArtifactCas, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sizeが合っていても中身が違えば止める。"""
    real_fsync = os.fsync

    def corrupt_then_fsync(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, b"X" * 10)
        os.lseek(fd, 0, os.SEEK_END)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", corrupt_then_fsync)

    with pytest.raises(HarnessError) as error:
        cas.write_bytes(PAYLOAD, metadata())
    assert error.value.code is ErrorCode.ARTIFACT_CONTENT_CONFLICT


# ---------------------------------------------------------------------------
# 正常系が壊れていないこと
# ---------------------------------------------------------------------------


def test_normal_write_is_unaffected(cas: FilesystemArtifactCas) -> None:
    content_hash = cas.write_bytes(PAYLOAD, metadata())
    assert cas.object_path(content_hash).read_bytes() == PAYLOAD
    assert cas.verify(content_hash, manifest_exists=True).outcome.name == "OK"


def test_empty_payload_is_written(cas: FilesystemArtifactCas, tmp_path: Path) -> None:
    """0Byteでもループが止まらないこと。`while remaining` の境界。"""
    empty_metadata = ArtifactMetadata(
        media_type="text/plain",
        size_bytes=0,
        data_classification="INTERNAL",
        trust_level="VERIFIED_INTERNAL",
    )
    content_hash = cas.write_bytes(b"", empty_metadata)
    assert cas.object_path(content_hash).read_bytes() == b""
