"""適用の最終境界。**Fence と、Rename 後 Receipt 前の crash を実際に起こす。**"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_bytes
from harness.ports.lease import LeaseAcquireRequest

from .conftest import WorkbenchEnv, make_env, run_to_proposal

pytestmark = pytest.mark.integration


def _approved(env: WorkbenchEnv) -> dict[str, Any]:
    session = run_to_proposal(env)
    return env.gateway.approve_apply(
        session["session_id"],
        apply_execution_plan_hash=session["apply_execution_plan_hash"],
        proposal_hash=session["proposal_hash"],
    )


def test_a_stale_fence_stops_the_write_at_the_last_moment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Application の確認を通ったあとで Lease を入れ替える。**書込みは起きない。**"""
    env = make_env(tmp_path)
    try:
        session = _approved(env)
        before = env.read("hello.py")
        service = env.gateway._service
        original_prepare = service.workspace.prepare

        def prepare_then_steal_the_lease(**kwargs: Any) -> Any:
            prepared = original_prepare(**kwargs)
            # 別の Worker が同じ Resource を取り直した状況を、Port 経由で作る。
            resource = (
                service.inspect(session["session_id"])["workspace_identity"]
                + ":"
                + session["target"]
            )
            with service.uow.begin_immediate():
                held = service.leases.get(f"lease:{resource}:1")
                assert held is not None
                service.leases.release(held, now=service.clock.now())
                service.leases.acquire(
                    LeaseAcquireRequest(
                        resource,
                        "competing-worker",
                        "competing-attempt",
                        service.clock.now(),
                        session["apply_expires_at"],
                    )
                )
            return prepared

        monkeypatch.setattr(service.workspace, "prepare", prepare_then_steal_the_lease)
        with pytest.raises(HarnessError) as error:
            env.gateway.apply(
                session["session_id"],
                apply_execution_plan_hash=session["apply_execution_plan_hash"],
            )
        assert error.value.code is ErrorCode.STALE_FENCING_TOKEN
        assert env.read("hello.py") == before
    finally:
        env.services.close()


def test_crash_between_rename_and_receipt_is_reconciled_by_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rename は済み、Receipt は未保存。**再書込みせず観測で畳む。**"""
    env = make_env(tmp_path)
    session_id = ""
    proposal_hash = ""
    try:
        session = _approved(env)
        session_id = session["session_id"]
        proposal_hash = session["proposal_hash"]
        service = env.gateway._service

        def crash(*_: Any, **__: Any) -> dict[str, Any]:
            raise OSError("simulated crash after the atomic rename")

        monkeypatch.setattr(service, "_receipt", crash)
        with pytest.raises(OSError):
            env.gateway.apply(
                session_id, apply_execution_plan_hash=session["apply_execution_plan_hash"]
            )
        # Rename は本当に起きている。**ここで再送も再書込みもしない。**
        assert str(hash_bytes((env.worktree / "hello.py").read_bytes())) == proposal_hash
    finally:
        env.services.close()

    restarted = make_env(tmp_path)
    try:
        rows = {row["session_id"]: row for row in restarted.gateway.overview()["sessions"]}
        assert rows[session_id]["state"] == "APPLY_ATTEMPTED"
        recovered = restarted.gateway.recover(session_id)
        assert recovered["state"] == "APPLIED"
        assert recovered["recovery_decision"] == "EXPECTED_EFFECT_OBSERVED_NO_REPLAY"
        assert recovered["apply_result"]["observed_hash"] == proposal_hash
        assert restarted.runner.calls == []
    finally:
        restarted.services.close()


def test_recovery_refuses_to_decide_when_the_bytes_do_not_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path)
    session_id = ""
    try:
        session = _approved(env)
        session_id = session["session_id"]
        service = env.gateway._service
        monkeypatch.setattr(service, "_receipt", _raise)
        with pytest.raises(OSError):
            env.gateway.apply(
                session_id, apply_execution_plan_hash=session["apply_execution_plan_hash"]
            )
    finally:
        env.services.close()

    (tmp_path / "demo" / "worktree" / "hello.py").write_text("someone else\n", encoding="utf-8")
    restarted = make_env(tmp_path)
    try:
        with pytest.raises(HarnessError) as error:
            restarted.gateway.recover(session_id)
        assert error.value.code is ErrorCode.EFFECT_UNKNOWN
        assert restarted.read("hello.py") == "someone else\n"
    finally:
        restarted.services.close()


def _raise(*_: Any, **__: Any) -> dict[str, Any]:
    raise OSError("simulated crash after the atomic rename")
