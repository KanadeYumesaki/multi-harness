"""Artifact CAS の統合試験（§1.14、不変条件#13）。

書込み順序そのものを検証する。順序の誤りは平常時には見えず、
電源断・Crash時にだけ「Manifestが指すBytesが無い」形で現れるため、
`os` 呼出の順序を捕捉して検査する。

## Case Adapter はここではない

`AT-GC-001/RAW_RETENTION` の `@pytest.mark.case` は
`tests/integration/artifact/test_gc_executor.py` へ移した。
Case は `ARTIFACT_PAYLOAD_DELETED` の Event 列まで要求するが、本 File は
Ledger を観測しない。Ledger を観測しない試験を Adapter にすると
「State だけ一致した Case」になる。

本 File の試験は消していない。Artifact Store の振る舞いは引き続き検証する。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from harness.domain.artifact import (
    ArtifactMetadata,
    ArtifactVerification,
    VerificationOutcome,
)
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash, hash_bytes
from harness.infrastructure.artifact.filesystem_cas import FilesystemArtifactCas
from harness.infrastructure.artifact.store import ArtifactStore
from harness.infrastructure.sqlite.artifact_manifest_repository import (
    SqliteArtifactManifestRepository,
)
from harness.infrastructure.sqlite.connection_factory import (
    ConnectionFactory,
    ConnectionRole,
)
from harness.infrastructure.sqlite.migrations import SCHEMA_VERSION, migrate
from harness.ports.artifact_store import ArtifactManifestRecord

pytestmark = pytest.mark.integration

STORED_AT = "2026-08-07T00:00:00Z"
PAYLOAD = b"contract text\n"


def _metadata(size: int = len(PAYLOAD)) -> ArtifactMetadata:
    return ArtifactMetadata(
        media_type="text/plain",
        size_bytes=size,
        data_classification="INTERNAL",
        trust_level="VERIFIED_INTERNAL",
    )


@pytest.fixture
def cas(tmp_path: Path) -> FilesystemArtifactCas:
    return FilesystemArtifactCas(tmp_path / "cas")


@pytest.fixture
def factory(tmp_path: Path) -> ConnectionFactory:
    made = ConnectionFactory(tmp_path / "harness.db")
    migrate(made, recorded_at=STORED_AT)
    return made


@pytest.fixture
def connection(factory: ConnectionFactory) -> Iterator[sqlite3.Connection]:
    conn = factory.connect(ConnectionRole.RUNTIME)
    try:
        yield conn
    finally:
        conn.close()


class _UnitOfWorkStore:
    """試験用。`put()` をUnit of Work相当のTransactionで包む。

    `ArtifactStore` は `commit()` しない（不変条件#15）。Manifest登録は
    呼出側が開いたTransactionの内側で行う契約であり、本来その役はApplication層の
    Unit of Workが担う。試験でも同じ境界を再現する。

    以前はTransaction外で`put()`を呼んでいた。Repositoryが前提を検査していな
    かったため素通りしていたが、検査を足した時点でこの試験群が落ちた。
    **試験の側が契約を守っていなかった**ということである。

    Transaction境界そのものの検査は
    `tests/integration/sqlite/test_transaction_precondition.py` が持つ。
    """

    def __init__(
        self,
        store: ArtifactStore,
        factory: ConnectionFactory,
        connection: sqlite3.Connection,
    ) -> None:
        self._store = store
        self._factory = factory
        self._connection = connection

    def put(self, *args: object, **kwargs: object) -> ArtifactManifestRecord:
        with self._factory.begin_immediate(self._connection):
            return self._store.put(*args, **kwargs)  # type: ignore[arg-type]

    def get(self, content_hash: ContentHash) -> bytes:
        return self._store.get(content_hash)

    def verify(self, content_hash: ContentHash) -> ArtifactVerification:
        return self._store.verify(content_hash)


@pytest.fixture
def store(
    cas: FilesystemArtifactCas,
    factory: ConnectionFactory,
    connection: sqlite3.Connection,
) -> _UnitOfWorkStore:
    return _UnitOfWorkStore(
        ArtifactStore(cas, SqliteArtifactManifestRepository(connection)),
        factory,
        connection,
    )


# --------------------------------------------------------------------------
# Manifest の一意性
# --------------------------------------------------------------------------


def test_duplicate_manifest_registration_is_rejected(
    factory: ConnectionFactory, connection: sqlite3.Connection
) -> None:
    """同じ `artifact_id` を二度登録させない。

    通すと、同じIDに別のContent Hashが並ぶ。あとから「このArtifact IDの
    中身は何か」に答えられなくなる。答えられない状態でReceiptが
    そのIDを参照している。

    第1層はCAS側の内容照合だが、Manifest表の一意制約が第2層にある。
    下の層が実際に効いているかを確かめる。
    """
    repository = SqliteArtifactManifestRepository(connection)
    record = ArtifactManifestRecord(
        artifact_id="artifact-1",
        content_hash=hash_bytes(PAYLOAD),
        media_type="text/plain",
        size_bytes=len(PAYLOAD),
        data_classification="INTERNAL",
        trust_level="VERIFIED_INTERNAL",
        stored_at=STORED_AT,
        storage_path="cas/aa/bb",
        payload_deleted=False,
    )

    with factory.begin_immediate(connection):
        repository.register(record)

    with factory.begin_immediate(connection), pytest.raises(HarnessError) as error:
        repository.register(record)
    assert error.value.code is ErrorCode.ARTIFACT_CONTENT_CONFLICT


def test_all_content_hashes_lists_registered_artifacts(
    factory: ConnectionFactory, connection: sqlite3.Connection
) -> None:
    """GCの土台。登録済みContent Hashを列挙できること。

    `all_content_hashes()` は書かれてはいるが、実装のどこからも呼ばれて
    いない。GC（Tier 5）が使う前提の関数である。呼ばれないまま置いておくと、
    いざ使う段になって初めて壊れていることが分かる。
    """
    repository = SqliteArtifactManifestRepository(connection)
    hashes = []
    for index, payload in enumerate((b"one\n", b"two\n", b"three\n")):
        content_hash = hash_bytes(payload)
        hashes.append(content_hash)
        with factory.begin_immediate(connection):
            repository.register(
                ArtifactManifestRecord(
                    artifact_id=f"artifact-{index}",
                    content_hash=content_hash,
                    media_type="text/plain",
                    size_bytes=len(payload),
                    data_classification="INTERNAL",
                    trust_level="VERIFIED_INTERNAL",
                    stored_at=STORED_AT,
                    storage_path=f"cas/{index}",
                    payload_deleted=False,
                )
            )

    listed = repository.all_content_hashes()
    assert sorted(str(h) for h in listed) == sorted(str(h) for h in hashes)


# --------------------------------------------------------------------------
# 書込み順序（不変条件#13）
# --------------------------------------------------------------------------


def test_durable_write_order_is_temp_fsync_rename_dirfsync(
    cas: FilesystemArtifactCas, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§1.14 の5段の順序を実際の呼出列で確認する。

    順序を1つ入れ替えても平常時のテストは通ってしまう。Crash時にだけ壊れるため、
    呼出順そのものを固定する。
    """
    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def traced_fsync(fd: int) -> None:
        # ディレクトリのfsyncとファイルのfsyncを区別する
        kind = "dir" if os.path.isdir(f"/proc/self/fd/{fd}") else "file"
        calls.append(f"fsync:{kind}")
        real_fsync(fd)

    def traced_replace(src: Any, dst: Any) -> None:
        calls.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", traced_fsync)
    monkeypatch.setattr(os, "replace", traced_replace)

    cas.write_bytes(PAYLOAD, _metadata())

    assert calls == ["fsync:file", "replace", "fsync:dir"], calls


def test_temp_file_is_inside_cas_root(
    cas: FilesystemArtifactCas, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rename元が同一Filesystemにあること。跨ぐとAtomicでなくなる。"""
    seen: list[Path] = []
    real_replace = os.replace

    def traced_replace(src: Any, dst: Any) -> None:
        seen.append(Path(src))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", traced_replace)
    cas.write_bytes(PAYLOAD, _metadata())

    assert len(seen) == 1
    assert cas.root in seen[0].parents


def test_no_temp_file_remains_after_success(cas: FilesystemArtifactCas) -> None:
    cas.write_bytes(PAYLOAD, _metadata())
    incoming = cas.root / "incoming"
    assert not incoming.exists() or not any(incoming.iterdir())


def test_no_temp_file_remains_after_failure(
    cas: FilesystemArtifactCas, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失敗時にTempを残さない。残すと孤児Bytesと区別できない。"""

    def exploding_replace(src: Any, dst: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", exploding_replace)
    with pytest.raises(OSError, match="disk full"):
        cas.write_bytes(PAYLOAD, _metadata())

    incoming = cas.root / "incoming"
    assert not incoming.exists() or not any(incoming.iterdir())


# --------------------------------------------------------------------------
# Content-addressed の性質
# --------------------------------------------------------------------------


def test_put_then_get_roundtrip(store: _UnitOfWorkStore) -> None:
    record = store.put(PAYLOAD, _metadata(), artifact_id="a-1", stored_at=STORED_AT)
    assert record.content_hash == hash_bytes(PAYLOAD)
    assert store.get(record.content_hash) == PAYLOAD
    assert record.storage_path.startswith("objects/")


def test_put_is_idempotent_for_identical_content(store: _UnitOfWorkStore) -> None:
    first = store.put(PAYLOAD, _metadata(), artifact_id="a-1", stored_at=STORED_AT)
    second = store.put(PAYLOAD, _metadata(), artifact_id="a-2", stored_at=STORED_AT)
    assert first.content_hash == second.content_hash
    # 2回目は既存Manifestを返す。artifact_idは最初のものが残る。
    assert second.artifact_id == "a-1"


def test_metadata_size_mismatch_is_rejected(store: _UnitOfWorkStore) -> None:
    """宣言Sizeと実Bytesの不一致を黙って受けない。"""
    with pytest.raises(HarnessError) as excinfo:
        store.put(PAYLOAD, _metadata(size=999), artifact_id="a-1", stored_at=STORED_AT)
    assert excinfo.value.code is ErrorCode.ARTIFACT_CONTENT_CONFLICT


def test_content_conflict_on_corrupted_object(
    cas: FilesystemArtifactCas, store: _UnitOfWorkStore
) -> None:
    """§1.14「同一Hashの既存Bytesがある場合は再Hashし、不一致なら停止」。"""
    record = store.put(PAYLOAD, _metadata(), artifact_id="a-1", stored_at=STORED_AT)
    cas.object_path(record.content_hash).write_bytes(b"tampered")

    with pytest.raises(HarnessError) as excinfo:
        store.put(PAYLOAD, _metadata(), artifact_id="a-2", stored_at=STORED_AT)
    assert excinfo.value.code is ErrorCode.ARTIFACT_CONTENT_CONFLICT


def test_get_detects_tampered_bytes(cas: FilesystemArtifactCas, store: _UnitOfWorkStore) -> None:
    record = store.put(PAYLOAD, _metadata(), artifact_id="a-1", stored_at=STORED_AT)
    cas.object_path(record.content_hash).write_bytes(b"tampered")
    with pytest.raises(HarnessError, match="hash to"):
        store.get(record.content_hash)


# --------------------------------------------------------------------------
# verify（§1.14 Repair方針）
# --------------------------------------------------------------------------


def test_verify_ok(store: _UnitOfWorkStore) -> None:
    record = store.put(PAYLOAD, _metadata(), artifact_id="a-1", stored_at=STORED_AT)
    assert store.verify(record.content_hash).outcome is VerificationOutcome.OK


def test_verify_detects_manifest_without_bytes(
    cas: FilesystemArtifactCas, store: _UnitOfWorkStore
) -> None:
    """Manifestあり・Bytesなし → BLOCKED_REPAIR_REQUIRED 相当。"""
    record = store.put(PAYLOAD, _metadata(), artifact_id="a-1", stored_at=STORED_AT)
    cas.object_path(record.content_hash).unlink()

    verification = store.verify(record.content_hash)
    assert verification.outcome is VerificationOutcome.BYTES_MISSING
    with pytest.raises(HarnessError, match="BLOCKED_REPAIR_REQUIRED"):
        verification.raise_if_repair_required()


def test_verify_detects_orphan_bytes(cas: FilesystemArtifactCas, store: _UnitOfWorkStore) -> None:
    """Bytesあり・Manifestなし → 孤児（GC候補）。停止事由ではない。"""
    content_hash = cas.write_bytes(PAYLOAD, _metadata())
    verification = store.verify(content_hash)
    assert verification.outcome is VerificationOutcome.ORPHAN_BYTES
    verification.raise_if_repair_required()


def test_verify_detects_content_mismatch(
    cas: FilesystemArtifactCas, store: _UnitOfWorkStore
) -> None:
    record = store.put(PAYLOAD, _metadata(), artifact_id="a-1", stored_at=STORED_AT)
    cas.object_path(record.content_hash).write_bytes(b"tampered")
    verification = store.verify(record.content_hash)
    assert verification.outcome is VerificationOutcome.CONTENT_MISMATCH
    assert verification.observed_hash == hash_bytes(b"tampered")


# --------------------------------------------------------------------------
# GC（AT-GC-001/RAW_RETENTION の基盤）
# --------------------------------------------------------------------------


def test_payload_delete_keeps_manifest(
    cas: FilesystemArtifactCas,
    factory: ConnectionFactory,
    connection: sqlite3.Connection,
    store: _UnitOfWorkStore,
) -> None:
    """AT-GC-001/RAW_RETENTION「payload_deleted かつ manifest_retained」。"""
    record = store.put(PAYLOAD, _metadata(), artifact_id="a-1", stored_at=STORED_AT)
    manifests = SqliteArtifactManifestRepository(connection)

    assert cas.delete_payload(record.content_hash) is True
    # Manifest更新もTransactionの内側で行う（不変条件#15）。
    with factory.begin_immediate(connection):
        manifests.mark_payload_deleted(record.content_hash)

    retained = manifests.find_by_content_hash(record.content_hash)
    assert retained is not None
    assert retained.payload_deleted is True
    assert not cas.object_path(record.content_hash).exists()


def test_delete_payload_is_false_when_absent(cas: FilesystemArtifactCas) -> None:
    assert cas.delete_payload(hash_bytes(b"never stored")) is False


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------


def test_migration_is_stepwise_and_idempotent(factory: ConnectionFactory) -> None:
    """既適用の版を再実行しない。版ごとに1行だけ記録される。"""
    migrate(factory, recorded_at=STORED_AT)
    conn = factory.connect(ConnectionRole.RUNTIME)
    try:
        rows = conn.execute("SELECT version FROM schema_migration ORDER BY version").fetchall()
    finally:
        conn.close()
    versions = [int(row["version"]) for row in rows]
    assert versions == sorted(set(versions))
    # 版数を直書きしない。Migrationを足すたびに試験を書き換える形にすると、
    # 「試験を通すために書き換えた」のか「意図した変更なのか」が消える。
    assert versions[-1] == SCHEMA_VERSION
    assert versions == list(range(1, SCHEMA_VERSION + 1)), "版の欠番がある"
