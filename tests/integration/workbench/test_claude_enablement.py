"""Owner承認済みのClaude生成専用経路の境界。"""

import json
import os
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from itertools import count
from pathlib import Path

import pytest

from harness.infrastructure.provider import cli_profiles
from harness.infrastructure.provider.boundary import UNRESOLVED_DESIGN_BLOCKERS, BoundaryMeasurement

from .conftest import make_env

pytestmark = pytest.mark.integration


def test_only_claude_can_use_the_approved_tool_free_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(cli_profiles, "_attest_tool_free_runtime", lambda argv: {"satisfied": True})
    env = make_env(
        tmp_path,
        provider="claude",
        extra_providers={"codex": "OK", "gemini": "OK"},
        real_boundary=True,
    )
    try:
        profiles = cli_profiles.CliRuntimeProfiles(cli_profiles.load_runtime_manifest(env.profile))
        rows = {row.provider_id: row for row in profiles.statuses()}
        assert rows["claude"].profile_verified
        assert not rows["codex"].profile_verified
        assert not rows["gemini"].profile_verified
    finally:
        env.services.close()


def test_claude_stays_blocked_when_tools_cannot_be_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        cli_profiles, "_attest_tool_free_runtime", lambda argv: {"satisfied": False}
    )
    env = make_env(tmp_path, provider="claude", real_boundary=True)
    try:
        profiles = cli_profiles.CliRuntimeProfiles(cli_profiles.load_runtime_manifest(env.profile))
        assert not profiles.boundary_verdict("claude").satisfied
    finally:
        env.services.close()


def test_tool_free_does_not_waive_filesystem_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(cli_profiles, "_attest_tool_free_runtime", lambda argv: {"satisfied": True})
    monkeypatch.setattr(
        cli_profiles.LinuxBoundaryProbe,
        "measure",
        lambda self, request: BoundaryMeasurement(
            filesystem_denied=False,
            descendants_contained=True,
            landlock_abi=1,
            truncate_handled=False,
            blocking_reasons=(*UNRESOLVED_DESIGN_BLOCKERS, "synthetic filesystem failure"),
        ),
    )
    env = make_env(tmp_path, provider="claude", real_boundary=True)
    try:
        profiles = cli_profiles.CliRuntimeProfiles(cli_profiles.load_runtime_manifest(env.profile))
        verdict = profiles.boundary_verdict("claude")
        assert not verdict.satisfied
        assert verdict.blocking_reasons == ("synthetic filesystem failure",)
    finally:
        env.services.close()


def test_independent_real_boundary_measurements_allow_worker_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """別々のnamespaceで測っても同じ制限。実モデル通信は偽CLIで代用する。"""
    monkeypatch.setattr(cli_profiles, "_attest_tool_free_runtime", lambda argv: {"satisfied": True})
    original = cli_profiles.LinuxBoundaryProbe._measure
    numbers = count(100)

    def measure(probe, request):
        observed = original(probe, request)
        details = deepcopy(observed.details)
        # Kernelは終了したnamespace番号を再利用できる。差を確実に再現する。
        details["containment"]["namespace"] = f"pid:[{next(numbers)}]"
        return replace(observed, details=details)

    monkeypatch.setattr(cli_profiles.LinuxBoundaryProbe, "_measure", measure)
    env = make_env(tmp_path, provider="claude", real_boundary=True)
    try:
        gateway = env.gateway
        session = gateway.create_session(
            provider_id="claude",
            model_id="fake-model",
            relative_path="hello.py",
            instruction="説明コメントを追加してください。",
        )
        sid = session["session_id"]
        gateway.approve_send(sid, execution_plan_hash=session["execution_plan_hash"])
        gateway.start_send(sid, execution_plan_hash=session["execution_plan_hash"])
        assert gateway.wait_for_idle(120)
        current = gateway.session(sid)
        assert current["state"] == "PROPOSAL_READY", current["job"]
        assert len(env.runner.calls) == 1
    finally:
        env.services.close()


@pytest.mark.parametrize("field", ["contained", "verdict", "residual_pids", "unknown_field"])
def test_boundary_contract_keeps_security_results_and_unknown_fields(field):
    original = {"details": {"containment": {"namespace": "pid:[1]", field: "before"}}}
    changed = deepcopy(original)
    changed["details"]["containment"][field] = "after"
    assert cli_profiles._boundary_contract_measurement(original) != (
        cli_profiles._boundary_contract_measurement(changed)
    )
    assert original["details"]["containment"]["namespace"] == "pid:[1]"


def test_claude_can_read_only_its_own_maps_in_real_boundary(tmp_path: Path):
    env = make_env(tmp_path, provider="claude")
    try:
        profiles = cli_profiles.CliRuntimeProfiles(cli_profiles.load_runtime_manifest(env.profile))
        program = """
import json
from pathlib import Path
results = {}
for path in ['/proc/self/maps', '/proc/self/environ', '/proc/1/maps', TARGET]:
    try:
        data = Path(path).read_bytes()
        results[path] = bool(data)
    except OSError as error:
        results[path] = error.errno
print(json.dumps(results))
""".replace("TARGET", repr(str(env.worktree / "hello.py")))
        profiles._require_neutral_workdir()
        result = subprocess.run(  # noqa: S603 - fixed diagnostic argv, no shell
            [
                *profiles.launcher_argv,
                json.dumps(profiles.sandbox_policy("claude")),
                "--",
                os.path.realpath(sys.executable),
                "-c",
                program,
            ],
            env=profiles._environment("claude"),
            cwd=profiles.manifest.neutral_workdir,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0
        observed = json.loads(result.stdout)
        assert observed["/proc/self/maps"] is True
        for path in ["/proc/self/environ", "/proc/1/maps", str(env.worktree / "hello.py")]:
            assert observed[path] in (13, 1), observed
    finally:
        env.services.close()
