"""読取りの上限と、読取り中の実体差し替え（§1.16.2）。

## なぜ必要か

§1.16.2 は「Size、File Count、Depth、Read Time上限を超える入力」を拒否
すると定める。Depth の拒否には試験があったが、**Size の拒否には無かった**。
上限が効いていることを一度も確かめていない上限は、上限ではない。

同じく「読取り中にIdentityが変化していないこと」の再検査（TOCTOU）にも
試験が無かった。stat と read の間に実体が入れ替わる攻撃は、
stat の結果だけを信じる実装に対して成立する。

どちらもFail-Closed側の分岐であり、正常系をいくら実行しても到達しない。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.domain.errors import ErrorCode
from harness.domain.input_read import CapabilityScope, ReadDenial
from harness.infrastructure.filesystem import safe_reader as safe_reader_module
from harness.infrastructure.filesystem.safe_reader import (
    CapabilityBroker,
    SafeInputReader,
)
from harness.infrastructure.filesystem.workspace_boundary import FilesystemPolicy

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
CAPABILITY_ID = "cap-limits"
MAX_BYTES = safe_reader_module._MAX_BYTES


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "docs").mkdir(parents=True)
    return root


@pytest.fixture
def broker() -> Iterator[CapabilityBroker]:
    with CapabilityBroker(FilesystemPolicy.load(REPO_ROOT), allow_test_filesystems=True) as issued:
        yield issued


@pytest.fixture
def reader(workspace: Path, broker: CapabilityBroker) -> SafeInputReader:
    broker.issue(CAPABILITY_ID, workspace, CapabilityScope(("docs",)))
    return SafeInputReader(broker)


def _denial(result: object) -> ReadDenial:
    assert isinstance(result, ReadDenial), f"拒否されるはずが {type(result)} を返した"
    return result


# ---------------------------------------------------------------------------
# Size上限
# ---------------------------------------------------------------------------


def test_file_larger_than_the_limit_is_denied(workspace: Path, reader: SafeInputReader) -> None:
    """`st_size` が上限を超えるFileを開かない。

    疎Fileで作る。16MiBを実際に書く必要は無い。ここで見たいのは
    「上限判定が効くか」であって、Filesystemの書込み速度ではない。
    """
    target = workspace / "docs" / "huge.bin"
    with open(target, "wb") as handle:
        os.truncate(handle.fileno(), MAX_BYTES + 1)
    assert target.stat().st_size == MAX_BYTES + 1

    denial = _denial(reader.open_read(CAPABILITY_ID, "docs/huge.bin"))
    assert denial.error_code is ErrorCode.PATH_OUTSIDE_CAPABILITY
    assert "exceeds" in denial.detail


def test_file_exactly_at_the_limit_is_allowed(workspace: Path, reader: SafeInputReader) -> None:
    """境界そのものは通す。`>` と `>=` の取り違えを固定する。

    上限を1Byte厳しく間違えても、超過側の試験だけでは気付けない。
    """
    target = workspace / "docs" / "exact.bin"
    with open(target, "wb") as handle:
        os.truncate(handle.fileno(), MAX_BYTES)

    result = reader.open_read(CAPABILITY_ID, "docs/exact.bin")
    assert not isinstance(result, ReadDenial), result
    assert len(result[0]) == MAX_BYTES


def test_file_that_grows_past_the_limit_after_stat_is_denied(
    workspace: Path, reader: SafeInputReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """statを通ったあとに上限を超えて伸びるFile。

    `st_size` の検査は open 直後の一点でしかない。その後に書き足されれば、
    上限を超えたBytesを読み込むことになる。読みながらも数える必要がある。

    実体の伸長を待つと競合になるので、`os.read` が上限超えの量を返す状況を
    直接作る。ここで確かめたいのは Filesystem の挙動ではなく、
    **読取り側が数えているかどうか**である。
    """
    target = workspace / "docs" / "grows.bin"
    target.write_bytes(b"x" * 1024)

    real_read = os.read
    oversized = b"y" * (1 << 20)
    calls = {"count": 0}

    def fake_read(fd: int, length: int) -> bytes:
        # Reader が開いたFDに対してだけ、上限を超える量を返し続ける。
        if calls["count"] < (MAX_BYTES // len(oversized)) + 1:
            calls["count"] += 1
            return oversized
        return real_read(fd, length)

    monkeypatch.setattr(os, "read", fake_read)

    denial = _denial(reader.open_read(CAPABILITY_ID, "docs/grows.bin"))
    assert denial.error_code is ErrorCode.PATH_OUTSIDE_CAPABILITY
    assert "exceeded" in denial.detail


def test_io_error_during_the_read_is_denied_not_raised(
    workspace: Path, reader: SafeInputReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """読取り中のI/O失敗を例外として外へ出さない。

    回帰対象（2026-08-15検出・修正済み）: 上限超過が素の `OSError` として
    `open_read` の外へ抜けていた。Portの契約は `tuple | ReadDenial` を
    返すことであり、呼出側は拒否を受け取る前提で書かれている。
    例外で抜けると、拒否として扱われずそのまま落ちる。

    Fail-Closedのつもりの分岐が、実際にはFail-Crashだった。
    """
    (workspace / "docs" / "flaky.bin").write_bytes(b"z" * 4096)

    def failing_read(fd: int, length: int) -> bytes:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "read", failing_read)

    denial = _denial(reader.open_read(CAPABILITY_ID, "docs/flaky.bin"))
    assert denial.error_code is ErrorCode.PATH_OUTSIDE_CAPABILITY
    assert "read failed" in denial.detail
    # 不変条件#7。Detailへ内容や中身の断片を載せない。
    assert "zzz" not in denial.detail


# ---------------------------------------------------------------------------
# 読取り中の実体変化（TOCTOU）
# ---------------------------------------------------------------------------


def test_file_truncated_during_the_read_is_denied(
    workspace: Path, reader: SafeInputReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """読取りの最中にSizeが変わったら結果を返さない。

    open 直後の `fstat` と読了後の `fstat` を突き合わせる検査である。
    途中で差し替えられたFileは、前半と後半が別の内容になりうる。
    そのBytesのHashをEvidenceとして残すと、**存在しなかったFileの
    Hashを記録する**ことになる。

    1回目の `os.read` の直後に実体を縮める。競合待ちにしない。
    """
    target = workspace / "docs" / "shrinks.bin"
    target.write_bytes(b"a" * (2 << 20))  # 2MiB。1MiB刻みで2回以上読ませる

    real_read = os.read
    shrunk = {"done": False}

    def fake_read(fd: int, length: int) -> bytes:
        chunk = real_read(fd, length)
        if not shrunk["done"]:
            shrunk["done"] = True
            with open(target, "r+b") as handle:
                os.truncate(handle.fileno(), 16)
        return chunk

    monkeypatch.setattr(os, "read", fake_read)

    denial = _denial(reader.open_read(CAPABILITY_ID, "docs/shrinks.bin"))
    assert denial.error_code is ErrorCode.SPECIAL_FILE_DENIED
    assert "identity changed" in denial.detail


def test_unchanged_file_passes_the_identity_recheck(
    workspace: Path, reader: SafeInputReader
) -> None:
    """再検査が正常な読取りを巻き込んでいないこと。

    これが無いと「常にidentity changedを返す実装」でも上の試験は通る。
    """
    payload = b"b" * (2 << 20)
    (workspace / "docs" / "stable.bin").write_bytes(payload)

    result = reader.open_read(CAPABILITY_ID, "docs/stable.bin")
    assert not isinstance(result, ReadDenial), result
    assert result[0] == payload


@pytest.mark.parametrize("payload", [b"", b"synthetic content\n"])
@pytest.mark.parametrize("overdue", [False, True])
def test_deadline_includes_eof_and_accepts_exact_boundary(
    workspace: Path, reader: SafeInputReader, monkeypatch, payload, overdue
) -> None:
    (workspace / "docs" / "timed.txt").write_bytes(payload)
    now = [0.0]
    monkeypatch.setattr(safe_reader_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    original = os.read

    def timed_read(fd, size):
        data = original(fd, size)
        now[0] = safe_reader_module._MAX_READ_SECONDS + (0.001 if overdue else 0)
        return data

    monkeypatch.setattr(os, "read", timed_read)
    result = reader.open_read(CAPABILITY_ID, "docs/timed.txt")
    if overdue:
        denial = _denial(result)
        assert denial.error_code is ErrorCode.PATH_OUTSIDE_CAPABILITY
        assert "read time limit" in denial.detail
    else:
        assert not isinstance(result, ReadDenial)
        assert result[0] == payload


def test_path_resolution_uses_the_same_read_deadline(workspace, reader, monkeypatch):
    (workspace / "docs" / "timed.txt").write_bytes(b"synthetic\n")
    now = [0.0]
    monkeypatch.setattr(safe_reader_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    original = reader._open_beneath

    def late_open(*args):
        fd = original(*args)
        now[0] = safe_reader_module._MAX_READ_SECONDS + 1
        return fd

    monkeypatch.setattr(reader, "_open_beneath", late_open)

    def forbidden_read(fd, size):
        pytest.fail("read started after path resolution exhausted the deadline")

    monkeypatch.setattr(os, "read", forbidden_read)
    denial = _denial(reader.open_read(CAPABILITY_ID, "docs/timed.txt"))
    assert "read time limit" in denial.detail


def test_each_chunk_uses_the_original_deadline(workspace, reader, monkeypatch):
    (workspace / "docs" / "timed.txt").write_bytes(b"x" * (3 << 20))
    now = [0.0]
    calls = []
    monkeypatch.setattr(safe_reader_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    original = os.read

    def slow_chunks(fd, size):
        calls.append(fd)
        data = original(fd, size)
        now[0] += safe_reader_module._MAX_READ_SECONDS * 0.6
        return data

    monkeypatch.setattr(os, "read", slow_chunks)
    result = reader.open_read(CAPABILITY_ID, "docs/timed.txt")
    assert isinstance(result, ReadDenial)
    assert len(calls) == 2


def test_evidence_hashing_cannot_return_a_late_success(workspace, reader, monkeypatch):
    (workspace / "docs" / "timed.txt").write_bytes(b"synthetic\n")
    now = [0.0]
    monkeypatch.setattr(safe_reader_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    original = safe_reader_module.hash_bytes

    def late_hash(data):
        result = original(data)
        now[0] = safe_reader_module._MAX_READ_SECONDS + 1
        return result

    monkeypatch.setattr(safe_reader_module, "hash_bytes", late_hash)
    denial = _denial(reader.open_read(CAPABILITY_ID, "docs/timed.txt"))
    assert "before returning evidence" in denial.detail
