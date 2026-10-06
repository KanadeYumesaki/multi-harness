"""Workspace Filesystem境界の検証（§0.1／§1.16.2、ADR-002、不変条件#12）。

`WORKSPACE_ON_FOREIGN_FS_DENIED`。受入Case `AT-WSL-BOUNDARY-001 / FOREIGN_FS`。

## `st_dev` 一致だけでは足りない

初版の`SafeInputReader`はWorkspace Rootと読取り対象の`st_dev`一致しか
見ていなかった。これは**Workspace内部でのMount越境**は防ぐが、
**Workspace自体が置かれているFilesystemの素性**は一切見ていない。
`/mnt/c` 上へWorkspaceを作れば、配下Fileの`st_dev`はRootと一致するため
そのまま通る。

## 判定は4段

1. 字句的な絶対Pathによる早期拒否（補助。`/mnt` 配下など明白な誤配置）
2. Root Directoryを**Componentごとに`O_NOFOLLOW`で開く**（Symlink拒否）
3. `/proc/self/mountinfo` の最長Prefix一致でMount Entryを特定し、
   Filesystem Type を Allowlist と照合（**主判定**）
4. 開いたFDの `/proc/self/fdinfo/<fd>` の `mnt_id` と、選んだMount Entryの
   `mount_id` を**完全一致**させる

### なぜ `Path.resolve()` を使わないか

`resolve()` はSymlinkを解決してしまう。その後で`O_NOFOLLOW`を付けても、
既に実体Pathへ変換済みなので何も拒否できない。Workspace Rootが
Symlinkでも発行が通っていた（レビュー指摘）。

字句正規化には `os.path.abspath` を使う。Filesystemへ触れずに `..` と `.` を
畳むだけで、Symlinkは解決しない。実体への到達は2段目のComponent walkが行う。

### なぜ major:minor だけでは足りないか

Mount Entryは**Path文字列**から選んでいる。同一Device上のbind mountは
major:minor が一致するため、別のMountでも通ってしまう。
`fdinfo` の `mnt_id` は「そのFDが実際に属するMount」であり、
これと突き合わせて初めてMount Entryの選択が正しいと言える。
"""

from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.mountinfo import MountEntry, find_mount_for_path, parse_mountinfo

__all__ = [
    "FilesystemPolicy",
    "WorkspaceMountAttestation",
    "open_and_verify_workspace",
    "read_fd_mount_id",
    "verify_workspace_filesystem",
]

_MOUNTINFO_PATH = "/proc/self/mountinfo"


def _require(mapping: dict[str, Any], key: str) -> Any:
    if key not in mapping:
        raise HarnessError(
            ErrorCode.RUNTIME_SPEC_MISMATCH,
            f"filesystem-policy.yaml is missing {key}",
        )
    return mapping[key]


@dataclass(frozen=True, slots=True)
class FilesystemPolicy:
    """`filesystem-policy.yaml` を写した境界条件。既定値を持たない。"""

    policy_version: int
    allowed_filesystem_types: frozenset[str]
    test_only_filesystem_types: frozenset[str]
    denied_filesystem_types: frozenset[str]
    denied_mount_point_prefixes: tuple[str, ...]

    @classmethod
    def load(cls, repo_root: Path, registries: Path | None = None) -> FilesystemPolicy:
        directory = registries or (repo_root / "design-source" / "registries")
        document: dict[str, Any] = yaml.safe_load(
            (directory / "filesystem-policy.yaml").read_text(encoding="utf-8")
        )
        allowed = frozenset(str(x) for x in _require(document, "allowed_filesystem_types"))
        denied = frozenset(str(x) for x in _require(document, "denied_filesystem_types"))
        overlap = allowed & denied
        if overlap:
            # 両方に載っていると、どちらを優先するかで挙動が変わる。
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                f"filesystem types appear in both allow and deny lists: {sorted(overlap)}",
            )
        return cls(
            policy_version=int(_require(document, "filesystem_policy_version")),
            allowed_filesystem_types=allowed,
            test_only_filesystem_types=frozenset(
                str(x) for x in _require(document, "test_only_filesystem_types")
            ),
            denied_filesystem_types=denied,
            denied_mount_point_prefixes=tuple(
                str(x) for x in _require(document, "denied_mount_point_prefixes")
            ),
        )

    def permitted_types(self, *, allow_test_filesystems: bool) -> frozenset[str]:
        if allow_test_filesystems:
            return self.allowed_filesystem_types | self.test_only_filesystem_types
        return self.allowed_filesystem_types


@dataclass(frozen=True, slots=True)
class WorkspaceMountAttestation:
    """Runtime Attestationへ記録する実測値（§1.16.2）。人手で入力しない。

    `root_dev` / `root_ino` は**検証したその瞬間のRoot Directory実体**である。
    Capabilityはこれに束縛される。Pathは同じでも実体が入れ替われば別物であり、
    発行時に検証したWorkspaceではない。

    `mount_id` は `/proc/self/fdinfo` から読んだ、開いたFDが実際に属するMountの
    IDである。mountinfoから選んだEntryのIDと一致することを確認済みの値。
    """

    workspace_root: str
    mount_id: int
    parent_id: int
    major_minor: str
    root: str
    mount_point: str
    filesystem_type: str
    mount_options: str
    mount_source: str
    root_dev: int
    root_ino: int

    def identifies_same_directory(self, st: os.stat_result) -> bool:
        return st.st_dev == self.root_dev and st.st_ino == self.root_ino


def _deny(detail: str) -> HarnessError:
    return HarnessError(ErrorCode.WORKSPACE_ON_FOREIGN_FS_DENIED, detail)


def _deny_symlink(detail: str) -> HarnessError:
    return HarnessError(ErrorCode.SYMLINK_DENIED, detail)


def _is_under(prefix: str, path: str) -> bool:
    """Segment境界で判定する。`/mnt` が `/mnthome` を覆わないようにする。"""
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


# ---------------------------------------------------------------------------
# Mount ID
# ---------------------------------------------------------------------------


def read_fd_mount_id(fd: int) -> int:
    """`/proc/self/fdinfo/<fd>` の `mnt_id` を読む。

    「このFDが実際に属するMount」を返す。Path文字列から引いたMount Entryが
    正しいかを、これと突き合わせて確認する。

    読めない場合はFail-Closedで停止する。確認できないことを
    「確認できた」として扱わない。
    """
    try:
        text = Path(f"/proc/self/fdinfo/{fd}").read_text(encoding="utf-8")
    except OSError as exc:
        raise _deny(f"cannot read /proc/self/fdinfo/{fd}: {exc.strerror}") from None
    for line in text.splitlines():
        if line.startswith("mnt_id:"):
            return int(line.split(":", 1)[1].strip())
    raise _deny(f"/proc/self/fdinfo/{fd} has no mnt_id field")


# ---------------------------------------------------------------------------
# Symlinkを追わないRoot open
# ---------------------------------------------------------------------------


def _component_is_symlink(parent_fd: int, component: str) -> bool:
    """openが失敗した理由がsymlinkかを実体で判定する。

    `lstat` はsymlink自体を見る。ここで真なら、たとえKernelが
    ENOTDIR を返していてもSymlink拒否として報告する。
    """
    try:
        st = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode)


def _open_workspace_root_nofollow(absolute_path: str) -> int:
    """絶対PathをComponentごとに`O_NOFOLLOW`で辿ってDirectory FDを返す。

    `Path.resolve()` を使わない。`resolve()` はSymlinkを解決するため、
    その後に`O_NOFOLLOW`を付けても何も拒否できない。
    実際、Workspace RootがSymlinkでも発行が通っていた。

    中間Componentと最終Componentの両方でSymlinkを拒否する。
    """
    if not absolute_path.startswith("/"):
        raise _deny(f"workspace root must be an absolute path: {absolute_path!r}")

    components = [part for part in absolute_path.split("/") if part]
    if any(part == ".." for part in components):
        # 字句的な `..` は abspath が畳んでいるはずだが、残っていれば拒否する。
        raise _deny("workspace root path contains '..' after normalization")

    # `/` 自体はSymlinkになり得ない。
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, component in enumerate(components):
            try:
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=fd,
                )
            except OSError as exc:
                partial = "/" + "/".join(components[: index + 1])
                # **errnoで分類しない。**
                #
                # `O_NOFOLLOW` 単体ならsymlinkは ELOOP を返すが、
                # `O_DIRECTORY` と併用するとLinuxは ENOTDIR を返す
                # （symlink自体はDirectoryではないため）。errnoの順序に
                # 依存すると、拒否理由がKernel実装の都合で変わる。
                # 実体をlstatして分類する。
                if _component_is_symlink(fd, component):
                    raise _deny_symlink(
                        f"workspace root path component is a symlink: {partial}"
                    ) from None
                if exc.errno == errno.ENOTDIR:
                    raise _deny(
                        f"workspace root path component is not a directory: {partial}"
                    ) from None
                raise _deny(
                    f"workspace root is not an openable directory at {partial}: {exc.strerror}"
                ) from None
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


# ---------------------------------------------------------------------------
# 検証
# ---------------------------------------------------------------------------


def open_and_verify_workspace(
    workspace_root: Path,
    policy: FilesystemPolicy,
    *,
    mountinfo_text: str | None = None,
    allow_test_filesystems: bool = False,
) -> tuple[int, WorkspaceMountAttestation]:
    """Root Directory FDを開き、**そのFDを基準に**境界を検証して返す。

    FDを開いてから検証し、開いたままのFDを返すのが要点である。
    「検証する」と「読取りの基準にする」が別々のopenだと、その隙間で
    Directoryを差し替えられる。同じFDを使えば隙間が無い。

    **呼出側がFDのcloseに責任を持つ。** 通常は`CapabilityBroker`が保持する。
    """
    # 字句正規化のみ。Symlinkは解決しない（`resolve()` を使わない理由は
    # Module docstring 参照）。
    absolute = os.path.abspath(workspace_root)

    # --- 1. Path文字列による早期拒否（補助） ---
    for prefix in policy.denied_mount_point_prefixes:
        if _is_under(prefix, absolute):
            raise _deny(f"workspace root is under {prefix} (denied mount point prefix)")

    # --- 2. ComponentごとにO_NOFOLLOWで開く ---
    root_fd = _open_workspace_root_nofollow(absolute)
    try:
        attestation = _verify_open_workspace(
            root_fd,
            absolute,
            policy,
            mountinfo_text=mountinfo_text,
            allow_test_filesystems=allow_test_filesystems,
        )
    except BaseException:
        os.close(root_fd)
        raise
    return root_fd, attestation


def verify_workspace_filesystem(
    workspace_root: Path,
    policy: FilesystemPolicy,
    *,
    mountinfo_text: str | None = None,
    allow_test_filesystems: bool = False,
) -> WorkspaceMountAttestation:
    """境界を検証し、FDは閉じてAttestationだけ返す。

    起動時点検のように、以後の読取りへFDを引き継がない用途で使う。
    """
    root_fd, attestation = open_and_verify_workspace(
        workspace_root,
        policy,
        mountinfo_text=mountinfo_text,
        allow_test_filesystems=allow_test_filesystems,
    )
    os.close(root_fd)
    return attestation


def _verify_open_workspace(
    root_fd: int,
    absolute: str,
    policy: FilesystemPolicy,
    *,
    mountinfo_text: str | None,
    allow_test_filesystems: bool,
) -> WorkspaceMountAttestation:
    root_stat = os.fstat(root_fd)

    # --- 3. Mount Entryを特定してFilesystem Typeを照合（主判定） ---
    if mountinfo_text is None:
        try:
            mountinfo_text = Path(_MOUNTINFO_PATH).read_text(encoding="utf-8")
        except OSError as exc:
            # `on_mountinfo_unreadable: REJECT`。読めない＝確認できない＝通さない。
            raise _deny(f"cannot read {_MOUNTINFO_PATH}: {exc.strerror}") from None

    try:
        entries = parse_mountinfo(mountinfo_text)
    except ValueError as exc:
        raise _deny(f"{_MOUNTINFO_PATH} is malformed: {exc}") from None

    entry = find_mount_for_path(entries, absolute)
    if entry is None:
        raise _deny(f"no mount entry covers the workspace root ({len(entries)} entries scanned)")

    permitted = policy.permitted_types(allow_test_filesystems=allow_test_filesystems)
    if entry.filesystem_type not in permitted:
        reason = (
            "explicitly denied"
            if entry.filesystem_type in policy.denied_filesystem_types
            else "not on the allowlist"
        )
        raise _deny(
            f"workspace is on filesystem type {entry.filesystem_type!r} "
            f"at mount point {entry.mount_point!r} ({reason}); "
            f"permitted: {sorted(permitted)}"
        )

    # --- 4. Mount Identity の照合 ---
    # Device IDだけでは足りない。同一Device上のbind mountは major:minor が
    # 一致するため、Path文字列から別のMount Entryを引いても通ってしまう。
    # fdinfo の mnt_id は「このFDが実際に属するMount」であり、これと
    # 完全一致して初めてEntryの選択が正しいと言える。
    actual_device = (os.major(root_stat.st_dev), os.minor(root_stat.st_dev))
    if actual_device != entry.device_id:
        raise _deny(
            f"mount identity mismatch: mountinfo says {entry.major}:{entry.minor} "
            f"but the opened workspace root is on {actual_device[0]}:{actual_device[1]}"
        )
    actual_mount_id = read_fd_mount_id(root_fd)
    if actual_mount_id != entry.mount_id:
        raise _deny(
            f"mount id mismatch: the opened workspace root belongs to mount "
            f"{actual_mount_id} but the selected mountinfo entry is {entry.mount_id}"
        )

    return _attestation(absolute, entry, root_stat)


def _attestation(
    workspace_root: str, entry: MountEntry, root_stat: os.stat_result
) -> WorkspaceMountAttestation:
    return WorkspaceMountAttestation(
        workspace_root=workspace_root,
        mount_id=entry.mount_id,
        parent_id=entry.parent_id,
        major_minor=f"{entry.major}:{entry.minor}",
        root=entry.root,
        mount_point=entry.mount_point,
        filesystem_type=entry.filesystem_type,
        mount_options=entry.mount_options,
        mount_source=entry.mount_source,
        root_dev=root_stat.st_dev,
        root_ino=root_stat.st_ino,
    )
