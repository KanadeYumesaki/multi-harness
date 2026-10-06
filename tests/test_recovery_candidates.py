"""Lease期限に束縛したRecovery候補選別の回帰試験。"""

from __future__ import annotations

from harness.application.recovery_service import RecoveryCandidateService
from harness.domain.attempt import ActionAttempt
from harness.domain.lease import Lease, LeaseStatus
from harness.domain.recovery import RecoveryClassification

NOW = "2026-08-15T00:05:00Z"


class StubLeasePort:
    def __init__(self, leases: tuple[Lease, ...]) -> None:
        self._leases = {lease.lease_id: lease for lease in leases}

    def get(self, lease_id: str) -> Lease | None:
        return self._leases.get(lease_id)


def _attempt(attempt_id: str, *, lease_id: str | None, state: str = "RUNNING") -> ActionAttempt:
    return ActionAttempt(
        attempt_id=attempt_id,
        action_id=f"action-{attempt_id}",
        attempt_number=1,
        state=state,
        lease_id=lease_id,
        started_at="2026-08-15T00:00:00Z" if state == "RUNNING" else None,
        ended_at="2026-08-15T00:06:00Z" if state == "FAILED_PERMANENT" else None,
        error_classification="TEST" if state == "FAILED_PERMANENT" else None,
    )


def _lease(
    lease_id: str, attempt_id: str, *, expires_at: str, status: LeaseStatus = LeaseStatus.ACTIVE
) -> Lease:
    return Lease(
        lease_id=lease_id,
        resource_key=f"resource-{attempt_id}",
        holder_id="worker-1",
        attempt_id=attempt_id,
        fencing_token=1,
        issued_at="2026-08-15T00:00:00Z",
        expires_at=expires_at,
        renewed_at="2026-08-15T00:00:00Z",
        status=status,
        store_version=1,
    )


def test_active_attempt_is_not_returned_as_recovery_candidate() -> None:
    active = _attempt("attempt-active", lease_id="lease-active")
    service = RecoveryCandidateService(
        StubLeasePort(
            (_lease("lease-active", active.attempt_id, expires_at="2026-08-15T00:10:00Z"),)
        )
    )

    decisions = service.classify((active,), now=NOW)

    assert decisions[0].classification is RecoveryClassification.ACTIVE
    assert not decisions[0].automatic_recovery_allowed
    assert service.expired_candidates((active,), now=NOW) == ()


def test_only_expired_attempt_is_returned_as_recovery_candidate() -> None:
    expired = _attempt("attempt-expired", lease_id="lease-expired")
    active = _attempt("attempt-active", lease_id="lease-active")
    service = RecoveryCandidateService(
        StubLeasePort(
            (
                _lease("lease-expired", expired.attempt_id, expires_at="2026-08-15T00:04:59Z"),
                _lease("lease-active", active.attempt_id, expires_at="2026-08-15T00:10:00Z"),
            )
        )
    )

    candidates = service.expired_candidates((active, expired), now=NOW)

    assert [(item.attempt_id, item.classification) for item in candidates] == [
        ("attempt-expired", RecoveryClassification.EXPIRED)
    ]


def test_missing_mismatched_or_released_lease_requires_manual_reconciliation() -> None:
    missing = _attempt("attempt-missing", lease_id="lease-missing")
    mismatched = _attempt("attempt-mismatched", lease_id="lease-mismatched")
    released = _attempt("attempt-released", lease_id="lease-released")
    service = RecoveryCandidateService(
        StubLeasePort(
            (
                _lease(
                    "lease-mismatched",
                    "different-attempt",
                    expires_at="2026-08-15T00:04:59Z",
                ),
                _lease(
                    "lease-released",
                    released.attempt_id,
                    expires_at="2026-08-15T00:10:00Z",
                    status=LeaseStatus.RELEASED,
                ),
            )
        )
    )

    decisions = service.classify((missing, mismatched, released), now=NOW)

    assert [item.classification for item in decisions] == [
        RecoveryClassification.LEASE_MISSING,
        RecoveryClassification.LEASE_MISMATCH,
        RecoveryClassification.LEASE_RELEASED,
    ]
    assert all(item.requires_manual_reconciliation for item in decisions)
    assert service.expired_candidates((missing, mismatched, released), now=NOW) == ()


def test_terminal_attempt_is_never_a_recovery_candidate() -> None:
    terminal = _attempt("attempt-terminal", lease_id="lease-expired", state="FAILED_PERMANENT")
    service = RecoveryCandidateService(
        StubLeasePort(
            (_lease("lease-expired", terminal.attempt_id, expires_at="2026-08-15T00:04:59Z"),)
        )
    )

    decision = service.classify((terminal,), now=NOW)[0]

    assert decision.classification is RecoveryClassification.TERMINAL
    assert not decision.automatic_recovery_allowed
