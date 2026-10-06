"""CASの競合Putと欠落Object（ADR-001、`ARTIFACT_CONTENT_CONFLICT`）。

## なぜ必要か

`filesystem_cas.py` の次の分岐は一度も実行されていなかった。

* 既にObjectが存在する状態でのPut（競合Put）
* 内容がHashと合わないObjectが既に置かれている
* Bytesが存在しないHashのread

CASは「名前がHashである」ことに全面的に依存する。その前提が崩れた
ときにどうなるかを確かめていない状態で、前提の上に Ledger と Receipt が
載っている。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.domain.artifact import ArtifactMetadata
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_bytes
from harness.infrastructure.artifact.filesystem_cas import FilesystemArtifactCas

pytestmark = pytest.mark.integration

PAYLOAD = b"artifact bytes\n"


def metadata_for(data: bytes) -> ArtifactMetadata:
    return ArtifactMetadata(
        media_type="text/plain",
        size_bytes=len(data),
        data_classification="INTERNAL",
        trust_level="UNTRUSTED",
    )


def put(cas: FilesystemArtifactCas, data: bytes = PAYLOAD):
    return cas.write_bytes(data, metadata_for(data))


@pytest.fixture
def cas(tmp_path: Path) -> FilesystemArtifactCas:
    return FilesystemArtifactCas(tmp_path / "objects")


# ---------------------------------------------------------------------------
# 競合Put
# ---------------------------------------------------------------------------


def test_putting_the_same_bytes_twice_is_idempotent(cas: FilesystemArtifactCas) -> None:
    """同じBytesの再Putは成功する。

    CASは内容で名前が決まるので、同じ内容の再投入は「既にある」だけである。
    ここで失敗にすると、再送や再実行のたびに止まる。
    """
    first = put(cas)
    second = put(cas)
    assert first == second
    assert cas.read_bytes(first) == PAYLOAD


def test_existing_object_with_wrong_content_is_rejected(cas: FilesystemArtifactCas) -> None:
    """Hashの位置に別の内容が置かれている。

    起こり得るのは、Hash衝突ではなく**Filesystem側の破損や外部からの
    書換え**である。上書きして直すと、破損の事実が消えて次に困る。
    上書きせず、照合して止める。
    """
    content_hash = put(cas)
    path = cas.object_path(content_hash)
    path.write_bytes(b"replaced by something else\n")

    with pytest.raises(HarnessError) as error:
        put(cas)
    assert error.value.code is ErrorCode.ARTIFACT_CONTENT_CONFLICT
    assert str(content_hash) in str(error.value)

    # 上書きしていないこと。破損したBytesはそのまま残っている。
    assert path.read_bytes() == b"replaced by something else\n"


def test_conflicting_put_leaves_no_temporary_file(
    cas: FilesystemArtifactCas, tmp_path: Path
) -> None:
    """競合で止まってもTempを残さない。

    孤児Bytesと区別できなくなる。GCが「参照の無いObject」として
    数える対象がぶれる。
    """
    content_hash = put(cas)
    cas.object_path(content_hash).write_bytes(b"corrupted\n")

    with pytest.raises(HarnessError):
        put(cas)

    leftovers = [p.name for p in (tmp_path / "objects").rglob("*") if p.is_file()]
    assert all(not name.startswith(".tmp") for name in leftovers), leftovers


# ---------------------------------------------------------------------------
# 欠落Object
# ---------------------------------------------------------------------------


def test_reading_missing_bytes_is_rejected(cas: FilesystemArtifactCas) -> None:
    """Manifestにあるのに実体が無い場合。

    GCが消したか、Filesystemが失ったか。どちらにせよ空を返してはならない。
    空を返すと、呼出側は「中身の無いArtifact」として先へ進む。
    """
    absent = hash_bytes(b"never stored")
    with pytest.raises(HarnessError) as error:
        cas.read_bytes(absent)
    assert error.value.code is ErrorCode.ARTIFACT_CONTENT_CONFLICT
    assert "missing" in str(error.value)


def test_reading_corrupted_bytes_is_rejected(cas: FilesystemArtifactCas) -> None:
    """読み出したBytesがHashと合わない。

    名前がHashである以上、合わないなら名前が嘘になっている。
    """
    content_hash = put(cas)
    cas.object_path(content_hash).write_bytes(b"tampered\n")

    with pytest.raises(HarnessError) as error:
        cas.read_bytes(content_hash)
    assert error.value.code is ErrorCode.ARTIFACT_CONTENT_CONFLICT
    assert "hash to" in str(error.value)
