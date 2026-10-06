"""確定した送信のLeaseを解放し、不明な実行は保持する。"""

import pytest

from harness.domain.errors import HarnessError
from harness.domain.lease import LeaseStatus

from .conftest import make_env, run_to_proposal

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("mode", ["OK", "NON_ZERO", "GARBAGE"])
def test_completed_send_allows_a_new_separately_approved_request(tmp_path, mode):
    env = make_env(tmp_path, mode=mode)
    try:
        first = run_to_proposal(env)
        service = env.gateway._service
        stored = service.inspect(first["session_id"])
        assert stored["send_lease_settlement"] == "RELEASED"
        assert service.leases.get(stored["send_lease_id"]).status is LeaseStatus.RELEASED
        second = run_to_proposal(env)
        assert second["session_id"] != first["session_id"]
        assert len(env.runner.calls) == 2
        with pytest.raises(HarnessError):
            env.gateway.start_send(
                first["session_id"], execution_plan_hash=first["execution_plan_hash"]
            )
        assert len(env.runner.calls) == 2
    finally:
        env.services.close()


def test_legacy_completed_send_can_release_lease_without_resending(tmp_path, monkeypatch):
    env = make_env(tmp_path, mode="NON_ZERO")
    try:
        worker = env.gateway._worker
        with monkeypatch.context() as patch:
            patch.setattr(worker, "_release_completed_send_lease", lambda document: None)
            first = run_to_proposal(env)
        service = env.gateway._service
        old = service.inspect(first["session_id"])
        assert service.leases.get(old["send_lease_id"]).status is LeaseStatus.ACTIVE
        recovered = env.gateway.recover(first["session_id"])
        assert recovered["state"] == "SEND_FAILED"
        assert service.leases.get(old["send_lease_id"]).status is LeaseStatus.RELEASED
        env.gateway.recover(first["session_id"])
        assert len(env.runner.calls) == 1
        assert env.read("hello.py")
    finally:
        env.services.close()


def test_unknown_send_cannot_release_lease_as_completed(tmp_path):
    env = make_env(tmp_path)
    try:
        gateway = env.gateway
        s = gateway.create_session(
            provider_id="codex",
            model_id="fake-model",
            relative_path="hello.py",
            instruction="説明を追加",
        )
        gateway.approve_send(s["session_id"], execution_plan_hash=s["execution_plan_hash"])
        service = gateway._service
        claimed = service.claim_send(s["session_id"])
        # 子Processが終わった証跡がない状態を、完了扱いで回収してはならない。
        with service.uow.begin_immediate(), pytest.raises(HarnessError):
            service._release_completed_send_lease(claimed)
        assert service.leases.get(claimed["send_lease_id"]).status is LeaseStatus.ACTIVE
        assert len(env.runner.calls) == 0
    finally:
        env.services.close()


def test_completed_apply_releases_its_lease_and_allows_next_edit(tmp_path):
    env = make_env(tmp_path)
    try:
        for _ in range(2):
            s = run_to_proposal(env)
            gateway = env.gateway
            gateway.approve_apply(
                s["session_id"],
                apply_execution_plan_hash=s["apply_execution_plan_hash"],
                proposal_hash=s["proposal_hash"],
            )
            applied = gateway.apply(
                s["session_id"], apply_execution_plan_hash=s["apply_execution_plan_hash"]
            )
            assert applied["state"] == "APPLIED"
            stored = gateway._service.inspect(s["session_id"])
            assert stored["apply_lease_settlement"] == "RELEASED"
            assert (
                gateway._service.leases.get(stored["apply_lease_id"]).status is LeaseStatus.RELEASED
            )
        assert len(env.runner.calls) == 2
    finally:
        env.services.close()
