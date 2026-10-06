"""Workspace Filesystem境界の統合試験（§0.1／§1.16.2、ADR-002、不変条件#12）。

受入Case `AT-WSL-BOUNDARY-001 / FOREIGN_FS`。

実Directoryを開いてDevice IDを照合するため統合試験に置く。ただし
Filesystem Type の判定は合成mountinfoで行う。WSL2上でNFSやCIFSを
mountしないと拒否を確認できない、という状態にしないためである。

## Case Adapter はここではない

本 File が持っていた `@pytest.mark.case` は
`tests/integration/sqlite/test_approval_plan_orchestrator.py` へ移した。
Case は Event 列まで要求するが、本 File は Ledger を観測しない。
Ledger を観測しない試験を Adapter にすると「State だけ一致した Case」になる。

本 File の試験は消していない。Domain／Repository の振る舞いは引き続き検証する。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.input_read import CapabilityScope, ReadDenial
from harness.infrastructure.filesystem.safe_reader import (
    CapabilityBroker,
    CapabilityRevoked,
    SafeInputReader,
)
from harness.infrastructure.filesystem.workspace_boundary import (
    FilesystemPolicy,
    read_fd_mount_id,
    verify_workspace_filesystem,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def policy() -> FilesystemPolicy:
    return FilesystemPolicy.load(REPO_ROOT)


def real_mount_id(path: Path) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return read_fd_mount_id(fd)
    finally:
        os.close(fd)


def mountinfo_for(path: Path, filesystem_type: str, *, mount_point: str = "/") -> str:
    """対象Pathを覆うMount Entryを1行だけ持つmountinfoを作る。

    major:minor と Mount ID は実際の値に合わせる。合わせないとMount Identity
    照合で落ちてしまい、Filesystem Type の判定を試験できない。
    """
    st = os.stat(path)
    major, minor = os.major(st.st_dev), os.minor(st.st_dev)
    mount_id = real_mount_id(path)
    return (
        f"{mount_id} 1 {major}:{minor} / {mount_point} rw,relatime - {filesystem_type} /dev/x rw\n"
    )


# ---------------------------------------------------------------------------
# Allowlist 判定
# ---------------------------------------------------------------------------


def test_ext4_workspace_is_accepted(tmp_path: Path, policy: FilesystemPolicy) -> None:
    attestation = verify_workspace_filesystem(
        tmp_path, policy, mountinfo_text=mountinfo_for(tmp_path, "ext4")
    )
    assert attestation.filesystem_type == "ext4"
    assert attestation.workspace_root == str(tmp_path)


@pytest.mark.parametrize(
    "filesystem_type",
    ["drvfs", "9p", "cifs", "nfs4", "virtiofs", "vboxsf", "fuseblk", "overlay", "squashfs"],
)
def test_foreign_filesystem_is_denied(
    tmp_path: Path, policy: FilesystemPolicy, filesystem_type: str
) -> None:
    """AT-WSL-BOUNDARY-001 / FOREIGN_FS。

    初版は `st_dev` の一致しか見ておらず、Workspace自体がどのFilesystemに
    在るかを判定していなかった。`/mnt/c` 上のWorkspaceでも配下Fileの
    `st_dev` はRootと一致するため、そのまま通っていた。
    """
    with pytest.raises(HarnessError) as error:
        verify_workspace_filesystem(
            tmp_path, policy, mountinfo_text=mountinfo_for(tmp_path, filesystem_type)
        )
    assert error.value.code is ErrorCode.WORKSPACE_ON_FOREIGN_FS_DENIED
    assert filesystem_type in str(error.value)


def test_unknown_filesystem_type_is_denied(tmp_path: Path, policy: FilesystemPolicy) -> None:
    """`on_unknown_filesystem_type: REJECT`。

    未知の種別は「新しいLinux FS」かもしれないが「未知の共有FS」かもしれない。
    区別できない以上、通してはならない。
    """
    with pytest.raises(HarnessError) as error:
        verify_workspace_filesystem(
            tmp_path, policy, mountinfo_text=mountinfo_for(tmp_path, "somefs2099")
        )
    assert error.value.code is ErrorCode.WORKSPACE_ON_FOREIGN_FS_DENIED
    assert "not on the allowlist" in str(error.value)


def test_tmpfs_requires_the_explicit_test_flag(tmp_path: Path, policy: FilesystemPolicy) -> None:
    """tmpfsは試験用途に限る。既定では恒久Workspaceとして認めない。"""
    text = mountinfo_for(tmp_path, "tmpfs")
    with pytest.raises(HarnessError):
        verify_workspace_filesystem(tmp_path, policy, mountinfo_text=text)
    accepted = verify_workspace_filesystem(
        tmp_path, policy, mountinfo_text=text, allow_test_filesystems=True
    )
    assert accepted.filesystem_type == "tmpfs"


# ---------------------------------------------------------------------------
# Path文字列は補助にすぎない
# ---------------------------------------------------------------------------


def test_denied_prefix_is_rejected_even_before_reading_mountinfo(
    policy: FilesystemPolicy,
) -> None:
    """`/mnt` 配下は早期に落とす。ここは補助判定である。"""
    with pytest.raises(HarnessError) as error:
        verify_workspace_filesystem(
            Path("/mnt/c/Users/x/ws"), policy, mountinfo_text="(read should not happen)"
        )
    assert error.value.code is ErrorCode.WORKSPACE_ON_FOREIGN_FS_DENIED
    assert "/mnt" in str(error.value)


def test_prefix_check_respects_segment_boundaries(tmp_path: Path, policy: FilesystemPolicy) -> None:
    """`/mnt` が `/mnthome` を巻き込まないこと。"""
    root = tmp_path / "mnthome"
    root.mkdir()
    attestation = verify_workspace_filesystem(
        root, policy, mountinfo_text=mountinfo_for(root, "ext4")
    )
    assert attestation.filesystem_type == "ext4"


def test_string_prefix_alone_does_not_grant_permission(
    tmp_path: Path, policy: FilesystemPolicy
) -> None:
    """§1.16.2「文字列Prefixだけで判定しない」。

    `/mnt` 配下でない普通のPathでも、Filesystem Type が drvfs なら拒否する。
    判定の根拠はType であってPath文字列ではない。
    """
    with pytest.raises(HarnessError, match="drvfs"):
        verify_workspace_filesystem(
            tmp_path, policy, mountinfo_text=mountinfo_for(tmp_path, "drvfs")
        )


# ---------------------------------------------------------------------------
# Mount Identity の併用
# ---------------------------------------------------------------------------


def test_device_identity_mismatch_is_denied(tmp_path: Path, policy: FilesystemPolicy) -> None:
    """mountinfoから引いたMountと、実際に開いたDirectoryの実体が違う場合。

    Mount EntryはPath**文字列**を根拠に選んでいる。実体と突き合わせて
    初めて「いま検証したMountが、いま開いているMountである」と言える。
    """
    bogus = "28 1 99:99 / / rw,relatime - ext4 /dev/x rw\n"
    with pytest.raises(HarnessError) as error:
        verify_workspace_filesystem(tmp_path, policy, mountinfo_text=bogus)
    assert error.value.code is ErrorCode.WORKSPACE_ON_FOREIGN_FS_DENIED
    assert "mount identity mismatch" in str(error.value)


def test_mount_id_must_match_the_opened_fd(tmp_path: Path, policy: FilesystemPolicy) -> None:
    """major:minor 一致だけでは足りない。

    同一Device上のbind mountは major:minor が一致するため、Path文字列から
    別のMount Entryを引いても通ってしまう。`/proc/self/fdinfo/<fd>` の
    `mnt_id` は「このFDが実際に属するMount」であり、完全一致を要求する。
    """
    st = os.stat(tmp_path)
    major, minor = os.major(st.st_dev), os.minor(st.st_dev)
    wrong_mount_id = real_mount_id(tmp_path) + 1000
    text = f"{wrong_mount_id} 1 {major}:{minor} / / rw,relatime - ext4 /dev/x rw\n"
    with pytest.raises(HarnessError) as error:
        verify_workspace_filesystem(tmp_path, policy, mountinfo_text=text)
    assert error.value.code is ErrorCode.WORKSPACE_ON_FOREIGN_FS_DENIED
    assert "mount id mismatch" in str(error.value)


def test_attested_mount_id_matches_the_real_fd(tmp_path: Path, policy: FilesystemPolicy) -> None:
    attestation = verify_workspace_filesystem(
        tmp_path, policy, mountinfo_text=mountinfo_for(tmp_path, "ext4")
    )
    assert attestation.mount_id == real_mount_id(tmp_path)


# ---------------------------------------------------------------------------
# Workspace Root のSymlink
# ---------------------------------------------------------------------------


def test_symlinked_workspace_root_is_denied(tmp_path: Path, policy: FilesystemPolicy) -> None:
    """Workspace Root自体がSymlinkなら拒否する。

    初版は `Path.resolve()` してから `O_NOFOLLOW` で開いていた。
    `resolve()` が先にSymlinkを解決するため、`O_NOFOLLOW` は何も拒否しない。
    Symlink既定拒否という仕様と不一致だった。
    """
    target = tmp_path / "target"
    (target / "docs").mkdir(parents=True)
    link = tmp_path / "workspace"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(HarnessError) as error:
        verify_workspace_filesystem(link, policy, mountinfo_text=mountinfo_for(target, "ext4"))
    assert error.value.code is ErrorCode.SYMLINK_DENIED
    assert "symlink" in str(error.value)


def test_symlinked_intermediate_component_is_denied(
    tmp_path: Path, policy: FilesystemPolicy
) -> None:
    """途中Componentがsymlinkの場合も拒否する。"""
    real = tmp_path / "real"
    (real / "ws").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(HarnessError) as error:
        verify_workspace_filesystem(
            link / "ws", policy, mountinfo_text=mountinfo_for(real / "ws", "ext4")
        )
    assert error.value.code is ErrorCode.SYMLINK_DENIED


def test_capability_cannot_be_issued_on_a_symlinked_root(
    tmp_path: Path, policy: FilesystemPolicy
) -> None:
    target = tmp_path / "target"
    (target / "docs").mkdir(parents=True)
    link = tmp_path / "workspace"
    link.symlink_to(target, target_is_directory=True)

    with CapabilityBroker(policy, allow_test_filesystems=True) as broker:
        with pytest.raises(HarnessError) as error:
            broker.issue("cap-symlink", link, CapabilityScope(("docs",)))
    assert error.value.code is ErrorCode.SYMLINK_DENIED


def test_plain_directory_root_is_still_accepted(tmp_path: Path, policy: FilesystemPolicy) -> None:
    """Symlink拒否が普通のDirectoryを巻き込んでいないこと。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    attestation = verify_workspace_filesystem(
        plain, policy, mountinfo_text=mountinfo_for(plain, "ext4")
    )
    assert attestation.workspace_root == str(plain)


# ---------------------------------------------------------------------------
# Fail-Closed
# ---------------------------------------------------------------------------


def test_unparsable_mountinfo_is_denied(tmp_path: Path, policy: FilesystemPolicy) -> None:
    with pytest.raises(HarnessError, match="malformed"):
        verify_workspace_filesystem(tmp_path, policy, mountinfo_text="garbage line\n")


def test_no_covering_mount_is_denied(tmp_path: Path, policy: FilesystemPolicy) -> None:
    """`on_mount_not_found: REJECT`。"""
    text = "23 28 0:22 / /proc rw - proc proc rw\n"
    with pytest.raises(HarnessError, match="no mount entry"):
        verify_workspace_filesystem(tmp_path, policy, mountinfo_text=text)


def test_missing_workspace_root_is_denied(tmp_path: Path, policy: FilesystemPolicy) -> None:
    with pytest.raises(HarnessError, match="not an openable directory"):
        verify_workspace_filesystem(tmp_path / "does-not-exist", policy, mountinfo_text="")


# ---------------------------------------------------------------------------
# Capability発行時の強制（§0.1）
# ---------------------------------------------------------------------------


def test_capability_cannot_be_issued_on_a_foreign_filesystem(
    tmp_path: Path, policy: FilesystemPolicy
) -> None:
    """拒否とは**Capabilityを発行しないこと**である。

    発行されなかったCapabilityでは読取り自体が始まらない。
    `ReadDenial` を返す形にすると、呼出側が無視できてしまう。
    """
    with CapabilityBroker(policy) as broker, pytest.raises(HarnessError) as error:
        broker.issue(
            "cap-foreign",
            tmp_path,
            CapabilityScope(("docs",)),
            mountinfo_text=mountinfo_for(tmp_path, "9p"),
        )
    assert error.value.code is ErrorCode.WORKSPACE_ON_FOREIGN_FS_DENIED


def test_issued_capability_carries_the_mount_attestation(
    tmp_path: Path, policy: FilesystemPolicy
) -> None:
    """§1.16.2 が要求するMount項目をRuntime Attestationへ回せる形で持つ。"""
    with CapabilityBroker(policy) as broker:
        capability = broker.issue(
            "cap-ok",
            tmp_path,
            CapabilityScope(("docs",)),
            mountinfo_text=mountinfo_for(tmp_path, "ext4"),
        )
        attestation = capability.mount_attestation
    assert attestation.filesystem_type == "ext4"
    # Mount IDは合成mountinfoの値ではなく、実際に開いたFDのMountと
    # 一致した値である。一致しなければ発行自体が失敗する。
    assert attestation.mount_id == real_mount_id(tmp_path)
    assert attestation.parent_id == 1
    assert ":" in attestation.major_minor
    assert attestation.mount_point == "/"


# ---------------------------------------------------------------------------
# 発行後のWorkspace差し替え
# ---------------------------------------------------------------------------


def test_capability_reads_the_workspace_it_was_issued_for_after_a_swap(
    tmp_path: Path, policy: FilesystemPolicy
) -> None:
    """発行後にPathを差し替えても、読むのは発行時に検証したWorkspaceである。

    レビューで再現されたバイパス。Pathを保持して読取りのたびに開き直す方式だと
    次が成立していた。

        1. Workspace A に対してCapabilityを発行（検証は通る）
        2. Workspace A を rename
        3. 同じPathに Workspace B を作成
        4. Workspace B のFileが読める

    発行時のRoot Directory FDを保持し、そのFD基準で `openat` すれば、
    renameされても読むのは元の実体であり差し替えは成立しない。
    """
    original = tmp_path / "workspace"
    (original / "docs").mkdir(parents=True)
    (original / "docs" / "readme.md").write_bytes(b"original content\n")

    with CapabilityBroker(policy, allow_test_filesystems=True) as broker:
        broker.issue("cap-swap", original, CapabilityScope(("docs",)))
        reader = SafeInputReader(broker)

        # 発行時の中身が読める
        first = reader.open_read("cap-swap", "docs/readme.md")
        assert not isinstance(first, ReadDenial), first
        assert first[0] == b"original content\n"

        # --- 差し替え ---
        original.rename(tmp_path / "workspace-moved")
        replacement = tmp_path / "workspace"
        (replacement / "docs").mkdir(parents=True)
        (replacement / "docs" / "readme.md").write_bytes(b"replacement secret\n")

        second = reader.open_read("cap-swap", "docs/readme.md")
        assert not isinstance(second, ReadDenial), second
        assert second[0] == b"original content\n", (
            "差し替えたWorkspaceの内容を読んでいる。"
            "Capabilityが発行時のRoot実体へ束縛されていない。"
        )
        assert b"replacement" not in second[0]


def test_capability_root_identity_is_bound_at_issue_time(
    tmp_path: Path, policy: FilesystemPolicy
) -> None:
    """Attestationが発行時のRoot実体（dev/ino）を保持している。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    st = os.stat(workspace)
    with CapabilityBroker(policy, allow_test_filesystems=True) as broker:
        capability = broker.issue("cap-id", workspace, CapabilityScope(("docs",)))
        attestation = capability.mount_attestation
        assert attestation.root_dev == st.st_dev
        assert attestation.root_ino == st.st_ino
        with broker.acquire_root("cap-id").lease as lease:
            assert attestation.identifies_same_directory(os.fstat(lease.fd))


def test_broker_closes_the_root_handles(tmp_path: Path, policy: FilesystemPolicy) -> None:
    """FDを保持する以上、明示的な解放が要る。

    FD番号はBrokerのprivateなので、外からはLease越しにしか触れない。
    close後はLeaseを取れないことで解放を確認する。
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    broker = CapabilityBroker(policy, allow_test_filesystems=True)
    broker.issue("cap-close", workspace, CapabilityScope(("docs",)))
    with broker.acquire_root("cap-close").lease as lease:
        os.fstat(lease.fd)  # 保持中は使える
    broker.close()
    assert broker.lookup("cap-close") is None
    with pytest.raises(CapabilityRevoked):
        broker.acquire_root("cap-close")


def test_evidence_carries_the_mount_id_not_st_dev(tmp_path: Path, policy: FilesystemPolicy) -> None:
    """§1.16.2 の Mount ID。`st_dev` とは別物である。

    `st_dev` を入れると Mount ID 変化の検出に使えない。
    """
    workspace = tmp_path / "ws"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "docs" / "a.txt").write_bytes(b"x\n")
    with CapabilityBroker(policy, allow_test_filesystems=True) as broker:
        capability = broker.issue("cap-ev", workspace, CapabilityScope(("docs",)))
        reader = SafeInputReader(broker)
        result = reader.open_read("cap-ev", "docs/a.txt")
    assert not isinstance(result, ReadDenial), result
    _data, evidence = result
    assert evidence.mount_id == str(capability.mount_attestation.mount_id)
    assert evidence.mount_id != str(os.stat(workspace).st_dev)


def test_real_repository_workspace_passes_on_wsl2(policy: FilesystemPolicy) -> None:
    """実環境の実測。この試験が落ちる環境ではHarnessを動かしてはならない。

    合成mountinfoだけで固めると、実際の`/proc/self/mountinfo`が読めない、
    書式が違う、といった問題を検出できない。
    """
    attestation = verify_workspace_filesystem(REPO_ROOT, policy, allow_test_filesystems=True)
    assert attestation.filesystem_type in (
        policy.allowed_filesystem_types | policy.test_only_filesystem_types
    )
