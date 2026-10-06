"""LeaseとFencing TokenのDomain規則。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.timestamps import canonical_timestamp, timestamp_seconds

__all__ = ["Lease", "LeaseStatus", "require_current_fencing_token"]


class LeaseStatus(Enum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class Lease:
    """Resource、Holder、Attemptへ束縛された期限付きLease。"""

    lease_id: str
    resource_key: str
    holder_id: str
    attempt_id: str
    fencing_token: int
    issued_at: str
    expires_at: str
    renewed_at: str
    status: LeaseStatus
    store_version: int

    def __post_init__(self) -> None:
        if not all((self.lease_id, self.resource_key, self.holder_id, self.attempt_id)):
            raise ValueError("lease identity fields must not be empty")
        if self.fencing_token < 1 or self.store_version < 1:
            raise ValueError("fencing_token and store_version must be >= 1")
        canonical_timestamp(self.issued_at)
        canonical_timestamp(self.expires_at)
        canonical_timestamp(self.renewed_at)
        if timestamp_seconds(self.expires_at) <= timestamp_seconds(self.issued_at):
            raise ValueError("expires_at must be later than issued_at")
        if (
            not timestamp_seconds(self.issued_at)
            <= timestamp_seconds(self.renewed_at)
            <= timestamp_seconds(self.expires_at)
        ):
            raise ValueError("renewed_at must be inside the lease period")

    def renew(self, *, now: str, expires_at: str, current_token: int) -> Lease:
        """同一Holder／Attemptだけが現在Tokenで更新できる。"""
        require_current_fencing_token(self, current_token=current_token, now=now)
        canonical_timestamp(expires_at)
        if timestamp_seconds(expires_at) <= timestamp_seconds(now):
            raise ValueError("renewed expiry must be in the future")
        return replace(
            self, expires_at=expires_at, renewed_at=now, store_version=self.store_version + 1
        )

    def release(self, *, now: str, current_token: int) -> Lease:
        require_current_fencing_token(self, current_token=current_token, now=now)
        return replace(self, status=LeaseStatus.RELEASED, store_version=self.store_version + 1)


def require_current_fencing_token(lease: Lease, *, current_token: int, now: str) -> None:
    """最終Effect実行点が呼び出すFail-Closed検証。"""
    canonical_timestamp(now)
    if lease.status is not LeaseStatus.ACTIVE:
        raise HarnessError(ErrorCode.STALE_FENCING_TOKEN, "lease is no longer active")
    if current_token != lease.fencing_token:
        raise HarnessError(ErrorCode.STALE_FENCING_TOKEN, "fencing token is stale")
    if timestamp_seconds(now) >= timestamp_seconds(lease.expires_at):
        raise HarnessError(ErrorCode.STALE_FENCING_TOKEN, "lease has expired")
