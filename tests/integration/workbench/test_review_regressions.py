"""Codex レビュー R1〜R3 の回帰試験。

いずれも **修正前の実装では落ちる** 形にしてある。再現記録は
`docs/development/cli-workbench-review-fix-20260908/reproduction-before.json`。

* R1: 実行前の操作範囲制限が足りない（shell_tool 等が有効のまま）
* R2: 親 CLI が終わったあと子孫が残る
* R3: 8 MiB 以上の実行体を同じ size/inode/mtime で書き換えると検査を通る
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_canonical
from harness.infrastructure.provider import sandbox_launcher
from harness.infrastructure.provider.cli_profiles import (
    CliRuntimeProfiles,
    load_runtime_manifest,
)
from harness.infrastructure.provider.cli_runner import SubprocessCliRunner
from harness.ports.cli_workbench import (
    CliLaunchSpec,
    CliOutcome,
    CliRunResult,
    Containment,
    DescendantCleanup,
)

from .conftest import REPO_ROOT, WorkbenchEnv, make_env, run_to_proposal

sys.path.insert(0, str(REPO_ROOT / "tests" / "support"))
from workbench_fixtures import (
    StubBoundaryProbe,
    contained_argv_prefix,
    fake_argv_prefix,
    sandbox_policy_for,
    write_fake_cli,
    write_runtime_profile,
)

pytestmark = pytest.mark.integration


# =====================================================================
# R1: 実行前の操作範囲制限
# =====================================================================


def test_the_boundary_gate_refuses_to_attest_when_a_required_protection_is_missing(
    tmp_path: Path,
) -> None:
    """必要な保護が欠けていれば、**Plan を作らせない。**

    実測そのものは `test_boundary_gate.py` が `LinuxBoundaryProbe` で行う。ここでは
    「測定が不成立を返したら止まる」ことだけを見る。
    """
    reason = "合成: TRUNCATE を拒否できない"
    env = make_env(tmp_path, boundary_probe=StubBoundaryProbe(satisfied=False, reasons=(reason,)))
    try:
        rows = env.gateway._service.provider_status()
        assert rows
        assert [row["provider_id"] for row in rows] == ["codex", "chatgpt"]
        for row in rows:
            assert row["profile_verified"] is False
            if row["provider_id"] == "chatgpt":
                # The separate HTTP route has no CLI boundary; it still needs its own login.
                assert row["blocking_reason"] == "CHATGPT_LOGIN_REQUIRED"
                assert row["models"] == []
            else:
                assert reason in (row["blocking_reason"] or "")
        with pytest.raises(HarnessError) as error:
            env.gateway.create_session(
                provider_id="codex",
                model_id="fake-model",
                relative_path="hello.py",
                instruction="コメントを足してください。",
            )
        assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
        assert reason in str(error.value)
        # **起動 0 回。** 理由を出して止まるだけで、Process は起こさない。
        assert env.runner.calls == []
    finally:
        env.services.close()


def test_codex_argv_disables_every_removable_capability(workbench: WorkbenchEnv) -> None:
    from harness.infrastructure.provider.cli_profiles import _CODEX_DISABLED_FEATURES

    spec = workbench.gateway._service.profiles.resolve(provider_id="codex", model_id="fake-model")
    argv = list(spec.argv)
    for name in _CODEX_DISABLED_FEATURES:
        index = argv.index(name)
        assert argv[index - 1] == "--disable", f"{name} is not passed to --disable"
    # 実行体は Launcher であり、境界を掛けてから CLI へ置き換わる。
    assert argv[1].endswith("sandbox_launcher.py")
    policy = json.loads(argv[2])
    assert policy["contract"] == sandbox_launcher.POLICY_CONTRACT
    assert str(workbench.worktree) not in json.dumps(policy)


def test_attestation_stops_when_a_restricted_capability_stays_enabled(tmp_path: Path) -> None:
    """`--disable` したはずの機能が `true` のままなら、**Plan を作らせない。**"""
    env = make_env(tmp_path, feature_overrides={"shell_tool": True})
    try:
        with pytest.raises(HarnessError) as error:
            env.gateway.create_session(
                provider_id="codex",
                model_id="fake-model",
                relative_path="hello.py",
                instruction="コメントを足してください。",
            )
        assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
        assert "shell_tool" in str(error.value)
        assert env.runner.calls == []
    finally:
        env.services.close()


def test_a_newly_unremovable_capability_is_not_accepted_silently(tmp_path: Path) -> None:
    """既知の例外（unified_exec）以外が居座ったら止める。"""
    env = make_env(tmp_path, feature_overrides={"unified_exec_zsh_fork": True})
    try:
        with pytest.raises(HarnessError) as error:
            env.gateway.create_session(
                provider_id="codex",
                model_id="fake-model",
                relative_path="hello.py",
                instruction="コメントを足してください。",
            )
        assert "unified_exec_zsh_fork" in str(error.value)
    finally:
        env.services.close()


def test_the_known_unremovable_capability_is_recorded_not_hidden(
    workbench: WorkbenchEnv,
) -> None:
    """消せない機能は **消せないと記録する。** 黙って通さない。"""
    profiles = workbench.gateway._service.profiles
    spec = profiles.resolve(provider_id="codex", model_id="fake-model")
    features = profiles.attest(spec)["provider_features"]
    assert features["still_enabled"] == ["unified_exec"]
    assert features["known_unremovable"] == ["unified_exec"]
    assert features["contained_by"] == "HARNESS_LANDLOCK_BOUNDARY"
    risks = " ".join(spec.runtime_projection["residual_risks"])
    assert "unified_exec" in risks


def test_a_cli_that_tries_to_leave_the_boundary_is_blocked_during_the_run(
    tmp_path: Path,
) -> None:
    """実際に走った CLI が Workspace を読もうとしても読めない。

    事後の応答検査ではなく、**実行中に遮断される**ことを見る。
    """
    worktree = tmp_path / "demo" / "worktree"
    env = make_env(tmp_path, mode="PROBE_ESCAPE", probe_path=str(worktree / "notes.md"))
    try:
        session = run_to_proposal(env)
        assert session["state"] == "PROPOSAL_READY"
        diff = env.gateway.diff(session["session_id"])
        # 偽 CLI は読めた内容か errno を返す。**読めていない。**
        assert diff["after_text"].startswith("DENIED"), diff["after_text"][:120]
        assert "メモ" not in diff["after_text"]
    finally:
        env.services.close()


def test_the_attestation_is_bound_to_the_approval(workbench: WorkbenchEnv) -> None:
    """測った結果は Plan へ束縛される。緩めば承認は失効する。"""
    session = workbench.gateway.create_session(
        provider_id="codex",
        model_id="fake-model",
        relative_path="hello.py",
        instruction="コメントを足してください。",
    )
    document = workbench.gateway._service.inspect(session["session_id"])
    attestation = document["restriction_attestation"]
    assert attestation["boundary"]["all_denied"] is True
    assert attestation["boundary"]["unenforceable_operations"] == []
    assert {"chmod", "utime"} <= set(attestation["boundary"]["required_operations"])
    assert attestation["boundary"]["mount_policy"] == "recursive-readonly-with-explicit-write-paths"
    assert attestation["method"] == "LANDLOCK_SELF_TEST_AND_CODEX_FEATURE_LIST"


# =====================================================================
# R2: 子孫 Process の後始末
# =====================================================================
#
# 「片付いた」と言えるのは PID namespace の member を数えられたときだけである。
# Process Group は `setsid()` で抜けられるので、Group が空でも子孫は残り得る。


def _spec(
    tmp_path: Path,
    script: Path,
    cwd: Path,
    *,
    contained: bool = True,
) -> CliLaunchSpec:
    """偽 CLI の起動仕様。既定では **本番と同じ Launcher 経由** で封じ込める。"""
    cwd.mkdir(parents=True, exist_ok=True)
    sandbox_tmp = tmp_path / "sandbox-tmp"
    sandbox_tmp.mkdir(parents=True, exist_ok=True)
    if contained:
        policy = sandbox_policy_for(read_execute=[script.parent], read_write=[cwd, sandbox_tmp])
        argv = tuple(contained_argv_prefix(script, policy))
        containment = Containment.PID_NAMESPACE
    else:
        argv = tuple(fake_argv_prefix(script))
        containment = Containment.NONE
    projection = {"argv": list(argv), "containment": containment}
    return CliLaunchSpec(
        provider_id="codex",
        model_id="probe",
        argv=argv,
        env={
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin",
            "TMPDIR": str(sandbox_tmp),
        },
        cwd=str(cwd),
        runtime_hash=hash_canonical(projection, artifact_type="probe", schema_major=1),
        runtime_projection=projection,
        containment=containment,
    )


def _alive(pid: int) -> bool:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    marker = text.rfind(")")
    return marker >= 0 and text[marker + 2 :].split()[0] != "Z"


def _run(spec: CliLaunchSpec, *, timeout: int = 25, limit: int = 65536) -> CliRunResult:
    return SubprocessCliRunner().run(
        spec,
        stdin_payload=b"{}",
        timeout_seconds=timeout,
        max_stdout_bytes=limit,
        max_stderr_bytes=limit,
    )


def test_a_child_that_left_the_process_group_is_not_reported_as_settled(
    tmp_path: Path,
) -> None:
    """**R2-A の回帰。** `setsid` で離脱した子が居ても片付いたと言わせない。

    修正前は Group だけを数えていたので、離脱した子が動いていても
    `CONFIRMED_EMPTY` を返した（独立再現: 境界レビューの REVIEW-EVIDENCE.json）。
    いまは PID namespace ごと落ちるので、実際に残らない。
    """
    script = write_fake_cli(tmp_path / "bin", mode="DETACH_CHILD", provider="codex")
    result = _run(_spec(tmp_path, script, tmp_path / "cwd"))
    assert result.stdout.decode("utf-8", "replace").startswith("detached ")
    assert result.descendant_cleanup is DescendantCleanup.CONFIRMED_EMPTY
    assert result.residual_pids == ()
    assert result.descendants_settled


def test_without_containment_a_detached_child_is_never_called_settled(
    tmp_path: Path,
) -> None:
    """封じ込めの無い起動では、**確認できないことを結論にする。**

    Group を畳んでも離脱した子は残る。ここで `CONFIRMED_EMPTY` を返していたのが
    R2-A だった。いまは `UNVERIFIABLE` を返し、呼出側が `EFFECT_UNKNOWN` へ倒す。
    """
    script = write_fake_cli(tmp_path / "bin", mode="DETACH_CHILD", provider="codex")
    result = _run(_spec(tmp_path, script, tmp_path / "cwd", contained=False))
    marker, _, pid_text = result.stdout.decode("utf-8", "replace").strip().partition(" ")
    assert marker == "detached"
    escaped = int(pid_text)
    try:
        assert result.descendant_cleanup is DescendantCleanup.UNVERIFIABLE
        assert not result.descendants_settled
        # 「確認できない」は実際に残っているという意味でもある。
        assert _alive(escaped)
    finally:
        # **自分が起こした Process だけを回収する。**
        try:
            os.kill(escaped, signal.SIGKILL)
        except OSError:
            pass


def test_a_descendant_does_not_survive_the_runner_when_the_parent_exits_first(
    tmp_path: Path,
) -> None:
    """親だけ終了しても、孫は Runner が戻る前に片付いている。"""
    script = write_fake_cli(tmp_path / "bin", mode="LEAK_CHILD", provider="codex")
    result = _run(_spec(tmp_path, script, tmp_path / "cwd"))
    assert result.descendant_cleanup is DescendantCleanup.CONFIRMED_EMPTY
    assert result.residual_pids == ()
    assert result.descendants_settled


def test_a_double_forked_grandchild_is_also_settled(tmp_path: Path) -> None:
    """二重 fork で親を失った孫も、namespace ごと片付く。"""
    script = write_fake_cli(tmp_path / "bin", mode="DOUBLE_FORK", provider="codex")
    result = _run(_spec(tmp_path, script, tmp_path / "cwd"))
    assert result.stdout.decode("utf-8", "replace").startswith("orphaned")
    assert result.descendant_cleanup is DescendantCleanup.CONFIRMED_EMPTY


def test_a_child_that_ignores_sigterm_is_still_stopped(tmp_path: Path) -> None:
    """TERM を無視する子も、KILL と終了観測まで行う。"""
    script = write_fake_cli(tmp_path / "bin", mode="SIGTERM_IGNORE", provider="codex")
    started = time.monotonic()
    result = _run(_spec(tmp_path, script, tmp_path / "cwd"), timeout=3)
    assert result.outcome is CliOutcome.TIMEOUT
    assert result.descendant_cleanup is DescendantCleanup.CONFIRMED_EMPTY
    assert time.monotonic() - started < 60


def test_an_output_flood_is_cut_and_the_group_is_still_settled(tmp_path: Path) -> None:
    """上限超過で打ち切っても、後片付けの結論は同じ規則で出す。"""
    script = write_fake_cli(tmp_path / "bin", mode="FLOOD_STDOUT", provider="codex")
    result = _run(_spec(tmp_path, script, tmp_path / "cwd"), timeout=30, limit=4096)
    assert result.outcome is CliOutcome.OUTPUT_LIMIT_EXCEEDED
    assert result.descendant_cleanup is DescendantCleanup.CONFIRMED_EMPTY


def test_stopping_the_runner_takes_the_whole_group_down(tmp_path: Path) -> None:
    """`stop()` でも namespace ごと畳む。Pipe close との併用で例外を握り潰さない。"""
    import threading

    script = write_fake_cli(tmp_path / "bin", mode="HANG", provider="codex")
    runner = SubprocessCliRunner()
    outcome: list[CliRunResult] = []
    spec = _spec(tmp_path, script, tmp_path / "cwd")

    def go() -> None:
        outcome.append(
            runner.run(
                spec,
                stdin_payload=b"{}",
                timeout_seconds=120,
                max_stdout_bytes=65536,
                max_stderr_bytes=65536,
            )
        )

    thread = threading.Thread(target=go)
    thread.start()
    time.sleep(3)
    runner.stop()
    runner.stop()  # 二重呼出しでも壊れない
    thread.join(timeout=60)
    assert outcome, "runner did not return"
    assert outcome[0].descendant_cleanup is DescendantCleanup.CONFIRMED_EMPTY


def test_an_unreadable_proc_is_not_read_as_an_empty_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/proc` を読めないことは「残っていない」の証拠にならない。"""
    from harness.infrastructure.provider import cli_runner as runner_module
    from harness.infrastructure.provider.process_containment import ProcessCensus

    monkeypatch.setattr(
        runner_module,
        "namespace_members",
        lambda namespace, **kwargs: ProcessCensus(namespace, (), reliable=False),
    )
    script = write_fake_cli(tmp_path / "bin", mode="OK", provider="codex")
    result = _run(_spec(tmp_path, script, tmp_path / "cwd"))
    assert result.descendant_cleanup is DescendantCleanup.UNVERIFIABLE
    assert not result.descendants_settled


def test_an_unidentifiable_namespace_is_not_read_as_an_empty_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """namespace を特定できなければ、全数確認はできていない。"""
    from harness.infrastructure.provider import cli_runner as runner_module
    from harness.infrastructure.provider.process_containment import NamespaceLookupFailed

    def refuse(*_args: object, **_kwargs: object) -> str:
        raise NamespaceLookupFailed("synthetic")

    monkeypatch.setattr(runner_module, "find_sandbox_namespace", refuse)
    script = write_fake_cli(tmp_path / "bin", mode="OK", provider="codex")
    result = _run(_spec(tmp_path, script, tmp_path / "cwd"))
    assert result.descendant_cleanup is DescendantCleanup.UNVERIFIABLE


def test_the_group_is_not_signalled_after_the_parent_has_been_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回収済みの Group へ signal を送らない。**PID 再利用で他人を止めない。**

    レビューの静的指摘（`poll()`/`wait()` が先に親を回収する経路がある）への回帰。
    正常終了では `_pump` が先に `wait()` を済ませているので、そのあとの後片付けは
    Group ではなく namespace 所属を確かめた PID にだけ働く。
    """
    from harness.infrastructure.provider import cli_runner as runner_module

    sent: list[tuple[int, int]] = []
    original = runner_module._signal_group
    monkeypatch.setattr(
        runner_module,
        "_signal_group",
        lambda group, number: sent.append((group, number)) or original(group, number),
    )
    script = write_fake_cli(tmp_path / "bin", mode="OK", provider="codex")
    result = _run(_spec(tmp_path, script, tmp_path / "cwd"))
    assert result.outcome is CliOutcome.COMPLETED
    assert sent == [], f"reaped group was signalled: {sent}"
    assert result.descendant_cleanup is DescendantCleanup.CONFIRMED_EMPTY


def test_a_live_group_is_still_signalled_when_the_run_is_cut_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """まだ回収していない Group へは送る。**止める手段を失っていない。**"""
    from harness.infrastructure.provider import cli_runner as runner_module

    sent: list[tuple[int, int]] = []
    original = runner_module._signal_group
    monkeypatch.setattr(
        runner_module,
        "_signal_group",
        lambda group, number: sent.append((group, number)) or original(group, number),
    )
    script = write_fake_cli(tmp_path / "bin", mode="HANG", provider="codex")
    result = _run(_spec(tmp_path, script, tmp_path / "cwd"), timeout=3)
    assert result.outcome is CliOutcome.TIMEOUT
    assert sent, "a live group was never signalled"
    assert result.descendant_cleanup is DescendantCleanup.CONFIRMED_EMPTY


def test_residual_descendants_are_reported_as_effect_unknown(tmp_path: Path) -> None:
    """止めきれなかった場合、出力が読めていても **不明で閉じる。**"""

    class LeakingRunner:
        def __init__(self) -> None:
            self.calls = 0

        def stop(self) -> None:
            return None

        def run(self, spec: Any, **kwargs: Any) -> CliRunResult:
            self.calls += 1
            body = json.dumps({"replacement_text": "x\n"}, ensure_ascii=False)
            envelope = "\n".join(
                (
                    json.dumps({"type": "thread.started", "thread_id": "t"}),
                    json.dumps({"type": "turn.started"}),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"id": "i", "type": "agent_message", "text": body},
                        }
                    ),
                    json.dumps({"type": "turn.completed"}),
                )
            )
            return CliRunResult(
                outcome=CliOutcome.COMPLETED,
                exit_code=0,
                stdout=envelope.encode("utf-8"),
                stdout_truncated=False,
                stderr_bytes_observed=0,
                stderr_truncated=False,
                diagnostic_id="probe",
                descendant_cleanup=DescendantCleanup.RESIDUAL_PROCESSES,
                residual_pids=(4242,),
            )

    env = make_env(tmp_path, inner_runner=LeakingRunner())
    try:
        session = run_to_proposal(env)
        assert session["state"] == "SEND_UNKNOWN"
        assert session["failure"]["class"] == "EFFECT_UNKNOWN"
        assert "4242" in session["failure"]["detail"]
        assert session["send_observation"]["descendant_cleanup"] == "RESIDUAL_PROCESSES"
        assert session["proposal_hash"] is None
    finally:
        env.services.close()


def test_an_unverifiable_cleanup_is_reported_as_effect_unknown(tmp_path: Path) -> None:
    """「確認できない」も **成功にしない。** 不明で閉じる。"""

    class UnverifiableRunner:
        def stop(self) -> None:
            return None

        def run(self, spec: Any, **kwargs: Any) -> CliRunResult:
            return CliRunResult(
                outcome=CliOutcome.COMPLETED,
                exit_code=0,
                stdout=b"{}",
                stdout_truncated=False,
                stderr_bytes_observed=0,
                stderr_truncated=False,
                diagnostic_id="probe",
                descendant_cleanup=DescendantCleanup.UNVERIFIABLE,
            )

    env = make_env(tmp_path, inner_runner=UnverifiableRunner())
    try:
        session = run_to_proposal(env)
        assert session["state"] == "SEND_UNKNOWN"
        assert session["failure"]["class"] == "EFFECT_UNKNOWN"
        assert session["send_observation"]["descendant_cleanup"] == "UNVERIFIABLE"
    finally:
        env.services.close()


def test_a_failed_spawn_is_recorded_as_a_settled_group(tmp_path: Path) -> None:
    """起動できなかったときは、後片付けの余地も無い。"""
    missing = tmp_path / "not-there"
    projection = {"argv": [str(missing)]}
    spec = CliLaunchSpec(
        provider_id="codex",
        model_id="probe",
        argv=(str(missing),),
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        cwd=str(tmp_path),
        runtime_hash=hash_canonical(projection, artifact_type="probe", schema_major=1),
        runtime_projection=projection,
    )
    result = SubprocessCliRunner().run(
        spec,
        stdin_payload=b"{}",
        timeout_seconds=10,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
    )
    assert result.outcome is CliOutcome.SPAWN_FAILED
    assert result.descendant_cleanup is DescendantCleanup.CONFIRMED_EMPTY


# =====================================================================
# R3: 実行体の同一性
# =====================================================================


def _big_profile(tmp_path: Path, size: int) -> tuple[CliRuntimeProfiles, Path]:
    binary = tmp_path / "bin" / "big-cli"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"A" * size)
    (tmp_path / "home").mkdir(exist_ok=True)
    profile = write_runtime_profile(
        tmp_path / "profile.json",
        executables={"codex": [str(binary)]},
        home=tmp_path / "home",
        neutral_workdir=tmp_path / "state" / "cwd",
        state_dir=tmp_path / "state",
        cli_runtime_root=tmp_path / "bin",
    )
    return (
        CliRuntimeProfiles(
            manifest=load_runtime_manifest(profile), boundary_probe=StubBoundaryProbe()
        ),
        binary,
    )


@pytest.mark.parametrize("size", [9 * 1024 * 1024, 64 * 1024])
def test_same_stat_but_different_bytes_invalidates_the_plan(tmp_path: Path, size: int) -> None:
    """size・inode・mtime を保ったまま Bytes を書き換えても見逃さない。

    以前は 8 MiB 以上を `(path,size,mtime_ns,ino,dev)` で Cache していたため、
    **mtime を戻すと古い Hash で検査が通った。** 閾値の上下どちらでも試す。
    """
    profiles, binary = _big_profile(tmp_path, size)
    old_spec = profiles.resolve(provider_id="codex", model_id="m")
    profiles.verify(old_spec)  # 改変前は通る

    before = binary.stat()
    digest_before = hashlib.sha256(binary.read_bytes()).hexdigest()
    with binary.open("r+b") as handle:
        handle.seek(1024)
        handle.write(b"B")
    os.utime(binary, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = binary.stat()

    assert (after.st_size, after.st_ino, after.st_mtime_ns) == (
        before.st_size,
        before.st_ino,
        before.st_mtime_ns,
    )
    assert hashlib.sha256(binary.read_bytes()).hexdigest() != digest_before

    with pytest.raises(HarnessError) as error:
        profiles.verify(old_spec)
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH


def test_an_unchanged_executable_still_verifies(tmp_path: Path) -> None:
    """改変が無ければ通る。**厳しくして使えなくしたのではない。**"""
    profiles, _ = _big_profile(tmp_path, 9 * 1024 * 1024)
    spec = profiles.resolve(provider_id="codex", model_id="m")
    for _ in range(3):
        profiles.verify(spec)


def test_the_identity_records_the_inode_so_a_swap_is_detected(tmp_path: Path) -> None:
    """同じ Bytes でも別の実体へ入れ替わったら失効する。"""
    profiles, binary = _big_profile(tmp_path, 64 * 1024)
    old_spec = profiles.resolve(provider_id="codex", model_id="m")
    payload = binary.read_bytes()
    replacement = binary.with_name("swap")
    replacement.write_bytes(payload)
    os.replace(replacement, binary)
    with pytest.raises(HarnessError) as error:
        profiles.verify(old_spec)
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH


def test_a_symlinked_executable_is_refused(tmp_path: Path) -> None:
    """最終要素の Symlink は開かない。"""
    profiles, binary = _big_profile(tmp_path, 4096)
    real = binary.with_name("real-cli")
    binary.rename(real)
    binary.symlink_to(real)
    with pytest.raises(HarnessError) as error:
        profiles.resolve(provider_id="codex", model_id="m")
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH


def test_the_launcher_is_part_of_the_executable_identity(workbench: WorkbenchEnv) -> None:
    """境界を掛けるのは Launcher なので、差し替われば失効する。"""
    spec = workbench.gateway._service.profiles.resolve(provider_id="codex", model_id="fake-model")
    paths = [row["path"] for row in spec.runtime_projection["executables"]]
    assert any(path.endswith("sandbox_launcher.py") for path in paths)
    for row in spec.runtime_projection["executables"]:
        assert set(row) == {"path", "size_bytes", "sha256", "device", "inode"}


def test_the_launcher_refuses_to_exec_without_a_boundary(tmp_path: Path) -> None:
    """Landlock を掛けられなければ **exec しない。**"""
    marker = tmp_path / "should-not-exist"
    result = subprocess.run(  # noqa: S603 - fixed argv list, no shell
        [
            os.path.realpath(sys.executable),
            str(Path(sandbox_launcher.__file__).resolve()),
            json.dumps({"contract": "wrong-contract/9"}),
            "--",
            "/usr/bin/touch",
            str(marker),
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 3
    assert not marker.exists()
    assert b"unsupported sandbox policy contract" in result.stderr


def test_the_launcher_stops_when_a_required_path_is_missing(tmp_path: Path) -> None:
    """許可 Path の書き忘れを黙って飛ばさない。"""
    marker = tmp_path / "should-not-exist"
    policy = {
        "contract": sandbox_launcher.POLICY_CONTRACT,
        "read_execute": ["/usr", str(tmp_path / "absent")],
        "read_only": [],
        "read_write": [],
        "optional_paths": [],
    }
    result = subprocess.run(  # noqa: S603 - fixed argv list, no shell
        [
            os.path.realpath(sys.executable),
            str(Path(sandbox_launcher.__file__).resolve()),
            json.dumps(policy),
            "--",
            "/usr/bin/touch",
            str(marker),
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 3
    assert not marker.exists()
    assert b"sandbox path is unavailable" in result.stderr


def test_signal_handling_does_not_swallow_unexpected_errors(tmp_path: Path) -> None:
    """`killpg` の ESRCH/EPERM 以外は握り潰さない。"""
    from harness.infrastructure.provider import cli_runner

    with pytest.raises(OSError):
        cli_runner._signal_group(-1, signal.SIGTERM)
