"""Capability の FD ライフサイクル試験（§1.16.2、レビュー BLOCKER 5）。

## 何を守る試験か

Capabilityは発行時に検証したRoot Directoryへ束縛される。その束縛を
**FD番号**で表すと、番号が再利用された瞬間に崩れる。

    Broker発行:              root_fd = 4
    broker.close()
    同じWorkspaceを再open:   fd = 4      ← 番号が再利用される
    古いReaderで読取り                    ← 通ってしまう

再openしたDirectoryはDevice/Inodeも一致するため、Identity検査でも
捕まらない。読取り中にcloseして同じ番号を別Workspaceへ割り当てれば、
別のWorkspaceを読ませることもできる。

対策は所有権の分離である。FDはBrokerのprivateとし、読取りは`dup`済みの
Leaseを通してのみ行う。Leaseが生きている間はFile Descriptionが解放されず、
番号の再利用が起きない。
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.input_read import CapabilityScope, ReadDenial
from harness.infrastructure.filesystem.safe_reader import (
    CapabilityBroker,
    CapabilityIdInUse,
    CapabilityRevoked,
    SafeInputReader,
)
from harness.infrastructure.filesystem.workspace_boundary import FilesystemPolicy

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
PROTECTED = b"protected\n"
REPLACEMENT = b"replacement-secret\n"


@pytest.fixture(scope="module")
def policy() -> FilesystemPolicy:
    return FilesystemPolicy.load(REPO_ROOT)


def make_workspace(root: Path, payload: bytes) -> Path:
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.txt").write_bytes(payload)
    return root


@pytest.fixture
def broker(policy: FilesystemPolicy) -> Iterator[CapabilityBroker]:
    with CapabilityBroker(policy, allow_test_filesystems=True) as issued:
        yield issued


# ---------------------------------------------------------------------------
# FD番号の再利用
# ---------------------------------------------------------------------------


def test_reader_denies_after_broker_close_even_if_fd_number_is_reused(
    tmp_path: Path, policy: FilesystemPolicy
) -> None:
    """close後に同じFD番号で同じWorkspaceが再openされても読ませない。

    レビューで再現されたバイパス。生のFD番号をCapabilityへ載せていたため、
    番号が再利用されると古いReaderがそのまま通っていた。
    """
    workspace = make_workspace(tmp_path / "ws", PROTECTED)
    broker = CapabilityBroker(policy, allow_test_filesystems=True)
    broker.issue("cap-1", workspace, CapabilityScope(("docs",)))
    reader = SafeInputReader(broker)

    first = reader.open_read("cap-1", "docs/a.txt")
    assert not isinstance(first, ReadDenial), first
    assert first[0] == PROTECTED

    broker.close()

    # 解放された番号を埋めるため、同じWorkspaceを何度も開き直す。
    reopened = [os.open(workspace, os.O_RDONLY | os.O_DIRECTORY) for _ in range(16)]
    try:
        denial = reader.open_read("cap-1", "docs/a.txt")
        assert isinstance(denial, ReadDenial), (
            "close後のCapabilityで読めてしまった。FD番号の再利用で束縛が崩れている。"
        )
        assert denial.error_code is ErrorCode.PATH_OUTSIDE_CAPABILITY
    finally:
        for fd in reopened:
            os.close(fd)


def test_revoked_capability_is_denied(tmp_path: Path, broker: CapabilityBroker) -> None:
    """個別失効。以後の読取りは拒否する。"""
    workspace = make_workspace(tmp_path / "ws", PROTECTED)
    broker.issue("cap-rev", workspace, CapabilityScope(("docs",)))
    reader = SafeInputReader(broker)
    assert not isinstance(reader.open_read("cap-rev", "docs/a.txt"), ReadDenial)

    broker.revoke("cap-rev")
    denial = reader.open_read("cap-rev", "docs/a.txt")
    assert isinstance(denial, ReadDenial)
    assert denial.error_code is ErrorCode.PATH_OUTSIDE_CAPABILITY


def test_reader_does_not_hold_a_stale_capability_copy(
    tmp_path: Path, broker: CapabilityBroker
) -> None:
    """Readerは辞書を複製しない。有効性の権威はBrokerだけである。"""
    workspace = make_workspace(tmp_path / "ws", PROTECTED)
    broker.issue("cap-copy", workspace, CapabilityScope(("docs",)))
    reader = SafeInputReader(broker)
    broker.revoke("cap-copy")
    assert broker.lookup("cap-copy") is None
    assert isinstance(reader.open_read("cap-copy", "docs/a.txt"), ReadDenial)


# ---------------------------------------------------------------------------
# 読取り中のclose/revokeとの競合
# ---------------------------------------------------------------------------


def test_lease_survives_a_concurrent_close(tmp_path: Path, policy: FilesystemPolicy) -> None:
    """読取り中にBrokerがcloseしても、その読取りは別Workspaceを読まない。

    Leaseは`dup`済みFDであり、元のFile Descriptionを生かす。番号が
    別Workspaceへ割り当て直されることがない。
    """
    protected = make_workspace(tmp_path / "protected", PROTECTED)
    intruder = make_workspace(tmp_path / "intruder", REPLACEMENT)

    broker = CapabilityBroker(policy, allow_test_filesystems=True)
    broker.issue("cap-race", protected, CapabilityScope(("docs",)))

    grant = broker.acquire_root("cap-race")
    ready = threading.Barrier(2)

    def closer() -> None:
        ready.wait(timeout=5)
        broker.close()
        # 解放された番号を別Workspaceで埋めにいく。
        for _ in range(32):
            os.open(intruder, os.O_RDONLY | os.O_DIRECTORY)

    thread = threading.Thread(target=closer)
    thread.start()
    ready.wait(timeout=5)
    thread.join(timeout=10)

    # Leaseは生きており、指しているのは元のWorkspaceのまま。
    lease_stat = os.fstat(grant.lease.fd)
    assert lease_stat.st_ino == os.stat(protected).st_ino
    assert lease_stat.st_ino != os.stat(intruder).st_ino
    grant.lease.release()


def test_acquire_root_after_close_raises(tmp_path: Path, policy: FilesystemPolicy) -> None:
    workspace = make_workspace(tmp_path / "ws", PROTECTED)
    broker = CapabilityBroker(policy, allow_test_filesystems=True)
    broker.issue("cap-x", workspace, CapabilityScope(("docs",)))
    broker.close()
    with pytest.raises(CapabilityRevoked):
        broker.acquire_root("cap-x")


# ---------------------------------------------------------------------------
# close の冪等性
# ---------------------------------------------------------------------------


def test_close_is_idempotent_without_swallowing_errors(
    tmp_path: Path, policy: FilesystemPolicy
) -> None:
    """2回目のcloseで例外を出さない。ただし`OSError`は握り潰さない。

    握り潰すと、FD管理の不具合が静かに埋もれる。閉じ済みかどうかは
    状態で判断する。
    """
    workspace = make_workspace(tmp_path / "ws", PROTECTED)
    broker = CapabilityBroker(policy, allow_test_filesystems=True)
    broker.issue("cap-idem", workspace, CapabilityScope(("docs",)))
    broker.close()
    broker.close()  # 2回目。例外を出さない
    assert broker.lookup("cap-idem") is None


def test_issue_after_close_is_rejected(tmp_path: Path, policy: FilesystemPolicy) -> None:
    workspace = make_workspace(tmp_path / "ws", PROTECTED)
    broker = CapabilityBroker(policy, allow_test_filesystems=True)
    broker.close()
    with pytest.raises(CapabilityRevoked):
        broker.issue("cap-late", workspace, CapabilityScope(("docs",)))


def test_lease_release_is_idempotent(tmp_path: Path, broker: CapabilityBroker) -> None:
    workspace = make_workspace(tmp_path / "ws", PROTECTED)
    broker.issue("cap-lease", workspace, CapabilityScope(("docs",)))
    lease = broker.acquire_root("cap-lease").lease
    fd = lease.fd
    lease.release()
    lease.release()  # 2回目
    with pytest.raises(OSError):
        os.fstat(fd)


def test_lease_fd_is_a_distinct_number_from_the_broker_handle(
    tmp_path: Path, broker: CapabilityBroker
) -> None:
    """`dup`しているため番号は別。同じFile Descriptionを指す。"""
    workspace = make_workspace(tmp_path / "ws", PROTECTED)
    broker.issue("cap-dup", workspace, CapabilityScope(("docs",)))
    first = broker.acquire_root("cap-dup").lease
    second = broker.acquire_root("cap-dup").lease
    try:
        assert first.fd != second.fd
        assert os.fstat(first.fd).st_ino == os.fstat(second.fd).st_ino
    finally:
        first.release()
        second.release()


# ---------------------------------------------------------------------------
# Capability ID の一意性（tombstone）
# ---------------------------------------------------------------------------


def test_revoked_capability_id_can_never_be_reissued(
    tmp_path: Path, broker: CapabilityBroker
) -> None:
    """失効IDは永久に再利用できない。

    レビューで再現されたバイパス。`revoke()` が記録ごと消していたため、
    同じIDを別Workspaceへ再発行でき、**古いID保持者が新しいWorkspaceを
    読めていた**。

        same-id -> Workspace A へ発行
        same-id を revoke
        same-id -> Workspace B へ再発行
        古い same-id 保持者が読取り  -> Workspace B の new-secret が読めた
    """
    workspace_a = make_workspace(tmp_path / "a", PROTECTED)
    workspace_b = make_workspace(tmp_path / "b", REPLACEMENT)

    broker.issue("same-id", workspace_a, CapabilityScope(("docs",)))
    broker.revoke("same-id")

    with pytest.raises(CapabilityIdInUse, match="revoked"):
        broker.issue("same-id", workspace_b, CapabilityScope(("docs",)))

    reader = SafeInputReader(broker)
    denial = reader.open_read("same-id", "docs/a.txt")
    assert isinstance(denial, ReadDenial)


def test_active_capability_id_cannot_be_reissued(tmp_path: Path, broker: CapabilityBroker) -> None:
    workspace_a = make_workspace(tmp_path / "a", PROTECTED)
    workspace_b = make_workspace(tmp_path / "b", REPLACEMENT)
    broker.issue("dup-id", workspace_a, CapabilityScope(("docs",)))
    with pytest.raises(CapabilityIdInUse, match="active"):
        broker.issue("dup-id", workspace_b, CapabilityScope(("docs",)))


def test_failed_issuance_also_burns_the_id(tmp_path: Path, broker: CapabilityBroker) -> None:
    """検証に失敗したIDも再利用させない。

    「失敗したなら空いている」とすると、失敗の直後に別のWorkspaceで同じIDを
    取れてしまう。IDは1回限りの識別子として扱う。
    """
    workspace = make_workspace(tmp_path / "ws", PROTECTED)
    with pytest.raises(HarnessError):
        broker.issue(
            "burned-id",
            workspace,
            CapabilityScope(("docs",)),
            mountinfo_text="28 1 99:99 / / rw - ext4 /dev/x rw\n",
        )
    with pytest.raises(CapabilityIdInUse, match="revoked"):
        broker.issue("burned-id", workspace, CapabilityScope(("docs",)))


def test_concurrent_issue_of_the_same_id_yields_exactly_one_success(
    tmp_path: Path, broker: CapabilityBroker
) -> None:
    """同一IDの並行発行は1件だけ成功する。

    レビューで再現された不具合。重複確認後にlockを解放してFilesystem検証し、
    再確認せずに格納していたため2件とも成功していた。後勝ちで記録が
    上書きされ、もう一方のFDがリークしていた。
    """
    workspace_a = make_workspace(tmp_path / "a", PROTECTED)
    workspace_b = make_workspace(tmp_path / "b", REPLACEMENT)

    start = threading.Barrier(2)
    successes: list[Path] = []
    failures: list[Exception] = []
    guard = threading.Lock()

    def attempt(workspace: Path) -> None:
        start.wait(timeout=5)
        try:
            broker.issue("race-id", workspace, CapabilityScope(("docs",)))
            with guard:
                successes.append(workspace)
        except Exception as exc:  # 競合の結果を集めるため広く捕まえる
            with guard:
                failures.append(exc)

    threads = [
        threading.Thread(target=attempt, args=(workspace_a,)),
        threading.Thread(target=attempt, args=(workspace_b,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(successes) == 1, f"並行発行が{len(successes)}件成功した"
    assert len(failures) == 1
    assert isinstance(failures[0], CapabilityIdInUse)

    capability = broker.lookup("race-id")
    assert capability is not None
    assert capability.workspace_root == successes[0]


# ---------------------------------------------------------------------------
# 読取り中の失効
# ---------------------------------------------------------------------------


def test_read_result_is_denied_when_revoked_mid_read(
    tmp_path: Path, broker: CapabilityBroker
) -> None:
    """Lease取得後にrevokeされたら、Bytesが読めていても返さない。

    Leaseを生かすのは**安全にcloseするため**であって、認可の延長ではない。
    読取りの完了と、結果を認可済みとして返してよいかは別の判断である。
    """
    workspace = make_workspace(tmp_path / "ws", PROTECTED)
    broker.issue("cap-midread", workspace, CapabilityScope(("docs",)))
    reader = SafeInputReader(broker)

    original_acquire = broker.acquire_root

    def acquire_then_revoke(capability_id: str) -> object:
        grant = original_acquire(capability_id)
        broker.revoke(capability_id)  # Lease取得直後に失効
        return grant

    broker.acquire_root = acquire_then_revoke  # type: ignore[method-assign]
    try:
        result = reader.open_read("cap-midread", "docs/a.txt")
    finally:
        broker.acquire_root = original_acquire  # type: ignore[method-assign]

    assert isinstance(result, ReadDenial), "失効後の読取り結果が返却された"
    assert result.error_code is ErrorCode.PATH_OUTSIDE_CAPABILITY
    assert "revoked while the read was in progress" in result.detail


def test_generation_is_checked_not_only_the_id(tmp_path: Path, broker: CapabilityBroker) -> None:
    """再検証はIDだけでなく世代も見る。"""
    workspace = make_workspace(tmp_path / "ws", PROTECTED)
    broker.issue("cap-gen", workspace, CapabilityScope(("docs",)))
    grant = broker.acquire_root("cap-gen")
    try:
        assert broker.is_active("cap-gen", grant.generation)
        assert not broker.is_active("cap-gen", grant.generation + 1)
    finally:
        grant.lease.release()


def test_generations_are_distinct_per_capability(tmp_path: Path, broker: CapabilityBroker) -> None:
    first = make_workspace(tmp_path / "a", PROTECTED)
    second = make_workspace(tmp_path / "b", PROTECTED)
    broker.issue("cap-a", first, CapabilityScope(("docs",)))
    broker.issue("cap-b", second, CapabilityScope(("docs",)))
    grant_a = broker.acquire_root("cap-a")
    grant_b = broker.acquire_root("cap-b")
    try:
        assert grant_a.generation != grant_b.generation
    finally:
        grant_a.lease.release()
        grant_b.lease.release()


def test_capability_does_not_expose_a_raw_fd() -> None:
    """`IssuedCapability` にFD番号のFieldを持たせない。

    公開するとBrokerの外でcloseやdupができ、所有権が壊れる。
    """
    import dataclasses

    from harness.infrastructure.filesystem.safe_reader import IssuedCapability

    names = {field.name for field in dataclasses.fields(IssuedCapability)}
    assert "root_fd" not in names
    assert names == {"capability_id", "workspace_root", "scope", "mount_attestation"}
