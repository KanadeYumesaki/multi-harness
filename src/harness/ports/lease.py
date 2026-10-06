"""LeaseとFencing TokenのPort契約。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from harness.domain.lease import Lease

__all__ = ["LeaseAcquireRequest", "LeasePort"]


@dataclass(frozen=True, slots=True)
class LeaseAcquireRequest:
    resource_key: str
    holder_id: str
    attempt_id: str
    issued_at: str
    expires_at: str


class LeasePort(Protocol):
    def acquire(self, request: LeaseAcquireRequest) -> Lease:
        """Resourceごとに単調増加するFencing Tokenを発行する。"""
        ...

    def get(self, lease_id: str) -> Lease | None: ...

    def current_fencing_token(self, resource_key: str) -> int:
        """最終Effect実行直前に照合する現在Tokenを返す。"""
        ...

    def renew(self, lease: Lease, *, now: str, expires_at: str) -> Lease: ...

    def release(self, lease: Lease, *, now: str) -> Lease: ...
