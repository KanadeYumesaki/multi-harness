"""Approval Grant永続化のPort契約。

Repository実装は署名検証済みGrantの発行と、`status=ISSUED`・Nonce一意性・
store_versionを条件にしたCAS消費を同一SQLite Transactionで行う。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from harness.domain.approval import ApprovalGrant
from harness.domain.hashing import ContentHash

__all__ = ["ApprovalConsumeRequest", "ApprovalGrantPort"]


@dataclass(frozen=True, slots=True)
class ApprovalConsumeRequest:
    grant_id: str
    expected_execution_plan_hash: ContentHash
    current_revocation_epoch: int
    actor_id: str
    attempt_id: str
    now: str
    signature_valid: bool
    expected_store_version: int


class ApprovalGrantPort(Protocol):
    def issue(self, grant: ApprovalGrant) -> None:
        """NonceとGrant IDの一意制約を満たすISSUED Grantを保存する。"""
        ...

    def get(self, grant_id: str) -> ApprovalGrant | None: ...

    def consume(self, request: ApprovalConsumeRequest) -> ApprovalGrant:
        """ISSUED状態からCONSUMEDへの一回限りCASを実行する。"""
        ...

    def invalidate(
        self, *, grant_id: str, expected_store_version: int, now: str, reason: str
    ) -> ApprovalGrant: ...
