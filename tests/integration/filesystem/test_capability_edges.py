"""Capability仲介の端の経路（§1.16.2、BLOCKER 5 の周辺）。

## なぜ必要か

失効・再発行・Lease解放まわりは BLOCKER 5 で厚く試験したが、
次の分岐は残ったままだった。

* 解放済みLeaseの `fd` を触る
* Broker を閉じたあとの `is_active` / `revoke`
* 未発行IDへの `revoke`
* 失効済みIDでの読取り開始（Lease取得の前段で弾く）

どれも「もう無効なものを使おうとしたとき」の振る舞いである。
無効化の**後**に何が起きるかは、無効化そのものの試験では確かめられない。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.domain.errors import ErrorCode
from harness.domain.input_read import CapabilityScope, ReadDenial
from harness.infrastructure.filesystem.safe_reader import (
    CapabilityBroker,
    CapabilityRevoked,
    SafeInputReader,
)
from harness.infrastructure.filesystem.workspace_boundary import FilesystemPolicy

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
CAPABILITY_ID = "cap-edge"
PAYLOAD = b"readable\n"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "file.txt").write_bytes(PAYLOAD)
    return root


@pytest.fixture
def broker() -> Iterator[CapabilityBroker]:
    with CapabilityBroker(FilesystemPolicy.load(REPO_ROOT), allow_test_filesystems=True) as issued:
        yield issued


# ---------------------------------------------------------------------------
# Lease の解放後
# ---------------------------------------------------------------------------


def test_released_lease_refuses_to_hand_out_its_descriptor(
    workspace: Path, broker: CapabilityBroker
) -> None:
    """解放済みLeaseの `fd` を渡さない。

    解放後のFD番号は**別のFileへ再利用される**。番号だけを持ち回る
    コードが後から `fd` を読むと、まったく無関係なFileを掴む。
    番号が有効に見えるのが厄介で、エラーにならず「別の何か」が読める。
    """
    broker.issue(CAPABILITY_ID, workspace, CapabilityScope(("docs",)))
    grant = broker.acquire_root(CAPABILITY_ID)
    assert grant.lease.fd >= 0

    grant.lease.release()
    with pytest.raises(ValueError, match="already released"):
        _ = grant.lease.fd


def test_releasing_twice_is_harmless(workspace: Path, broker: CapabilityBroker) -> None:
    """二重解放でFD番号を巻き添えにしない。

    2回目が別のFDを閉じると、無関係な読取りが壊れる。原因も辿れない。
    """
    broker.issue(CAPABILITY_ID, workspace, CapabilityScope(("docs",)))
    grant = broker.acquire_root(CAPABILITY_ID)
    grant.lease.release()
    grant.lease.release()


# ---------------------------------------------------------------------------
# 失効済み・未発行
# ---------------------------------------------------------------------------


def test_read_with_a_revoked_capability_is_denied(
    workspace: Path, broker: CapabilityBroker
) -> None:
    """失効後の読取りはLease取得の前段で弾く。

    `open_read` はまず `lookup` で拒否するが、その隙間で失効した場合は
    `acquire_root` が投げる。**両方で止まる**ことを確かめる。
    片方だけだと、順序が変わったときに穴になる。
    """
    broker.issue(CAPABILITY_ID, workspace, CapabilityScope(("docs",)))
    reader = SafeInputReader(broker)
    assert not isinstance(reader.open_read(CAPABILITY_ID, "docs/file.txt"), ReadDenial)

    broker.revoke(CAPABILITY_ID)

    result = reader.open_read(CAPABILITY_ID, "docs/file.txt")
    assert isinstance(result, ReadDenial)
    assert result.error_code is ErrorCode.PATH_OUTSIDE_CAPABILITY

    with pytest.raises(CapabilityRevoked):
        broker.acquire_root(CAPABILITY_ID)


def test_revoking_an_unknown_capability_is_a_no_op(broker: CapabilityBroker) -> None:
    """未発行IDへのrevokeで例外にしない。

    Recovery経路は「念のため失効させる」呼び方をする。存在しないIDで
    落ちると、後片付けの途中で止まって残りが失効しないまま残る。
    """
    broker.revoke("never-issued")


def test_is_active_is_false_after_the_broker_is_closed(workspace: Path) -> None:
    """Brokerを閉じたあとは、どの世代も有効ではない。

    閉じたあとに True を返すと、読取り完了後の再検証（`is_active`）が
    素通りする。Leaseは`dup`済みで生きているため、Bytesは読めている。
    **返してよいかどうか**だけがここで決まる。
    """
    broker = CapabilityBroker(FilesystemPolicy.load(REPO_ROOT), allow_test_filesystems=True)
    broker.issue(CAPABILITY_ID, workspace, CapabilityScope(("docs",)))
    grant = broker.acquire_root(CAPABILITY_ID)
    generation = grant.generation
    grant.lease.release()

    assert broker.is_active(CAPABILITY_ID, generation) is True
    broker.close()
    assert broker.is_active(CAPABILITY_ID, generation) is False


def test_revoke_after_close_is_a_no_op(workspace: Path) -> None:
    """閉じたあとのrevokeで落ちない。後片付けの順序に依存させない。"""
    broker = CapabilityBroker(FilesystemPolicy.load(REPO_ROOT), allow_test_filesystems=True)
    broker.issue(CAPABILITY_ID, workspace, CapabilityScope(("docs",)))
    broker.close()
    broker.revoke(CAPABILITY_ID)


def test_lookup_after_close_returns_nothing(workspace: Path) -> None:
    """閉じたあとにCapabilityを引けない。"""
    broker = CapabilityBroker(FilesystemPolicy.load(REPO_ROOT), allow_test_filesystems=True)
    broker.issue(CAPABILITY_ID, workspace, CapabilityScope(("docs",)))
    assert broker.lookup(CAPABILITY_ID) is not None
    broker.close()
    assert broker.lookup(CAPABILITY_ID) is None
