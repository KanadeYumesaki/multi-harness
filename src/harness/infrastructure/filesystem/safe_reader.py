"""§1.16.2 Linux Safe Read の実装。

Workspace RootのDirectory Handleを基準に読み、次を拒否する。

| 拒否対象 | Reason | 実装 |
|---|---|---|
| 絶対Path、`..`、NUL、Windows形 | `PATH_OUTSIDE_CAPABILITY` | `validate_relative_path` |
| Capability Scope外 | `PATH_OUTSIDE_CAPABILITY` | `CapabilityScope.permits` |
| Symlink、Magic Link | `SYMLINK_DENIED` | `RESOLVE_NO_SYMLINKS`／`O_NOFOLLOW` |
| Hardlinkの別名の所在が未確認 | `PATH_OUTSIDE_CAPABILITY` | `st_nlink == 1`を読取り前後で検証 |
| Mount越境 | `MOUNT_CROSSING_DENIED` | `RESOLVE_NO_XDEV`／`st_dev`比較 |
| Device、FIFO、Socket、Dir | `SPECIAL_FILE_DENIED` | `fstat`のFile Type検査 |
| 読取り中のIdentity変化 | `SPECIAL_FILE_DENIED` | 読取り前後の`fstat`比較 |

Path文字列だけで弾けるものはFilesystemへ触れる**前**に落とす。実体へ触れてから
判定すると、判定前にsymlink追跡やDevice openが起きうる。

`openat2`が使える場合はKernel側でPath解決全体を制約する。使えない場合は
Component単位の`openat` + `O_NOFOLLOW`へFail-Closedで切り替える（ADR-003）。

**拒否は例外ではなく`ReadDenial`で返す。** §1.16.2が拒否ごとの記録を要求するため、
呼出側が`INPUT_READ_DENIED`をLedgerへ書けるようにする。
"""

from __future__ import annotations

import os
import stat
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from harness.domain.errors import HarnessError
from harness.domain.hashing import hash_bytes
from harness.domain.input_read import (
    CapabilityScope,
    DenialReason,
    ReadDenial,
    validate_relative_path,
)
from harness.infrastructure.filesystem.openat2 import (
    RESOLVE_BENEATH,
    RESOLVE_NO_MAGICLINKS,
    RESOLVE_NO_SYMLINKS,
    RESOLVE_NO_XDEV,
    Openat2Unavailable,
    is_available,
    openat2,
)
from harness.infrastructure.filesystem.workspace_boundary import (
    FilesystemPolicy,
    WorkspaceMountAttestation,
    open_and_verify_workspace,
    read_fd_mount_id,
)
from harness.ports.safe_input_reader import ReadEvidence

__all__ = [
    "CapabilityBroker",
    "CapabilityIdInUse",
    "CapabilityRevoked",
    "IssuedCapability",
    "RootGrant",
    "SafeInputReader",
    "WorkspaceRootLease",
]

_RESOLVE_STRICT = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_XDEV

# **`O_NONBLOCK` は必須である。**
#
# FIFOを`O_RDONLY`で開くと、書き手が現れるまで`open`自体がブロックする。
# Workspaceへ細工されたFIFOが1つあるだけでHarnessが無期限に停止し、
# §1.16.2の「特殊File拒否」へ到達する前に固まる。File Type検査は
# `open`が返ってからでないと行えないため、先に`open`を返させる必要がある。
# 通常Fileに対しては無効果である。
_OPEN_FLAGS = os.O_RDONLY | os.O_NONBLOCK

# 上限。§1.16.2「Size、File Count、Depth、Read Time上限を超える入力」を拒否する。
_MAX_BYTES = 16 * 1024 * 1024
_MAX_DEPTH = 32
# 列挙と単一File読取りの経過時間上限。停止中のKernel I/Oの強制中断ではない。
_MAX_READ_SECONDS = 2.0


class _ReadLimitExceeded(OSError):
    """読取り中にSize上限を超えた。

    `OSError` を継承させて、読取り経路の失敗を1箇所で捕まえられるようにする。
    区別できる型にしておくのは、Detailの文言を変えるためであって、
    どちらも拒否になることに違いは無い。
    """


@dataclass(frozen=True, slots=True)
class IssuedCapability:
    """発行済みInputReadCapability（本Taskで扱う最小形）。

    署名・Expiry・Revocationの検証はCapability Registry実装（別Task）で足す。

    ## 生のFD番号を公開しない

    Capabilityは**発行時に検証したRoot Directoryそのもの**へ束縛される。
    Pathではない。Pathを保持して読取りのたびに開き直すと、Workspaceを
    renameして同じPathへ別のDirectoryを置く差し替えが成立する。

    ただしFD番号をここへ載せてはならない。番号は**閉じた瞬間に再利用される**。

        Broker発行:              root_fd = 4
        broker.close()
        同じWorkspaceを再open:   fd = 4
        古いReaderで読取り       → 通ってしまう

    再openしたDirectoryはDevice/Inodeも一致するため、Identity検査でも
    捕まらない。読取り中にcloseして同じ番号を別Workspaceへ割り当てれば、
    別のWorkspaceを読ませることもできる（いずれもレビューで再現された）。

    したがってFDは`CapabilityBroker`のprivateとし、読取りは
    `acquire_root()` が返すLease（`dup`済みFD）を通してのみ行う。
    Leaseを取るとその読取りが終わるまでFile Descriptionが生き続けるため、
    途中でBrokerがcloseしても番号の再利用は起きない。

    `mount_attestation` は検証の証跡であり、`root_dev`／`root_ino` を含む。
    """

    capability_id: str
    workspace_root: Path
    scope: CapabilityScope
    mount_attestation: WorkspaceMountAttestation


class CapabilityRevoked(Exception):
    """失効済み、または未発行のCapabilityに対してLeaseを要求した。"""


class CapabilityIdInUse(Exception):
    """Capability IDが既に使われている。

    発行中・有効・失効済みのいずれでも送出する。**失効済みIDは永久に
    再利用できない**（tombstone）。
    """


class _SlotState(Enum):
    """Capability IDの状態。IDはこの3状態を一方向にしか進まない。"""

    ISSUING = "ISSUING"
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


@dataclass(slots=True)
class _Slot:
    """Capability ID 1件分の記録。**失効後も消さない。**

    消すとIDが再利用可能になり、古いID保持者が別Workspaceを読めてしまう。
    `generation` は同一Broker内で単調増加し、読取り終了時の再検証に使う。
    """

    state: _SlotState
    generation: int
    capability: IssuedCapability | None = None
    root_fd: int | None = None


@dataclass(frozen=True, slots=True)
class RootGrant:
    """Lease取得時にlock内で確定した一式。

    Capability・世代・Leaseを別々に取ると、その隙間でrevokeされた場合に
    どこまでが有効だったのか判断できない。原子的に受け取る。
    """

    capability: IssuedCapability
    generation: int
    lease: WorkspaceRootLease


class WorkspaceRootLease:
    """Root Handleの貸出し。`dup`済みFDを持ち、`close`で返す。

    `dup` した時点で元のFDとは別の番号になるが、**同じFile Description**を
    指す。Brokerが正本FDを閉じても、Leaseが生きている間は対象Directoryが
    解放されず、番号の再利用も起きない。読取り中のclose/revokeと安全に
    共存できるのはこのためである。
    """

    __slots__ = ("_closed", "_fd")

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._closed = False

    @property
    def fd(self) -> int:
        if self._closed:
            raise ValueError("lease is already released")
        return self._fd

    def __enter__(self) -> WorkspaceRootLease:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def release(self) -> None:
        if self._closed:
            return
        self._closed = True
        os.close(self._fd)


class CapabilityBroker:
    """Root Directory FDを**private に**保持してCapabilityを発行する。

    §0.1「起動時とInputReadCapability発行時に`/proc/self/mountinfo`から
    最長Prefix一致でMountを特定する」の発行時側。

    FDの生存期間を持つため、明示的な`close()`か`with`が要る。

    `_lock` はLease取得・失効・closeを直列化する。Lease発行の途中で
    正本FDが閉じられると、`dup`が別のObjectを指しかねない。
    """

    def __init__(
        self,
        filesystem_policy: FilesystemPolicy,
        *,
        allow_test_filesystems: bool = False,
    ) -> None:
        self._policy = filesystem_policy
        self._allow_test_filesystems = allow_test_filesystems
        self._lock = threading.Lock()
        self._slots: dict[str, _Slot] = {}
        self._next_generation = 1
        self._closed = False

    def __enter__(self) -> CapabilityBroker:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------

    def issue(
        self,
        capability_id: str,
        workspace_root: Path,
        scope: CapabilityScope,
        *,
        mountinfo_text: str | None = None,
    ) -> IssuedCapability:
        """Workspace Filesystem境界を確認してからCapabilityを発行する。

        不合格は `WORKSPACE_ON_FOREIGN_FS_DENIED` を送出する。ここは拒否を
        `ReadDenial` で返さない。**Capabilityを発行しない**ことが拒否であり、
        発行されなかったCapabilityでは読取り自体が始まらない。
        """
        # --- IDをlock内で予約する ---
        #
        # 重複確認だけしてlockを解放し、Filesystem検証のあと再確認せずに
        # 格納していたため、同じIDの並行発行が2件とも成功していた。
        # 後勝ちで記録が上書きされ、もう一方のFDがリークしていた。
        #
        # 予約（ISSUING）をlock内で置けば、2件目は必ず弾かれる。
        with self._lock:
            self._ensure_open()
            self._reserve(capability_id)
            generation = self._slots[capability_id].generation

        # 検証はlockの外で行う。Filesystem I/Oを含むため、ここを握ったままだと
        # 他のCapabilityのLease取得まで止まる。
        try:
            root_fd, attestation = open_and_verify_workspace(
                workspace_root,
                self._policy,
                mountinfo_text=mountinfo_text,
                allow_test_filesystems=self._allow_test_filesystems,
            )
        except BaseException:
            # **検証に失敗したIDも再利用させない。**
            # 「失敗したなら空いている」とすると、失敗の直後に別のWorkspaceで
            # 同じIDを取れる。IDは1回限りの識別子として扱う。
            with self._lock:
                self._slots[capability_id].state = _SlotState.REVOKED
            raise

        capability = IssuedCapability(
            capability_id=capability_id,
            workspace_root=workspace_root,
            scope=scope,
            mount_attestation=attestation,
        )
        with self._lock:
            slot = self._slots[capability_id]
            # 検証中にcloseやrevokeが起きた可能性がある。開いたFDを捨てて失敗させる。
            if self._closed or slot.state is not _SlotState.ISSUING:
                slot.state = _SlotState.REVOKED
                os.close(root_fd)
                raise CapabilityRevoked(
                    f"capability was revoked or the broker closed during issuance: {capability_id}"
                )
            slot.state = _SlotState.ACTIVE
            slot.capability = capability
            slot.root_fd = root_fd
            slot.generation = generation
        return capability

    # -- 以下 `_lock` を保持した状態で呼ぶこと ------------------------------

    def _ensure_open(self) -> None:
        if self._closed:
            raise CapabilityRevoked("broker is closed")

    def _reserve(self, capability_id: str) -> None:
        """IDを予約する。既に使われていれば状態を問わず拒否する。

        `REVOKED` も拒否対象である。失効IDを再利用できると、古いID保持者が
        別Workspaceを読めてしまう（tombstone）。
        """
        existing = self._slots.get(capability_id)
        if existing is not None:
            raise CapabilityIdInUse(
                f"capability id is already {existing.state.value.lower()}: {capability_id}"
            )
        self._slots[capability_id] = _Slot(
            state=_SlotState.ISSUING, generation=self._next_generation
        )
        self._next_generation += 1

    # ----------------------------------------------------------------------

    def lookup(self, capability_id: str) -> IssuedCapability | None:
        """有効なCapabilityを返す。発行中・失効済み・未発行はNone。"""
        with self._lock:
            if self._closed:
                return None
            slot = self._slots.get(capability_id)
            if slot is None or slot.state is not _SlotState.ACTIVE:
                return None
            return slot.capability

    def acquire_root(self, capability_id: str) -> RootGrant:
        """Capability・世代・Leaseを原子的に返す。

        確認とdupが別々だと、その隙間でrevokeされたCapabilityのFDを
        複製できてしまう。世代も同時に確定させ、読取り終了時の再検証へ渡す。
        """
        with self._lock:
            self._ensure_open()
            slot = self._slots.get(capability_id)
            if slot is None or slot.state is not _SlotState.ACTIVE:
                raise CapabilityRevoked(f"capability is not active: {capability_id}")
            # `assert` を使わない。`-O` で消えるため、消えた状態では
            # `None` のFDを`dup`しようとして別の失敗の仕方をする。
            # ACTIVEなら両方揃っているはずだが、揃っていないなら貸さない。
            if slot.capability is None or slot.root_fd is None:
                raise CapabilityRevoked(
                    f"capability slot is inconsistent, refusing to lease: {capability_id}"
                )
            return RootGrant(
                capability=slot.capability,
                generation=slot.generation,
                lease=WorkspaceRootLease(os.dup(slot.root_fd)),
            )

    def is_active(self, capability_id: str, generation: int) -> bool:
        """読取り終了時の再検証。同一世代のまま有効かを問う。

        世代まで見るのは、失効と再発行が同一IDで起きる形を将来許した場合に
        「別物になっている」ことを検出するためである。現在はtombstoneに
        より再発行できないが、判定の根拠をIDだけに置かない。
        """
        with self._lock:
            if self._closed:
                return False
            slot = self._slots.get(capability_id)
            return (
                slot is not None
                and slot.state is _SlotState.ACTIVE
                and slot.generation == generation
            )

    def revoke(self, capability_id: str) -> None:
        """1件を失効させる。以後 `acquire_root` は拒否する。

        記録は消さない。消すとIDが再利用可能になる。

        既に貸し出されたLeaseは`dup`済みなので生き残る。読取りの途中で
        Handleが消える形にはしない。ただし**その読取りの結果は返らない**。
        認可済みとして返してよいかは `is_active()` が別途判定する。
        """
        with self._lock:
            slot = self._slots.get(capability_id)
            if slot is None:
                return
            fd = slot.root_fd
            slot.state = _SlotState.REVOKED
            slot.capability = None
            slot.root_fd = None
        if fd is not None:
            os.close(fd)

    def close(self) -> None:
        """全Capabilityを失効させ、正本FDを閉じる。冪等。

        `OSError` を握り潰さない。閉じ済みかどうかは`_closed`で判断する。
        例外を潰すと、FD管理の不具合が静かに埋もれる。
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            fds = [slot.root_fd for slot in self._slots.values() if slot.root_fd is not None]
            for slot in self._slots.values():
                slot.state = _SlotState.REVOKED
                slot.capability = None
                slot.root_fd = None
        for fd in fds:
            os.close(fd)


class SafeInputReader:
    """Brokerを介して読む。

    Capabilityの辞書を複製して持たない。複製すると、Brokerがcloseや
    revokeをしても古い複製が生き続け、失効したCapabilityで読めてしまう。
    有効性の判断はBrokerが唯一の権威である。
    """

    def __init__(self, broker: CapabilityBroker) -> None:
        self._broker = broker

    def enumerate(self, capability_id: str, relative_directory: str) -> list[str] | ReadDenial:
        """Bounded, deterministic enumeration on the capability's directory handle.

        Any denied entry invalidates the complete candidate snapshot. Each file must
        still pass open_read and classification before it becomes Context input.
        """
        capability = self._broker.lookup(capability_id)
        if capability is None:
            return ReadDenial(
                reason=DenialReason.PATH_OUTSIDE_CAPABILITY,
                capability_id=capability_id,
                requested_path=relative_directory,
                detail="unknown or revoked capability",
            )
        parts = validate_relative_path(relative_directory)
        if parts is None or not capability.scope.permits(relative_directory):
            return self._deny(
                DenialReason.PATH_OUTSIDE_CAPABILITY,
                capability,
                relative_directory,
                "directory is outside the capability scope",
            )
        try:
            grant = self._broker.acquire_root(capability_id)
        except CapabilityRevoked:
            return self._deny(
                DenialReason.PATH_OUTSIDE_CAPABILITY,
                capability,
                relative_directory,
                "capability root is unavailable",
            )
        deadline = time.monotonic() + _MAX_READ_SECONDS
        candidates: list[str] = []
        total_entries = 0
        total_bytes = 0

        def walk(directory: str, depth: int) -> DenialReason | None:
            nonlocal total_entries, total_bytes
            if depth > _MAX_DEPTH or time.monotonic() > deadline:
                return DenialReason.PATH_OUTSIDE_CAPABILITY
            opened = self._open_beneath(
                grant.lease.fd, tuple(directory.split("/")), capability.mount_attestation.mount_id
            )
            if isinstance(opened, DenialReason):
                return opened
            try:
                before = os.fstat(opened)
                if not stat.S_ISDIR(before.st_mode):
                    return DenialReason.SPECIAL_FILE_DENIED
                if read_fd_mount_id(opened) != capability.mount_attestation.mount_id:
                    return DenialReason.MOUNT_CROSSING_DENIED
                names: list[str] = []
                with os.scandir(opened) as entries:
                    for entry in entries:
                        total_entries += 1
                        if total_entries > 4096 or time.monotonic() > deadline:
                            return DenialReason.PATH_OUTSIDE_CAPABILITY
                        names.append(entry.name)
                for name in sorted(names):
                    path = directory + "/" + name
                    if validate_relative_path(path) is None or not capability.scope.permits(path):
                        return DenialReason.PATH_OUTSIDE_CAPABILITY
                    info = os.stat(name, dir_fd=opened, follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        return DenialReason.SYMLINK_DENIED
                    if info.st_dev != before.st_dev:
                        return DenialReason.MOUNT_CROSSING_DENIED
                    if stat.S_ISDIR(info.st_mode):
                        denial = walk(path, depth + 1)
                        if denial is not None:
                            return denial
                    elif stat.S_ISREG(info.st_mode):
                        # 他のリンク名が許可Scope内にあると証明できないFileは候補にしない。
                        if info.st_nlink != 1:
                            return DenialReason.PATH_OUTSIDE_CAPABILITY
                        total_bytes += info.st_size
                        if info.st_size > _MAX_BYTES or total_bytes > 64 * 1024 * 1024:
                            return DenialReason.PATH_OUTSIDE_CAPABILITY
                        candidates.append(path)
                    else:
                        return DenialReason.SPECIAL_FILE_DENIED
                after = os.fstat(opened)
                if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    return DenialReason.PATH_OUTSIDE_CAPABILITY
                # Re-resolve from the pinned root: the open directory may have been renamed.
                current = self._open_beneath(
                    grant.lease.fd,
                    tuple(directory.split("/")),
                    capability.mount_attestation.mount_id,
                )
                if isinstance(current, DenialReason):
                    return current
                try:
                    identity = os.fstat(current)
                    if (identity.st_dev, identity.st_ino) != (before.st_dev, before.st_ino):
                        return DenialReason.PATH_OUTSIDE_CAPABILITY
                finally:
                    os.close(current)
                return None
            finally:
                os.close(opened)

        denial: DenialReason | None
        try:
            if not capability.mount_attestation.identifies_same_directory(os.fstat(grant.lease.fd)):
                denial = DenialReason.PATH_OUTSIDE_CAPABILITY
            else:
                denial = walk(relative_directory, len(parts))
            if not self._broker.is_active(capability_id, grant.generation):
                denial = DenialReason.PATH_OUTSIDE_CAPABILITY
            if time.monotonic() > deadline:
                denial = DenialReason.PATH_OUTSIDE_CAPABILITY
            if denial is not None:
                return self._deny(
                    denial,
                    capability,
                    relative_directory,
                    "candidate enumeration rejected; partial snapshot discarded",
                )
            return sorted(candidates)
        except (OSError, HarnessError):
            return self._deny(
                DenialReason.PATH_OUTSIDE_CAPABILITY,
                capability,
                relative_directory,
                "candidate enumeration could not be verified",
            )
        finally:
            grant.lease.release()

    # ------------------------------------------------------------------

    def open_read(
        self, capability_id: str, relative_path: str
    ) -> tuple[bytes, ReadEvidence] | ReadDenial:
        deadline = time.monotonic() + _MAX_READ_SECONDS
        capability = self._broker.lookup(capability_id)
        if capability is None:
            return ReadDenial(
                reason=DenialReason.PATH_OUTSIDE_CAPABILITY,
                capability_id=capability_id,
                requested_path=relative_path,
                detail="unknown or revoked capability",
            )

        # 1. Filesystemへ触れる前にPath文字列だけで弾く。
        segments = validate_relative_path(relative_path)
        if segments is None:
            return self._deny(
                DenialReason.PATH_OUTSIDE_CAPABILITY,
                capability,
                relative_path,
                "path is absolute, empty, contains '..', NUL, or a Windows path form",
            )
        if len(segments) > _MAX_DEPTH:
            return self._deny(
                DenialReason.PATH_OUTSIDE_CAPABILITY,
                capability,
                relative_path,
                f"path depth {len(segments)} exceeds {_MAX_DEPTH}",
            )
        if not capability.scope.permits(relative_path):
            return self._deny(
                DenialReason.PATH_OUTSIDE_CAPABILITY,
                capability,
                relative_path,
                "path is outside the capability scope",
            )

        # 2. BrokerからRoot HandleのLeaseを取る。
        #
        # Pathで開き直さない。開き直すと、発行後にWorkspaceをrenameして
        # 同じPathへ別のDirectoryを置く差し替えが成立する。
        #
        # Leaseは`dup`済みFDであり、読取りが終わるまで元のFile Descriptionを
        # 生かす。Brokerがこの最中にcloseやrevokeをしても、番号が別Workspaceへ
        # 再利用されることはない。有効性の確認とdupはBrokerのlock内で行われる。
        try:
            grant = self._broker.acquire_root(capability_id)
        except CapabilityRevoked as exc:
            return self._deny(
                DenialReason.PATH_OUTSIDE_CAPABILITY,
                capability,
                relative_path,
                f"capability root handle is not available: {exc}",
            )

        try:
            result = self._read_with_lease(grant, relative_path, segments, deadline)
        finally:
            grant.lease.release()

        # 読取りの**完了**と、結果を認可済みとして返してよいかは別である。
        # Lease取得後にrevokeされていれば、Bytesは読めていても返してはならない。
        # Leaseを生かすのは安全にcloseするためであって、認可の延長ではない。
        if isinstance(result, ReadDenial):
            return result
        if not self._broker.is_active(capability_id, grant.generation):
            return self._deny(
                DenialReason.PATH_OUTSIDE_CAPABILITY,
                capability,
                relative_path,
                "capability was revoked while the read was in progress",
            )
        if time.monotonic() > deadline:
            return self._deny(
                DenialReason.PATH_OUTSIDE_CAPABILITY,
                capability,
                relative_path,
                "read time limit exceeded before returning evidence",
            )
        return result

    def _read_with_lease(
        self,
        grant: RootGrant,
        relative_path: str,
        segments: tuple[str, ...],
        deadline: float,
    ) -> tuple[bytes, ReadEvidence] | ReadDenial:
        capability = grant.capability
        root_fd = grant.lease.fd
        root_stat = os.fstat(root_fd)

        # Leaseが、発行時に検証した実体を指していることを確認する。
        if not capability.mount_attestation.identifies_same_directory(root_stat):
            return self._deny(
                DenialReason.PATH_OUTSIDE_CAPABILITY,
                capability,
                relative_path,
                "capability root handle no longer identifies the verified workspace",
            )

        opened = self._open_beneath(root_fd, segments, capability.mount_attestation.mount_id)
        if isinstance(opened, DenialReason):
            return self._deny(opened, capability, relative_path, self._reason_detail(opened))
        file_fd = opened

        try:
            before = os.fstat(file_fd)

            # 3. File Type。通常Fileだけを読む。
            if not stat.S_ISREG(before.st_mode):
                return self._deny(
                    DenialReason.SPECIAL_FILE_DENIED,
                    capability,
                    relative_path,
                    f"not a regular file (mode={stat.filemode(before.st_mode)})",
                )
            # openat2/O_NOFOLLOWはHardlinkの別名を識別しない。Kernelからは
            # その全Pathを列挙できず、Workspace外の別名が無いと証明できない。
            # 外を探索して推測せず、単一リンクの通常Fileだけを読む。
            if before.st_nlink != 1:
                return self._deny(
                    DenialReason.PATH_OUTSIDE_CAPABILITY,
                    capability,
                    relative_path,
                    "file has unverified hardlink aliases",
                )
            # 4. Mount越境。Root と同一Deviceであること。
            if before.st_dev != root_stat.st_dev:
                return self._deny(
                    DenialReason.MOUNT_CROSSING_DENIED,
                    capability,
                    relative_path,
                    f"st_dev {before.st_dev} differs from workspace root {root_stat.st_dev}",
                )
            if before.st_size > _MAX_BYTES:
                return self._deny(
                    DenialReason.PATH_OUTSIDE_CAPABILITY,
                    capability,
                    relative_path,
                    f"size {before.st_size} exceeds {_MAX_BYTES}",
                )

            try:
                data = self._read_all(file_fd, deadline=deadline)
            except _ReadLimitExceeded as exc:
                # statを通ったあとに伸びたFile。読みながらも数えている。
                return self._deny(
                    DenialReason.PATH_OUTSIDE_CAPABILITY, capability, relative_path, str(exc)
                )
            except OSError as exc:
                # 読取り中のI/O失敗を例外として外へ出さない。
                # `open_read` の契約は `tuple | ReadDenial` であり、
                # 例外で抜けると呼出側は拒否として扱えず、そのまま落ちる。
                # 内容やPathは載せない（不変条件#7）。
                return self._deny(
                    DenialReason.PATH_OUTSIDE_CAPABILITY,
                    capability,
                    relative_path,
                    f"read failed: {exc.strerror or type(exc).__name__}",
                )

            # 5. 読取り中にIdentityが変化していないこと。
            after = os.fstat(file_fd)
            # 読取り途中にScope外へ別名が作られた場合もBytesを返さない。
            if after.st_nlink != 1:
                return self._deny(
                    DenialReason.PATH_OUTSIDE_CAPABILITY,
                    capability,
                    relative_path,
                    "file link identity changed during read",
                )
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                return self._deny(
                    DenialReason.SPECIAL_FILE_DENIED,
                    capability,
                    relative_path,
                    "file identity changed during read",
                )
        finally:
            os.close(file_fd)

        identity = f"dev={before.st_dev}:ino={before.st_ino}:size={before.st_size}"
        evidence = ReadEvidence(
            capability_id=capability.capability_id,
            requested_path=relative_path,
            canonical_path="/".join(segments),
            content_hash=hash_bytes(data),
            size_bytes=len(data),
            file_identity=identity,
            # §1.16.2 の Mount ID。`st_dev` ではない。両者は別物であり、
            # `st_dev` を入れると Mount ID 変化の検出に使えない。
            mount_id=str(capability.mount_attestation.mount_id),
        )
        return data, evidence

    # ------------------------------------------------------------------

    def _open_beneath(
        self, root_fd: int, segments: tuple[str, ...], root_mount_id: int
    ) -> int | DenialReason:
        """Root配下だけを解決してFileを開く。"""
        relative = "/".join(segments)
        if is_available():
            try:
                return openat2(root_fd, relative, flags=_OPEN_FLAGS, resolve=_RESOLVE_STRICT)
            except Openat2Unavailable:
                pass  # Fallbackへ落ちる
            except OSError as exc:
                return self._classify_open_error(exc)
        return self._walk_open(root_fd, segments, root_mount_id)

    def _walk_open(
        self, root_fd: int, segments: tuple[str, ...], root_mount_id: int
    ) -> int | DenialReason:
        """ADR-003のFallback。Componentごとに`O_NOFOLLOW`と検証を行う。

        `openat2`と違い検査とopenの間に隙間が残るため、Kernelが対応していれば
        そちらを使う。ここは利用不能環境での**安全側の縮退**である。

        Mount越境の判定に `st_dev` を使わない。同一Device上のbind mountは
        `st_dev` が一致するため通ってしまう。Workspace Root境界と同じく、
        各ComponentのFDの `mnt_id` をRootのAttestationと比較する。
        `RESOLVE_NO_XDEV` と同等の厳しさをFallbackでも保つ。
        """
        current = os.dup(root_fd)
        try:
            for index, segment in enumerate(segments):
                last = index == len(segments) - 1
                flags = _OPEN_FLAGS | os.O_NOFOLLOW
                if not last:
                    # O_DIRECTORY|O_NOFOLLOWは途中SymlinkをENOTDIRへ潰す。
                    # O_PATHでリンク自体のHandleを取り、追跡せず種別を判定する。
                    flags = os.O_PATH | os.O_NOFOLLOW
                try:
                    nxt = os.open(segment, flags, dir_fd=current)
                except OSError as exc:
                    return self._classify_open_error(exc)
                os.close(current)
                current = nxt
                if not last:
                    mode = os.fstat(current).st_mode
                    if stat.S_ISLNK(mode):
                        return DenialReason.SYMLINK_DENIED
                    if not stat.S_ISDIR(mode):
                        return DenialReason.SPECIAL_FILE_DENIED
                try:
                    if read_fd_mount_id(current) != root_mount_id:
                        return DenialReason.MOUNT_CROSSING_DENIED
                except HarnessError:
                    # `mnt_id` が読めない＝越境していないと確認できない。
                    return DenialReason.MOUNT_CROSSING_DENIED
            handle = current
            current = -1
            return handle
        finally:
            if current >= 0:
                os.close(current)

    @staticmethod
    def _classify_open_error(exc: OSError) -> DenialReason:
        import errno as _errno

        if exc.errno in (_errno.ELOOP, _errno.EMLINK):
            # O_NOFOLLOW / RESOLVE_NO_SYMLINKS でSymlinkに当たった
            return DenialReason.SYMLINK_DENIED
        if exc.errno == _errno.EXDEV:
            return DenialReason.MOUNT_CROSSING_DENIED
        if exc.errno in (_errno.ENOTDIR, _errno.EISDIR):
            return DenialReason.SPECIAL_FILE_DENIED
        if exc.errno in (_errno.ENXIO, _errno.ENODEV, _errno.EOPNOTSUPP):
            # UNIX domain socketの`open`はENXIO。Device Nodeの未接続もここへ来る。
            # 「開けなかった」ではなく「通常Fileではない」ことが理由である。
            return DenialReason.SPECIAL_FILE_DENIED
        return DenialReason.PATH_OUTSIDE_CAPABILITY

    @staticmethod
    def _reason_detail(reason: DenialReason) -> str:
        return {
            DenialReason.SYMLINK_DENIED: "path component is a symlink or magic link",
            DenialReason.MOUNT_CROSSING_DENIED: "path crosses a mount boundary",
            DenialReason.SPECIAL_FILE_DENIED: "path is a directory or special file",
            DenialReason.PATH_OUTSIDE_CAPABILITY: "path could not be resolved beneath root",
        }[reason]

    @staticmethod
    def _read_all(file_fd: int, *, deadline: float | None = None) -> bytes:
        """上限まで読む。超えたら**読みながら**止める。

        `st_size` の検査は open 直後の一点でしかない。その後に書き足されれば
        上限を超えたBytesが入ってくる。stat を信じて数えるのをやめない。
        """
        if deadline is None:
            deadline = time.monotonic() + _MAX_READ_SECONDS
        chunks: list[bytes] = []
        total = 0
        while True:
            if time.monotonic() > deadline:
                raise _ReadLimitExceeded("read time limit exceeded before read")
            chunk = os.read(file_fd, 1 << 20)
            # EOFも時間超過を免除しない。遅れて取得できたBytesは返さない。
            if time.monotonic() > deadline:
                raise _ReadLimitExceeded("read time limit exceeded after read")
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_BYTES:
                raise _ReadLimitExceeded(f"read exceeded {_MAX_BYTES} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _deny(
        reason: DenialReason,
        capability: IssuedCapability,
        relative_path: str,
        detail: str,
    ) -> ReadDenial:
        return ReadDenial(
            reason=reason,
            capability_id=capability.capability_id,
            requested_path=relative_path,
            canonical_candidate=relative_path,
            detail=detail,
        )
