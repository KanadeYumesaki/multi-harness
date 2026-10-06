"""Hold the single-DB write lock while the final storage boundary uses its fence."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from harness.domain.effect import EffectState
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash
from harness.domain.lease import require_current_fencing_token
from harness.ports.effect import EffectJournalPort
from harness.ports.effect_execution import ClockPort
from harness.ports.lease import LeasePort
from harness.ports.unit_of_work import UnitOfWorkPort


@dataclass(frozen=True, slots=True)
class StorageFenceGuard:
    leases: LeasePort
    journals: EffectJournalPort
    unit_of_work: UnitOfWorkPort
    clock: ClockPort
    lease_id: str
    resource_key: str
    fencing_token: int

    @contextmanager
    def authorize(
        self,
        *,
        effect_id: str,
        relative_path: str,
        before_hash: ContentHash,
        after_hash: ContentHash,
    ) -> Iterator[None]:
        with self.unit_of_work.begin_immediate():
            lease = self.leases.get(self.lease_id)
            journal = self.journals.get_by_effect_id(effect_id)
            if lease is None or lease.resource_key != self.resource_key:
                raise HarnessError(ErrorCode.STALE_FENCING_TOKEN, "storage lease is unavailable")
            require_current_fencing_token(
                lease,
                current_token=self.leases.current_fencing_token(self.resource_key),
                now=self.clock.now(),
            )
            if lease.fencing_token != self.fencing_token:
                raise HarnessError(ErrorCode.STALE_FENCING_TOKEN, "storage fence changed")
            if (
                journal is None
                or journal.state is not EffectState.EXECUTION_ATTEMPTED
                or journal.receipt_id is not None
                or journal.fencing_token != lease.fencing_token
                or journal.attempt_id != lease.attempt_id
                or journal.target_resource_identity != self.resource_key
                or journal.before_hash != before_hash
                or journal.expected_after_hash != after_hash
                or not self.resource_key.endswith(":" + relative_path)
            ):
                raise HarnessError(
                    ErrorCode.EVENT_ORDER_VIOLATION, "storage journal does not authorize effect"
                )
            yield
