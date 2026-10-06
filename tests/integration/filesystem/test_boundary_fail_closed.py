"""Workspace境界のFail-Closed経路（ADR-002、不変条件#12）。

## なぜ必要か

`workspace_boundary.py` の拒否分岐のうち、次は一度も実行されていなかった。

* `filesystem-policy.yaml` の項目欠落
* 同じFilesystem TypeがAllowとDenyの両方に載っている
* `/proc/self/fdinfo/<fd>` が読めない・`mnt_id` 欄が無い
* Workspace Rootが相対Path／`..` を含む
* Path途中のComponentがDirectoryではない
* `/proc/self/mountinfo` が読めない（`on_mountinfo_unreadable: REJECT`）

いずれも「確認できないときに通さない」ための分岐である。**通さないことを
確かめたことが無い**まま、Registryには REJECT と書いてあった。

Registryの記述はPolicyの宣言であって、実装がそう動く保証ではない。
宣言と実装が食い違っていても、正常系では両者とも黙っている。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from harness.domain.errors import ErrorCode, HarnessError
from harness.infrastructure.filesystem import workspace_boundary as boundary
from harness.infrastructure.filesystem.workspace_boundary import (
    FilesystemPolicy,
    open_and_verify_workspace,
    read_fd_mount_id,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
POLICY_PATH = REPO_ROOT / "design-source" / "registries" / "filesystem-policy.yaml"


def _policy_document() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    return document


def _load_policy(tmp_path: Path, document: dict[str, Any]) -> FilesystemPolicy:
    (tmp_path / "filesystem-policy.yaml").write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return FilesystemPolicy.load(REPO_ROOT, registries=tmp_path)


# ---------------------------------------------------------------------------
# Policy File の不備
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing_key",
    [
        "filesystem_policy_version",
        "allowed_filesystem_types",
        "denied_filesystem_types",
        "test_only_filesystem_types",
        "denied_mount_point_prefixes",
    ],
)
def test_missing_policy_key_is_rejected(tmp_path: Path, missing_key: str) -> None:
    """既定値で埋めない。項目が無いなら判断材料が無いということである。

    「書いていない＝制限なし」と解釈する実装は、Registryを1行削るだけで
    境界が消える。
    """
    document = _policy_document()
    del document[missing_key]

    with pytest.raises(HarnessError) as error:
        _load_policy(tmp_path, document)
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
    assert missing_key in str(error.value)


def test_type_on_both_allow_and_deny_lists_is_rejected(tmp_path: Path) -> None:
    """AllowとDenyの両方に載っているFilesystem Type。

    どちらを優先するかで挙動が変わる。順序に依存する境界は、
    読んだ人ごとに違う結論になる。Registryの段階で潰す。
    """
    document = _policy_document()
    document["denied_filesystem_types"] = [
        *document["denied_filesystem_types"],
        document["allowed_filesystem_types"][0],
    ]

    with pytest.raises(HarnessError) as error:
        _load_policy(tmp_path, document)
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
    assert "both allow and deny" in str(error.value)


def test_unmodified_policy_loads(tmp_path: Path) -> None:
    """壊していない入力が落ちるなら、上の試験は何も証明しない。"""
    policy = _load_policy(tmp_path, _policy_document())
    assert policy.allowed_filesystem_types


# ---------------------------------------------------------------------------
# mnt_id が読めない
# ---------------------------------------------------------------------------


def test_unreadable_fdinfo_is_rejected() -> None:
    """閉じ済みFDの `fdinfo` は読めない。読めなければ通さない。

    ここで例外にせず「越境していない」と扱うと、`/proc` が使えない環境で
    Mount越境の判定が丸ごと無効になる。
    """
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    os.close(fd)

    with pytest.raises(HarnessError) as error:
        read_fd_mount_id(fd)
    assert error.value.code is ErrorCode.WORKSPACE_ON_FOREIGN_FS_DENIED


def test_fdinfo_without_mnt_id_field_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mnt_id` 欄が無い `fdinfo`。将来のKernelで欄が消えた場合にあたる。

    欄が無いのは「越境していない」ではなく「分からない」である。
    """
    fake = tmp_path / "fdinfo-without-mnt-id"
    fake.write_text("pos:\t0\nflags:\t0100000\n", encoding="utf-8")

    real_path = boundary.Path

    def fake_path(argument: str) -> Path:
        if str(argument).startswith("/proc/self/fdinfo/"):
            return fake
        return real_path(argument)

    monkeypatch.setattr(boundary, "Path", fake_path)

    with pytest.raises(HarnessError) as error:
        read_fd_mount_id(1)
    assert error.value.code is ErrorCode.WORKSPACE_ON_FOREIGN_FS_DENIED
    assert "no mnt_id" in str(error.value)


# ---------------------------------------------------------------------------
# Workspace Root の Path そのもの
# ---------------------------------------------------------------------------


def test_relative_workspace_root_is_rejected() -> None:
    """相対Pathを受け付けない。

    解決の基準がProcessのCurrent Directoryになる。Harnessの外の状態で
    境界が動くことになり、Attestationが意味を失う。
    """
    with pytest.raises(HarnessError) as error:
        boundary._open_workspace_root_nofollow("relative/path")
    assert error.value.code is ErrorCode.WORKSPACE_ON_FOREIGN_FS_DENIED
    assert "absolute" in str(error.value)


def test_workspace_root_with_dotdot_is_rejected() -> None:
    """正規化後に `..` が残っている場合。

    `abspath` が畳んでいるはずだが、畳めていなければ通さない。
    「はずだ」で通す箇所を残さない。
    """
    with pytest.raises(HarnessError) as error:
        boundary._open_workspace_root_nofollow("/var/../etc")
    assert error.value.code is ErrorCode.WORKSPACE_ON_FOREIGN_FS_DENIED
    assert ".." in str(error.value)


def test_workspace_root_component_that_is_a_file_is_rejected(tmp_path: Path) -> None:
    """Path途中が通常File。Directoryとして開けない。"""
    regular = tmp_path / "not-a-directory"
    regular.write_text("x", encoding="utf-8")

    with pytest.raises(HarnessError) as error:
        boundary._open_workspace_root_nofollow(str(regular / "child"))
    assert error.value.code is ErrorCode.WORKSPACE_ON_FOREIGN_FS_DENIED
    assert "not a directory" in str(error.value)


# ---------------------------------------------------------------------------
# mountinfo が読めない
# ---------------------------------------------------------------------------


def test_unreadable_mountinfo_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`on_mountinfo_unreadable: REJECT`。

    Mount表が読めなければ、Workspaceがどの Filesystem 上にあるか確認できない。
    確認できないものを許可しない。ADR-002がWindows Native FSを実行時に
    拒否できるのは、この確認が成立している前提の上である。
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setattr(boundary, "_MOUNTINFO_PATH", str(tmp_path / "absent-mountinfo"))

    with pytest.raises(HarnessError) as error:
        open_and_verify_workspace(
            workspace, FilesystemPolicy.load(REPO_ROOT), allow_test_filesystems=True
        )
    assert error.value.code is ErrorCode.WORKSPACE_ON_FOREIGN_FS_DENIED
    assert "cannot read" in str(error.value)


def test_malformed_mountinfo_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """壊れたMount表を推測で読まない。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broken = tmp_path / "broken-mountinfo"
    broken.write_text("this line has no separator at all\n", encoding="utf-8")

    monkeypatch.setattr(boundary, "_MOUNTINFO_PATH", str(broken))

    with pytest.raises(HarnessError) as error:
        open_and_verify_workspace(
            workspace, FilesystemPolicy.load(REPO_ROOT), allow_test_filesystems=True
        )
    assert error.value.code is ErrorCode.WORKSPACE_ON_FOREIGN_FS_DENIED
    assert "malformed" in str(error.value)
