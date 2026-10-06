"""Lease状態に基づくRecovery候補のFail-Closed分類。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from harness.domain.attempt import ActionAttempt
from harness.domain.lease import Lease, LeaseStatus
from harness.domain.timestamps import canonical_timestamp, timestamp_seconds
from harness.domain.transitions import ACTION_ATTEMPT_TERMINAL_STATES

__all__ = [
    "MaskingRecoveryDecision",
    "RecoveryClassification",
    "RecoveryDecision",
    "classify_masking_recovery_stream",
    "classify_recovery_candidate",
]


class RecoveryClassification(Enum):
    """Recovery起動前の候補分類。

    `EXPIRED`だけが自動Recovery候補である。Lease未取得・不一致・解放済みは
    Effect再実行を許さず、Operatorによる実体照合が必要な状態として分離する。
    """

    TERMINAL = "TERMINAL"
    NOT_LEASED = "NOT_LEASED"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    LEASE_MISSING = "LEASE_MISSING"
    LEASE_MISMATCH = "LEASE_MISMATCH"
    LEASE_RELEASED = "LEASE_RELEASED"


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    attempt_id: str
    classification: RecoveryClassification
    lease_id: str | None

    @property
    def automatic_recovery_allowed(self) -> bool:
        """期限切れLeaseだけを自動Recoveryへ渡す。"""
        return self.classification is RecoveryClassification.EXPIRED

    @property
    def requires_manual_reconciliation(self) -> bool:
        """Leaseの完全性を証明できないAttemptをEffectから隔離する。"""
        return self.classification in {
            RecoveryClassification.LEASE_MISSING,
            RecoveryClassification.LEASE_MISMATCH,
            RecoveryClassification.LEASE_RELEASED,
        }


@dataclass(frozen=True, slots=True)
class MaskingRecoveryDecision:
    """未終端Masking Streamに対するLease束縛済みRecovery判定。"""

    stream_id: str
    run_id: str | None
    attempt_id: str | None
    lease_id: str | None
    classification: RecoveryClassification

    @property
    def automatic_recovery_allowed(self) -> bool:
        return self.classification is RecoveryClassification.EXPIRED

    @property
    def requires_manual_reconciliation(self) -> bool:
        return self.classification in {
            RecoveryClassification.LEASE_MISSING,
            RecoveryClassification.LEASE_MISMATCH,
            RecoveryClassification.LEASE_RELEASED,
        }


def classify_masking_recovery_stream(
    *,
    stream_id: str,
    run_id: str | None,
    attempt_id: str | None,
    lease_id: str | None,
    lease: Lease | None,
    now: str,
) -> MaskingRecoveryDecision:
    """Masking Stream→Leaseの耐久束縛をFail-Closedで分類する。"""
    canonical_timestamp(now)
    if attempt_id is None or lease_id is None or lease is None:
        classification = RecoveryClassification.LEASE_MISSING
    elif lease.attempt_id != attempt_id:
        classification = RecoveryClassification.LEASE_MISMATCH
    elif lease.status is LeaseStatus.RELEASED:
        classification = RecoveryClassification.LEASE_RELEASED
    elif lease.status is LeaseStatus.EXPIRED or timestamp_seconds(now) >= timestamp_seconds(
        lease.expires_at
    ):
        classification = RecoveryClassification.EXPIRED
    else:
        classification = RecoveryClassification.ACTIVE
    return MaskingRecoveryDecision(
        stream_id=stream_id,
        run_id=run_id,
        attempt_id=attempt_id,
        lease_id=lease_id,
        classification=classification,
    )


def classify_recovery_candidate(
    attempt: ActionAttempt, lease: Lease | None, *, now: str
) -> RecoveryDecision:
    """Action AttemptをLease期限へ束縛してRecovery可否を返す。

    実行中のACTIVE Leaseを持つAttemptは、終端Eventを持たなくてもWorkerが
    正常実行中であり得る。従ってRecovery候補に含めない。Leaseがない、または
    Attemptへの束縛を確認できない場合も、期限切れと推測してはならない。
    """
    canonical_timestamp(now)
    if attempt.state in ACTION_ATTEMPT_TERMINAL_STATES:
        return RecoveryDecision(
            attempt.attempt_id, RecoveryClassification.TERMINAL, attempt.lease_id
        )
    if attempt.lease_id is None:
        return RecoveryDecision(attempt.attempt_id, RecoveryClassification.NOT_LEASED, None)
    if lease is None:
        return RecoveryDecision(
            attempt.attempt_id, RecoveryClassification.LEASE_MISSING, attempt.lease_id
        )
    if lease.lease_id != attempt.lease_id or lease.attempt_id != attempt.attempt_id:
        return RecoveryDecision(
            attempt.attempt_id, RecoveryClassification.LEASE_MISMATCH, attempt.lease_id
        )
    if lease.status is LeaseStatus.RELEASED:
        return RecoveryDecision(
            attempt.attempt_id, RecoveryClassification.LEASE_RELEASED, attempt.lease_id
        )
    if lease.status is LeaseStatus.EXPIRED or timestamp_seconds(now) >= timestamp_seconds(
        lease.expires_at
    ):
        return RecoveryDecision(
            attempt.attempt_id, RecoveryClassification.EXPIRED, attempt.lease_id
        )
    return RecoveryDecision(attempt.attempt_id, RecoveryClassification.ACTIVE, attempt.lease_id)
