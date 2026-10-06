"""画面の通常Gatewayにも同一DBの運用停止が効くことを確かめる。"""

from __future__ import annotations

import pytest

from harness.application.intake_gate import IntakeRefused
from harness.infrastructure.runtime_facade import HarnessRuntimeService

from .conftest import WorkbenchEnv, run_to_proposal

pytestmark = pytest.mark.integration


def test_restore_blocks_ui_request_before_session_creation(workbench: WorkbenchEnv) -> None:
    gateway = workbench.gateway
    before = gateway.overview()["sessions"]
    gate = HarnessRuntimeService(workbench.root / "db" / "state.sqlite3").intake_gate()
    with gate.restoration():
        with pytest.raises(IntakeRefused):
            gateway.create_session(
                provider_id="codex",
                model_id="fake-model",
                relative_path="hello.py",
                instruction="合成テスト",
            )
    assert gateway.overview()["sessions"] == before
    assert workbench.runner.calls == []


def test_drain_rejects_new_ui_request(workbench: WorkbenchEnv) -> None:
    gate = HarnessRuntimeService(workbench.root / "db" / "state.sqlite3").intake_gate()
    gate.stop()
    with pytest.raises(IntakeRefused):
        workbench.gateway.create_session(
            provider_id="codex",
            model_id="fake-model",
            relative_path="hello.py",
            instruction="合成テスト",
        )
    assert workbench.runner.calls == []


def test_drain_counts_a_claimed_send_that_was_never_dispatched(workbench: WorkbenchEnv) -> None:
    """claim後・dispatch前にProcessを失った送信を、Drainが進行中として数えること。

    `cli_invocation_journal` は `operation_journal` とは別の表であり、ここを数え落とすと
    「復元開始は未決として拒否するのに、Drain だけが ACCEPTED を返す」食い違いになる。
    """
    gateway = workbench.gateway
    session = gateway.create_session(
        provider_id="codex",
        model_id="fake-model",
        relative_path="hello.py",
        instruction="合成テスト",
    )
    session = gateway.approve_send(
        session["session_id"], execution_plan_hash=session["execution_plan_hash"]
    )
    # 送信Workerへ渡す前の claim だけを確定させる。偽CLIは起動しない。
    gateway._service.claim_send(session["session_id"])
    database = workbench.root / "db" / "state.sqlite3"
    outcome = (
        HarnessRuntimeService(database)
        .backup_restore_service(restore_database=database, restore_cas=database.parent)
        .drain_check(deployment_result_id="dep-claimed")
    )

    assert outcome.verdict.state == "REJECTED"
    assert outcome.verdict.error_code is not None
    assert outcome.verdict.error_code.value == "DEPLOY_DRAIN_REQUIRED"
    assert ("cli_invocation_journal:PREPARED_DURABLE", 1) in outcome.snapshot.active_states
    assert outcome.verdict.active_run_count == 1
    assert outcome.verdict.migration_started is False
    assert workbench.runner.calls == []


def test_restore_blocks_normal_ui_apply_without_touching_workspace(
    workbench: WorkbenchEnv,
) -> None:
    session = run_to_proposal(workbench)
    gateway = workbench.gateway
    before = workbench.read("hello.py")
    calls = len(workbench.runner.calls)
    gate = HarnessRuntimeService(workbench.root / "db" / "state.sqlite3").intake_gate()
    with gate.restoration():
        with pytest.raises(IntakeRefused):
            gateway.approve_apply(
                session["session_id"],
                apply_execution_plan_hash=session["apply_execution_plan_hash"],
                proposal_hash=session["proposal_hash"],
            )
        with pytest.raises(IntakeRefused):
            gateway.apply(
                session["session_id"],
                apply_execution_plan_hash=session["apply_execution_plan_hash"],
            )
    assert workbench.read("hello.py") == before
    assert gateway.session(session["session_id"])["state"] == session["state"]
    assert len(workbench.runner.calls) == calls
