"""起動前の能力検証を **実測** する。合成 canary と自作 Probe だけを使う。

ここだけが `LinuxBoundaryProbe` を本物のまま動かす。ほかの Flow 試験は代役
（`StubBoundaryProbe`）を使う。実 Provider へは 1 度も送信しない。

## この Machine では Provider を起動できない

Landlock ABI 1 は `LANDLOCK_ACCESS_FS_TRUNCATE` を扱わない。読めない File を
`truncate(path, 0)` で 0 bytes にできる。だから Gate は全 Provider を止める。
**それが期待される結果である。** 「動かないから緩める」ことはしない。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from harness.domain.errors import ErrorCode, HarnessError
from harness.infrastructure.provider import sandbox_launcher
from harness.infrastructure.provider.boundary import (
    UNRESOLVED_DESIGN_BLOCKERS,
    BoundaryRequest,
    LinuxBoundaryProbe,
)
from harness.infrastructure.provider.cli_profiles import (
    CliRuntimeProfiles,
    load_runtime_manifest,
)

from .conftest import REPO_ROOT, make_env

sys.path.insert(0, str(REPO_ROOT / "tests" / "support"))
from workbench_fixtures import (
    fake_argv_prefix,
    sandbox_policy_for,
    write_fake_cli,
    write_runtime_profile,
)

pytestmark = pytest.mark.integration


def _measurement(tmp_path: Path):
    """実測を 1 回だけ行い、その全体を返す。**実データは触らせない。**"""
    binary = write_fake_cli(tmp_path / "bin", mode="OK", provider="codex")
    cwd = tmp_path / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    sandbox_tmp = tmp_path / "sandbox-tmp"
    sandbox_tmp.mkdir(parents=True, exist_ok=True)
    pretend = tmp_path / "pretend-workspace"
    pretend.mkdir(parents=True, exist_ok=True)
    (pretend / "keep.txt").write_text("must stay\n", encoding="utf-8")
    request = BoundaryRequest(
        launcher_argv=(
            os.path.realpath(sys.executable),
            str(Path(sandbox_launcher.__file__).resolve()),
        ),
        policy=sandbox_policy_for(read_execute=[binary.parent], read_write=[cwd, sandbox_tmp]),
        env={
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin",
            "TMPDIR": str(sandbox_tmp),
        },
        cwd=str(cwd),
        canary_root=str(tmp_path / "attest"),
        read_only_probes=(str(pretend),),
    )
    return LinuxBoundaryProbe().measure(request), pretend


def test_the_boundary_measurement_reports_each_operation_it_could_deny(
    tmp_path: Path,
) -> None:
    """操作ごとに「拒否できたか」を測る。**まとめて成功と言わない。**"""
    measurement, pretend = _measurement(tmp_path)
    filesystem = measurement.details["filesystem"]
    canaries = filesystem["canaries"]
    assert canaries, "合成 canary を 1 つも試していない"
    for entry, operations in canaries.items():
        for name in ("read", "create", "rename", "unlink"):
            verdict = operations[name]
            assert verdict == ("DENIED(errno=13)" if name == "read" else "DENIED(errno=30)"), (
                f"{entry}:{name}={verdict}"
            )
    # 実 Path へは読取りだけを試し、Bytes を変えない。
    assert filesystem["read_only_probes"][str(pretend)]["read"].startswith("DENIED")
    assert (pretend / "keep.txt").read_text(encoding="utf-8") == "must stay\n"
    # exec を越えた子でも同じ拒否が続く。
    assert filesystem["descendant_after_exec"]["read"].startswith("DENIED")


def test_truncate_is_not_claimed_as_denied_when_the_kernel_cannot_deny_it(
    tmp_path: Path,
) -> None:
    """**R1-A の回帰。** ABI が TRUNCATE を扱わないなら、Gate は起動を止める。

    ABI 3 以上の Kernel では `truncate_handled` が真になり、`truncate` は拒否側へ
    回る。どちらの環境でも「拒否できたことにする」経路は無い。
    """
    measurement, _ = _measurement(tmp_path)
    filesystem = measurement.details["filesystem"]
    truncate_verdicts = {
        operations["truncate"]
        for operations in filesystem["canaries"].values()
        if operations["truncate"] != "NOT_APPLICABLE(directory)"
    }
    if filesystem.get("readonly_mounts"):
        assert truncate_verdicts == {"DENIED(errno=30)"}
        assert filesystem["all_denied"] is True
    elif measurement.truncate_handled:
        assert all(item.startswith("DENIED") for item in truncate_verdicts)
    else:
        assert truncate_verdicts == {"ALLOWED"}
        assert filesystem["all_denied"] is False
        assert not measurement.satisfied
        assert any("TRUNCATE" in reason for reason in measurement.blocking_reasons)


def test_metadata_changes_are_required_to_be_denied() -> None:
    """metadata保護もreadonly mount境界の必須条件とする。"""
    assert "chmod" in sandbox_launcher.PROTECTED_OPERATIONS
    assert "utime" in sandbox_launcher.PROTECTED_OPERATIONS
    assert sandbox_launcher.UNENFORCEABLE_OPERATIONS == ()


def test_a_missing_path_is_not_counted_as_a_denial() -> None:
    """`ENOENT` を `EACCES` の代わりにしない。**触れなかった理由が違う。**"""
    missing = Path(tempfile.gettempdir()) / "harness-not-there-probe"
    verdict = sandbox_launcher._attempt(lambda: missing.read_bytes())
    assert verdict.startswith("INCONCLUSIVE(errno=2)")


def test_a_detached_child_dies_with_the_sandbox_pid_namespace(tmp_path: Path) -> None:
    """**R2-A の実測。** `setsid` した子は PID namespace から出られない。"""
    measurement, _ = _measurement(tmp_path)
    containment = measurement.details["containment"]
    assert containment["contained"] is True, containment["verdict"]
    assert containment["namespace"] != containment.get("own_namespace")
    assert measurement.descendants_contained is True


def test_the_unresolved_credential_and_network_blockers_are_always_reported(
    tmp_path: Path,
) -> None:
    """R1-B は未解決のまま。**Report へ書いたことを承認と読み替えない。**"""
    measurement, _ = _measurement(tmp_path)
    for blocker in UNRESOLVED_DESIGN_BLOCKERS:
        assert blocker in measurement.blocking_reasons
    assert not measurement.satisfied


def test_no_provider_is_selectable_on_a_kernel_without_the_required_boundary(
    tmp_path: Path,
) -> None:
    """必要な境界が無ければ **画面へ理由を出して選ばせない。**"""
    binary = write_fake_cli(tmp_path / "bin", mode="OK", provider="codex")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    profile = write_runtime_profile(
        tmp_path / "profile.json",
        executables={"codex": fake_argv_prefix(binary)},
        home=home,
        neutral_workdir=tmp_path / "state" / "cwd",
        state_dir=tmp_path / "state",
        cli_runtime_root=tmp_path / "bin",
    )
    profiles = CliRuntimeProfiles(manifest=load_runtime_manifest(profile))
    rows = profiles.statuses()
    assert rows
    for row in rows:
        assert row.profile_verified is False
        reason = row.blocking_reason or ""
        assert "必要な実行前境界が成立しない" in reason
        assert "R1-B" in reason


def test_the_gate_stops_the_flow_before_a_single_process_is_started(
    tmp_path: Path,
) -> None:
    """止めるのは **起動前**。理由を返し、Process は 1 つも起こさない。"""
    env = make_env(tmp_path, real_boundary=True)
    try:
        with pytest.raises(HarnessError) as error:
            env.gateway.create_session(
                provider_id="codex",
                model_id="fake-model",
                relative_path="hello.py",
                instruction="コメントを足してください。",
            )
        assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
        assert "必要な実行前境界が成立しない" in str(error.value)
        assert env.runner.calls == []
    finally:
        env.services.close()


def test_the_command_line_cannot_replace_the_boundary_probe() -> None:
    """差し替え口は試験専用である。**CLI からは渡せない。**"""
    source = (REPO_ROOT / "src" / "harness" / "presentation" / "cli.py").read_text(encoding="utf-8")
    assert "WorkbenchSetup(" in source, "CLI が Workbench を組み立てていない"
    assert "boundary_probe" not in source
    for flag in ("--yes", "--auto-approve", "--force", "--skip-approval", "--no-sandbox"):
        assert flag not in source


def test_the_launcher_refuses_to_run_without_a_pid_namespace(tmp_path: Path) -> None:
    """namespace を作れない環境では、**exec せずに終了 Code で止まる。**"""
    cwd = tmp_path / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    marker = tmp_path / "should-not-exist"
    completed = subprocess.run(  # noqa: S603 - fixed argv list, no shell
        [
            os.path.realpath(sys.executable),
            str(Path(sandbox_launcher.__file__).resolve()),
            "not-json",
            "--",
            "/bin/touch",
            str(marker),
        ],
        capture_output=True,
        cwd=str(cwd),
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        timeout=60,
        check=False,
    )
    assert completed.returncode == sandbox_launcher.EXIT_USAGE
    assert not marker.exists()
