"""実bind mountに対するWorkspaceWriterのMount境界統合試験。"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from harness.domain.errors import ErrorCode

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
DRIVER = Path(__file__).parent / "workspace_writer_mount_driver.py"


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, shell disabled
        argv, capture_output=True, text=True, timeout=120, check=False
    )


def _mount_namespace_prefix() -> list[str]:
    """非特権user namespaceを優先し、不可ならpasswordless sudoを使う。"""
    unshare = shutil.which("unshare")
    if unshare is None:
        return []
    probe = _run([unshare, "--user", "--map-root-user", "--mount", "true"])
    if probe.returncode == 0:
        return [unshare, "--user", "--map-root-user", "--mount", "--propagation", "private"]
    sudo = shutil.which("sudo")
    if sudo is not None and _run([sudo, "-n", "true"]).returncode == 0:
        return [sudo, "-n", unshare, "--mount", "--propagation", "private"]
    return []


_PREFIX = _mount_namespace_prefix()


@pytest.mark.case("AT-INPUT-PATH-001/MOUNT_CROSSING")
def test_workspace_writer_rejects_a_real_same_device_bind_mount(tmp_path: Path) -> None:
    """実bind mountではst_devが同じでも、Writerが越境書込みを拒否する。"""
    assert _PREFIX, (
        "Mount名前空間を作れないためWorkspaceWriterのbind mount拒否を実測できない。"
        "非特権user namespaceまたはpasswordless sudoが必要である。"
    )
    result = _run([*_PREFIX, sys.executable, str(DRIVER), str(REPO_ROOT), str(tmp_path)])
    assert result.returncode == 0, (
        f"実bind mount Driverが失敗した。stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    report = json.loads(result.stdout.strip().splitlines()[-1])

    assert report["same_device"] is True, "bind mountでst_devが変わり、試験前提が崩れている"
    assert report["crossing_error"] == ErrorCode.MOUNT_CROSSING_DENIED.value
    assert report["outside_content"] == "before\n", "Workspace外の実Fileが書き換えられた"
    assert report["normal_committed"] is True
    assert report["normal_content"] == "after\n", "通常のWorkspace内書込みまで拒否している"
