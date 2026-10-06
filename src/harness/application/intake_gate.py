"""受付・作用・復元を同一DBの短いTransactionで直列化する。

受付予約をCommitしてから実処理を開始する。停止と予約の競合はDBで決まり、
復元は予約が残る限り開始できない。Processが失われた予約を時刻だけで解放しない。
ApplicationはPortだけに依存し、接続やファイルを直接操作しない。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from harness.domain._registry_generated import ErrorCode, EventType
from harness.domain.errors import HarnessError
from harness.ports.operation_control import OperationControlStore


@dataclass(frozen=True, slots=True)
class IntakeVerdict:
    request_id: str
    admitted: bool
    error_code: ErrorCode | None
    events: tuple[EventType, ...]
    state_source: str

    @property
    def refused(self) -> bool:
        return not self.admitted


class IntakeRefused(HarnessError):
    def __init__(self, verdict: IntakeVerdict) -> None:
        super().__init__(
            ErrorCode.DEPLOY_DRAIN_REQUIRED,
            "INTAKE_STOPPED: DEPLOY_DRAIN_REQUIRED; maintenance is active",
        )
        self.verdict = verdict


class IntakeGate:
    def __init__(self, store: OperationControlStore) -> None:
        self._store = store

    def _verdict(self, request_id: str, allowed: bool) -> IntakeVerdict:
        return IntakeVerdict(
            request_id,
            allowed,
            None if allowed else ErrorCode.DEPLOY_DRAIN_REQUIRED,
            () if allowed else (EventType.ACTION_BLOCKED,),
            self._store.identity(),
        )

    def evaluate(self, request_id: str) -> IntakeVerdict:
        with self._store.transaction() as state:
            return self._verdict(request_id, state.mode() == "OPEN")

    def admit(self, request_id: str) -> IntakeVerdict:
        """読取り専用の事前確認。実処理には必ずadmission予約も必要。"""
        verdict = self.evaluate(request_id)
        if verdict.refused:
            raise IntakeRefused(verdict)
        return verdict

    @contextmanager
    def admission(self, request_id: str, *, kind: str = "intake") -> Iterator[None]:
        if kind not in ("intake", "effect"):
            raise ValueError("unknown admission kind")
        token = self._store.new_token()
        with self._store.ownership().hold(token) as owner:
            if owner.status != "ACQUIRED":
                raise IntakeRefused(self._verdict(request_id, False))
            with self._store.transaction() as state:
                mode = state.mode()
                allowed = mode == "OPEN" or (kind == "effect" and mode == "DRAINING")
                if allowed:
                    state.reserve(token, kind, request_id=request_id, owner_scope=owner.scope)
                elif mode == "RESTORING":
                    state.record_refusal()
            if not allowed:
                raise IntakeRefused(self._verdict(request_id, False))
            try:
                yield
            finally:
                with self._store.transaction() as state:
                    state.release(token)

    def stop(self) -> None:
        with self._store.transaction() as state:
            if state.mode() == "RESTORING":
                raise IntakeRefused(self._verdict("stop", False))
            state.set_mode("DRAINING")

    def resume(self) -> None:
        """進行中の復元を解除できない。正常検証後の明示的な再開だけに使う。"""
        with self._store.transaction() as state:
            if state.mode() == "RESTORING" or state.active_count() or state.unsettled_count():
                raise IntakeRefused(self._verdict("resume", False))
            state.set_mode("OPEN")

    def active_count(self) -> int:
        with self._store.transaction() as state:
            return state.active_count()

    def refusals(self) -> int:
        with self._store.transaction() as state:
            return state.refusals()

    @contextmanager
    def restoration(self, *, purpose: str = "UNSPECIFIED") -> Iterator[None]:
        token = self._store.new_token()
        with self._store.ownership().hold(token) as owner:
            if owner.status != "ACQUIRED":
                raise IntakeRefused(self._verdict("restore", False))
            with self._store.transaction() as state:
                if state.mode() == "RESTORING" or state.active_count() or state.unsettled_count():
                    raise IntakeRefused(self._verdict("restore", False))
                state.start_restore(token, owner.scope, purpose)
                state.set_mode("RESTORING")
            # Failure retains RESTORING and its source binding. Owner lock release
            # does not itself permit intake, effects, or another restore.
            try:
                yield
            except BaseException:
                with self._store.transaction() as state:
                    state.finish_restore(token, "FAILED")
                raise
            else:
                with self._store.transaction() as state:
                    state.finish_restore(token, "COMPLETED")
                    state.set_mode("DRAINING")
