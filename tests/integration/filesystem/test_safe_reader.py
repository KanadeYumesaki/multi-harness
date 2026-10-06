"""§1.16.2 Linux Safe Read の統合試験。

実体（Symlink・FIFO・Device・Directory）を作って拒否を確認する。
MVP0-A Scope の次のCaseに対応する。

* `AT-PATH-001/LINUX_ESCAPE`            → `PATH_OUTSIDE_CAPABILITY`
* `AT-INPUT-PATH-001/SYMLINK`           → `SYMLINK_DENIED`
* `AT-INPUT-PATH-001/SPECIAL_FILE`      → `SPECIAL_FILE_DENIED`
* `AT-INPUT-PATH-001/MOUNT_CROSSING`    → `MOUNT_CROSSING_DENIED`

## Case Adapter はここではない

上記 Case の `@pytest.mark.case` は
`test_input_read_orchestrator.py`（MOUNT_CROSSING は `test_mount_crossing.py`）
へ移した。Case は `INPUT_READ_STARTED → INPUT_READ_DENIED` の Event 列まで
要求するが、本 File は Reader だけを呼ぶので Ledger を観測できない。
Ledger を観測しない試験を Adapter にすると「State だけ一致した Case」になる。

本 File の試験は消していない。Reader が判定を返すこと自体は引き続き検証する。
"""

from __future__ import annotations

import os
import signal
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.domain.errors import ErrorCode
from harness.domain.hashing import hash_bytes
from harness.domain.input_read import CapabilityScope, ReadDecision, ReadDenial
from harness.infrastructure.filesystem.openat2 import is_available
from harness.infrastructure.filesystem.safe_reader import (
    CapabilityBroker,
    SafeInputReader,
)
from harness.infrastructure.filesystem.workspace_boundary import FilesystemPolicy
from harness.ports.safe_input_reader import ReadEvidence

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
CAPABILITY_ID = "cap-1"
PAYLOAD = b"readable content\n"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "readme.md").write_bytes(PAYLOAD)
    (root / "secret").mkdir()
    (root / "secret" / "keys.txt").write_bytes(b"do not read\n")
    return root


@pytest.fixture
def broker() -> Iterator[CapabilityBroker]:
    # Capabilityは必ずBrokerを通す。Workspace Filesystem境界の検証（§0.1）を
    # 経ていないCapabilityは構築できない。Root Directory FDを保持するため
    # 明示的にcloseする。tmpfs上のCI環境でも動くよう試験用FSを許可する。
    with CapabilityBroker(FilesystemPolicy.load(REPO_ROOT), allow_test_filesystems=True) as issued:
        yield issued


@pytest.fixture
def reader(workspace: Path, broker: CapabilityBroker) -> SafeInputReader:
    broker.issue(CAPABILITY_ID, workspace, CapabilityScope(("docs",)))
    # ReaderはBrokerを参照する。Capability辞書を複製すると、closeやrevokeの
    # あとも古い複製で読めてしまう。
    return SafeInputReader(broker)


def _denial(result: object) -> ReadDenial:
    assert isinstance(result, ReadDenial), f"expected denial, got {type(result)}"
    return result


# --------------------------------------------------------------------------
# 正常系
# --------------------------------------------------------------------------


def test_reads_a_regular_file_inside_scope(reader: SafeInputReader) -> None:
    result = reader.open_read(CAPABILITY_ID, "docs/readme.md")
    assert not isinstance(result, ReadDenial), result
    data, evidence = result
    assert data == PAYLOAD
    assert isinstance(evidence, ReadEvidence)
    assert evidence.decision is ReadDecision.ALLOWED
    assert evidence.content_hash == hash_bytes(PAYLOAD)
    assert evidence.size_bytes == len(PAYLOAD)
    assert evidence.canonical_path == "docs/readme.md"
    assert evidence.file_identity.startswith("dev=")


def test_evidence_binds_file_identity(reader: SafeInputReader) -> None:
    """§1.16.2「読取りEvidence HashとCapability Set Hashを後続Planへ束縛する」。"""
    result = reader.open_read(CAPABILITY_ID, "docs/readme.md")
    assert not isinstance(result, ReadDenial)
    _, evidence = result
    assert evidence.capability_id == CAPABILITY_ID
    assert evidence.mount_id


# --------------------------------------------------------------------------
# AT-PATH-001/LINUX_ESCAPE
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["../../etc/passwd", "/etc/passwd", "docs/../secret/keys.txt", ""],
)
def test_path_escape_is_denied(reader: SafeInputReader, path: str) -> None:
    denial = _denial(reader.open_read(CAPABILITY_ID, path))
    assert denial.error_code is ErrorCode.PATH_OUTSIDE_CAPABILITY
    assert denial.decision is ReadDecision.DENIED


def test_path_outside_scope_is_denied(reader: SafeInputReader) -> None:
    """Workspace内でもCapability Scope外は拒否する。"""
    denial = _denial(reader.open_read(CAPABILITY_ID, "secret/keys.txt"))
    assert denial.error_code is ErrorCode.PATH_OUTSIDE_CAPABILITY


def test_unknown_capability_is_denied(reader: SafeInputReader) -> None:
    denial = _denial(reader.open_read("cap-unknown", "docs/readme.md"))
    assert denial.error_code is ErrorCode.PATH_OUTSIDE_CAPABILITY


# --------------------------------------------------------------------------
# AT-INPUT-PATH-001/SYMLINK
# --------------------------------------------------------------------------


def test_symlink_to_outside_is_denied(workspace: Path, reader: SafeInputReader) -> None:
    """Scope内に置かれたSymlinkでWorkspace外を指す。"""
    (workspace / "docs" / "escape").symlink_to("/etc/passwd")
    denial = _denial(reader.open_read(CAPABILITY_ID, "docs/escape"))
    assert denial.error_code is ErrorCode.SYMLINK_DENIED


def test_symlink_to_inside_is_also_denied(workspace: Path, reader: SafeInputReader) -> None:
    """Workspace内を指すSymlinkも拒否する。

    §1.16.2は「Symlink」を無条件で拒否対象に挙げる。追跡先がたまたま安全でも、
    追跡の可否をその都度判断する構造にすると判定面が広がる。
    """
    (workspace / "docs" / "inner").symlink_to(workspace / "docs" / "readme.md")
    denial = _denial(reader.open_read(CAPABILITY_ID, "docs/inner"))
    assert denial.error_code is ErrorCode.SYMLINK_DENIED


def test_symlinked_intermediate_directory_is_denied(
    workspace: Path, reader: SafeInputReader
) -> None:
    """途中Componentがsymlinkの場合も拒否する。"""
    (workspace / "real").mkdir()
    (workspace / "real" / "file.txt").write_bytes(b"x\n")
    (workspace / "docs" / "linkdir").symlink_to(workspace / "real")
    denial = _denial(reader.open_read(CAPABILITY_ID, "docs/linkdir/file.txt"))
    assert denial.error_code is ErrorCode.SYMLINK_DENIED


# --------------------------------------------------------------------------
# AT-INPUT-PATH-001/SPECIAL_FILE
# --------------------------------------------------------------------------


def test_fifo_is_denied_without_blocking(workspace: Path, reader: SafeInputReader) -> None:
    """FIFOは拒否され、かつ**ブロックしない**。

    回帰対象（2026-08-07検出・修正済み）: `O_NONBLOCK` を付けずにFIFOを
    `O_RDONLY`で開くと、書き手が現れるまで`open`自体が返らない。
    Workspaceへ細工されたFIFOが1つあるだけでHarnessが無期限に停止し、
    File Type検査へ到達する前に固まる。実際にテスト全体が固まって発見した。

    File Type検査は`open`が返ってからしか行えないため、先に`open`を返させる
    必要がある。時間上限付きで検証する。
    """
    os.mkfifo(workspace / "docs" / "pipe")

    timed_out: list[bool] = []

    def _alarm(_signum: int, _frame: object) -> None:
        timed_out.append(True)
        raise TimeoutError("open_read blocked on a FIFO")

    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(5)
    try:
        denial = _denial(reader.open_read(CAPABILITY_ID, "docs/pipe"))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)

    assert not timed_out
    assert denial.error_code is ErrorCode.SPECIAL_FILE_DENIED


def test_directory_is_denied(workspace: Path, reader: SafeInputReader) -> None:
    (workspace / "docs" / "subdir").mkdir()
    denial = _denial(reader.open_read(CAPABILITY_ID, "docs/subdir"))
    assert denial.error_code is ErrorCode.SPECIAL_FILE_DENIED


@pytest.mark.parametrize("long_parent", [False, True], ids=["short-parent", "long-parent"])
def test_socket_is_denied(
    workspace: Path, reader: SafeInputReader, monkeypatch: pytest.MonkeyPatch, long_parent: bool
) -> None:
    import socket

    relative_parent = Path("docs") / ("socket-fixture-" + "x" * 160 if long_parent else ".")
    parent = workspace / relative_parent
    parent.mkdir(exist_ok=True)
    # AF_UNIX's sun_path is short even when the filesystem path is valid.
    # Bind relative to its parent; the reader still receives the real socket path.
    monkeypatch.chdir(parent)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind("sock")
        denial = _denial(reader.open_read(CAPABILITY_ID, (relative_parent / "sock").as_posix()))
        assert denial.error_code is ErrorCode.SPECIAL_FILE_DENIED
    finally:
        sock.close()


def test_character_device_is_denied(workspace: Path, reader: SafeInputReader) -> None:
    """Device Nodeを直接置けない環境では、既存のDeviceへのhardlinkも作れない。

    そのため `/dev/null` を指すSymlinkで代替する。Symlink拒否が先に効くが、
    Device自体が読まれないことが要点である。
    """
    (workspace / "docs" / "nulldev").symlink_to("/dev/null")
    denial = _denial(reader.open_read(CAPABILITY_ID, "docs/nulldev"))
    assert denial.error_code in (
        ErrorCode.SYMLINK_DENIED,
        ErrorCode.SPECIAL_FILE_DENIED,
    )


# --------------------------------------------------------------------------
# 上限
# --------------------------------------------------------------------------


def test_deep_path_is_denied(reader: SafeInputReader) -> None:
    deep = "docs/" + "/".join(f"d{index}" for index in range(64)) + "/x.txt"
    denial = _denial(reader.open_read(CAPABILITY_ID, deep))
    assert denial.error_code is ErrorCode.PATH_OUTSIDE_CAPABILITY
    assert "depth" in denial.detail


def test_missing_file_is_denied(reader: SafeInputReader) -> None:
    denial = _denial(reader.open_read(CAPABILITY_ID, "docs/absent.md"))
    assert denial.error_code is ErrorCode.PATH_OUTSIDE_CAPABILITY


# --------------------------------------------------------------------------
# openat2 と Fallback（ADR-003）
# --------------------------------------------------------------------------


def test_openat2_availability_is_determined_at_runtime() -> None:
    """利用可否を実際に呼んで判定する。結果はbool。"""
    assert isinstance(is_available(), bool)


def test_fallback_walk_enforces_the_same_denials(
    workspace: Path, reader: SafeInputReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-003のFallback経路でも同じ拒否になること。

    `openat2`が使えない環境へ落ちたときだけ判定が緩むと、
    その環境が恒久的な抜け穴になる。
    """
    monkeypatch.setattr("harness.infrastructure.filesystem.safe_reader.is_available", lambda: False)
    (workspace / "docs" / "escape").symlink_to("/etc/passwd")

    assert (
        _denial(reader.open_read(CAPABILITY_ID, "docs/escape")).error_code
        is ErrorCode.SYMLINK_DENIED
    )

    os.mkfifo(workspace / "docs" / "pipe2")
    assert (
        _denial(reader.open_read(CAPABILITY_ID, "docs/pipe2")).error_code
        is ErrorCode.SPECIAL_FILE_DENIED
    )

    # 正常系はFallbackでも読める
    result = reader.open_read(CAPABILITY_ID, "docs/readme.md")
    assert not isinstance(result, ReadDenial)
    assert result[0] == PAYLOAD


def test_enumeration_is_sorted_recursive_and_readable(reader, workspace):
    (workspace / "docs" / "nested").mkdir()
    (workspace / "docs" / "z.txt").write_text("z")
    (workspace / "docs" / "nested" / "a.txt").write_text("a")
    result = reader.enumerate(CAPABILITY_ID, "docs")
    assert result == ["docs/nested/a.txt", "docs/readme.md", "docs/z.txt"]
    for path in result:
        assert not isinstance(reader.open_read(CAPABILITY_ID, path), ReadDenial)


@pytest.mark.parametrize("kind", ["symlink", "fifo", "scope", "revoked", "count"])
def test_enumeration_discards_partial_snapshot(reader, workspace, broker, kind):
    if kind == "symlink":
        (workspace / "docs" / "z-link").symlink_to(workspace / "secret")
    elif kind == "fifo":
        os.mkfifo(workspace / "docs" / "z-pipe")
    elif kind == "revoked":
        broker.revoke(CAPABILITY_ID)
    elif kind == "count":
        for index in range(4096):
            (workspace / "docs" / str(index)).touch()
    result = reader.enumerate(CAPABILITY_ID, "secret" if kind == "scope" else "docs")
    assert isinstance(result, ReadDenial)


def test_enumeration_rechecks_revocation(reader, broker, monkeypatch):
    original = os.scandir

    def revoke(fd):
        broker.revoke(CAPABILITY_ID)
        return original(fd)

    monkeypatch.setattr(os, "scandir", revoke)
    assert isinstance(reader.enumerate(CAPABILITY_ID, "docs"), ReadDenial)


@pytest.mark.parametrize("kind", ["symlink", "file", "directory"])
def test_fallback_intermediate_component_keeps_its_denial_reason(
    workspace: Path, reader: SafeInputReader, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setattr("harness.infrastructure.filesystem.safe_reader.is_available", lambda: False)
    target = workspace / "docs" / "component"
    if kind == "symlink":
        target.symlink_to(workspace / "secret", target_is_directory=True)
    elif kind == "file":
        target.write_bytes(b"synthetic\n")
    else:
        target.mkdir()
        (target / "keys.txt").write_bytes(b"synthetic\n")
    result = reader.open_read(CAPABILITY_ID, "docs/component/keys.txt")
    if kind == "directory":
        assert not isinstance(result, ReadDenial)
        assert result[0] == b"synthetic\n"
    else:
        expected = ErrorCode.SYMLINK_DENIED if kind == "symlink" else ErrorCode.SPECIAL_FILE_DENIED
        assert _denial(result).error_code is expected
