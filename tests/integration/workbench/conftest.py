"""Workbench 統合試験の共通 Fixture。

## 偽 CLI は必ず実 Process として起動する

関数 Mock では argv・env・cwd・stdin・終了 Code・出力上限・timeout・孫 Process を
一つも踏めない。だから既定の Runner は本物の `SubprocessCliRunner` のままにし、
起動先だけを偽 CLI へ差し替える。**呼出回数を数えたいときだけ** Spy を挿す。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "tests" / "support"))

from workbench_fixtures import (  # noqa: E402
    StubBoundaryProbe,
    build_demo_worktree,
    fake_argv_prefix,
    write_fake_cli,
    write_runtime_profile,
)

from harness.ports.cli_workbench import (  # noqa: E402
    CliLaunchSpec,
    CliRunnerPort,
    CliRunResult,
)
from harness.presentation.local_ui.composition import (  # noqa: E402
    LocalUiServices,
    WorkbenchSetup,
    build_services,
)


@dataclass
class SpyRunner:
    """起動回数を数える Runner。**本物の代わりに使うのは回数を測るときだけ。**"""

    inner: CliRunnerPort | None = None
    calls: list[CliLaunchSpec] = field(default_factory=list)
    payloads: list[bytes] = field(default_factory=list)

    def stop(self) -> None:
        if self.inner is not None:
            self.inner.stop()

    def run(
        self,
        spec: CliLaunchSpec,
        *,
        stdin_payload: bytes,
        timeout_seconds: int,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> CliRunResult:
        self.calls.append(spec)
        self.payloads.append(stdin_payload)
        if self.inner is None:
            raise AssertionError("spy runner was invoked without an inner runner")
        return self.inner.run(
            spec,
            stdin_payload=stdin_payload,
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
        )


@dataclass
class WorkbenchEnv:
    root: Path
    worktree: Path
    profile: Path
    services: LocalUiServices
    runner: SpyRunner

    @property
    def gateway(self) -> Any:
        assert self.services.workbench is not None
        return self.services.workbench

    def read(self, name: str) -> str:
        return (self.worktree / name).read_text(encoding="utf-8")


def make_env(
    tmp_path: Path,
    *,
    mode: str = "OK",
    provider: str = "codex",
    replacement: str | None = None,
    files: dict[str, str] | None = None,
    extra_providers: dict[str, str] | None = None,
    inner_runner: CliRunnerPort | None = None,
    feature_overrides: dict[str, bool] | None = None,
    probe_path: str | None = None,
    boundary_probe: Any | None = None,
    real_boundary: bool = False,
) -> WorkbenchEnv:
    from harness.infrastructure.provider.cli_runner import SubprocessCliRunner

    worktree = build_demo_worktree(tmp_path / "demo", files=files)
    state = tmp_path / "state"
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    executables = {
        provider: fake_argv_prefix(
            write_fake_cli(
                tmp_path / "bin",
                mode=mode,
                provider=provider,
                replacement=replacement,
                feature_overrides=feature_overrides,
                probe_path=probe_path,
            )
        )
    }
    for other_id, other_mode in (extra_providers or {}).items():
        executables[other_id] = fake_argv_prefix(
            write_fake_cli(
                tmp_path / "bin",
                mode=other_mode,
                provider=other_id,
                name=f"fake-{other_id}.py",
            )
        )
    profile = write_runtime_profile(
        tmp_path / "profile.json",
        executables=executables,
        home=home,
        neutral_workdir=state / "cwd",
        state_dir=state,
        # 偽 CLI の導入先。Landlock 境界が読み書きを許す唯一の CLI 領域である。
        cli_runtime_root=tmp_path / "bin",
    )
    runner = SpyRunner(inner=inner_runner or SubprocessCliRunner())
    services = build_services(
        repo_root=REPO_ROOT,
        database_path=tmp_path / "db" / "state.sqlite3",
        artifact_root=tmp_path / "cas",
        workbench=WorkbenchSetup(
            workspace=worktree,
            runtime_profile=profile,
            workspace_label="demo-worktree",
            auth_session="local-uid:" + str(os.getuid()),
            runner=runner,
            # 能力検証は既定で代役を使う。**実測は `test_boundary_gate.py` が行う。**
            # この Machine の実測 Probe は Provider を 1 つも起動させないので、
            # Flow 試験はここで測定だけを差し替える。承認は差し替えない。
            boundary_probe=None if real_boundary else (boundary_probe or StubBoundaryProbe()),
        ),
    )
    return WorkbenchEnv(
        root=tmp_path, worktree=worktree, profile=profile, services=services, runner=runner
    )


@pytest.fixture
def workbench(tmp_path: Path) -> Iterator[WorkbenchEnv]:
    env = make_env(tmp_path)
    try:
        yield env
    finally:
        env.services.close()


def run_to_proposal(
    env: WorkbenchEnv, *, instruction: str = "説明コメントを足してください。"
) -> dict[str, Any]:
    """依頼 → 承認 → 送信 → 応答捕捉まで進める。**適用はしない。**"""
    gateway = env.gateway
    session = gateway.create_session(
        provider_id="codex",
        model_id="fake-model",
        relative_path="hello.py",
        instruction=instruction,
    )
    session = gateway.approve_send(
        session["session_id"], execution_plan_hash=session["execution_plan_hash"]
    )
    session = gateway.start_send(
        session["session_id"], execution_plan_hash=session["execution_plan_hash"]
    )
    assert gateway.wait_for_idle(120)
    return gateway.session(session["session_id"])
