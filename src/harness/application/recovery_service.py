"""Leaseに基づき安全なRecovery候補だけを選別するApplication Service。"""

from __future__ import annotations

from collections.abc import Iterable

from harness.domain.attempt import ActionAttempt
from harness.domain.recovery import (
    MaskingRecoveryDecision,
    RecoveryDecision,
    classify_masking_recovery_stream,
    classify_recovery_candidate,
)
from harness.ports.lease import LeasePort
from harness.ports.masking import MaskingRecoveryStream

__all__ = ["RecoveryCandidateService"]


class RecoveryCandidateService:
    """実行中Workerを打ち切らず、期限切れAttemptだけをRecoveryへ渡す。"""

    def __init__(self, leases: LeasePort) -> None:
        self._leases = leases

    def classify(
        self, attempts: Iterable[ActionAttempt], *, now: str
    ) -> tuple[RecoveryDecision, ...]:
        """全Action Attemptを分類する。返却順は入力順で決定的に維持する。"""
        return tuple(
            classify_recovery_candidate(
                attempt,
                None if attempt.lease_id is None else self._leases.get(attempt.lease_id),
                now=now,
            )
            for attempt in attempts
        )

    def expired_candidates(
        self, attempts: Iterable[ActionAttempt], *, now: str
    ) -> tuple[RecoveryDecision, ...]:
        """自動Recoveryを許可できる期限切れAction Attemptだけを返す。"""
        return tuple(
            decision
            for decision in self.classify(attempts, now=now)
            if decision.automatic_recovery_allowed
        )

    def classify_masking_streams(
        self, streams: Iterable[MaskingRecoveryStream], *, now: str
    ) -> tuple[MaskingRecoveryDecision, ...]:
        """永続化されたMasking Stream→Lease束縛を分類する。"""
        return tuple(
            classify_masking_recovery_stream(
                stream_id=stream.stream_id,
                run_id=stream.run_id,
                attempt_id=stream.attempt_id,
                lease_id=stream.lease_id,
                lease=None if stream.lease_id is None else self._leases.get(stream.lease_id),
                now=now,
            )
            for stream in streams
        )

    def expired_masking_candidates(
        self, streams: Iterable[MaskingRecoveryStream], *, now: str
    ) -> tuple[MaskingRecoveryDecision, ...]:
        """実Recovery Engineへ渡してよい期限切れMasking Streamだけを返す。"""
        return tuple(
            decision
            for decision in self.classify_masking_streams(streams, now=now)
            if decision.automatic_recovery_allowed
        )
