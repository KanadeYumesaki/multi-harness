"""Preview invocation journal: a remote result hash cannot be known before sending.

This contract does not replace the local file OperationJournal, whose expected
output is known before the write. Every transition must be durably persisted by
the Application before the next external effect. An attempted call is never retried.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.events import EventType
from harness.domain.hashing import ContentHash


class InvocationState(Enum):
    PREPARED_DURABLE = "PREPARED_DURABLE"
    EXECUTION_ATTEMPTED = EventType.EXECUTION_ATTEMPTED.value
    RESPONSE_CAPTURED = "RESPONSE_CAPTURED"
    EFFECT_UNKNOWN = "EFFECT_UNKNOWN"


@dataclass(frozen=True, slots=True)
class CliInvocationJournal:
    invocation_id: str
    execution_plan_hash: ContentHash
    request_hash: ContentHash
    runtime_hash: ContentHash
    state: InvocationState = InvocationState.PREPARED_DURABLE
    response_hash: ContentHash | None = None
    store_version: int = 0

    def __post_init__(self) -> None:
        if not self.invocation_id or self.store_version < 0:
            raise ValueError("invalid invocation identity or version")
        if not isinstance(self.state, InvocationState):
            raise ValueError("invalid invocation state")
        if (self.state is InvocationState.RESPONSE_CAPTURED) != (self.response_hash is not None):
            raise ValueError("response hash must describe a captured response only")

    def attempted(self) -> CliInvocationJournal:
        return self._transition(
            InvocationState.PREPARED_DURABLE, InvocationState.EXECUTION_ATTEMPTED
        )

    def captured(self, response_hash: ContentHash) -> CliInvocationJournal:
        return self._transition(
            InvocationState.EXECUTION_ATTEMPTED,
            InvocationState.RESPONSE_CAPTURED,
            response_hash=response_hash,
        )

    def unknown(self) -> CliInvocationJournal:
        return self._transition(InvocationState.EXECUTION_ATTEMPTED, InvocationState.EFFECT_UNKNOWN)

    def _transition(
        self,
        expected: InvocationState,
        target: InvocationState,
        *,
        response_hash: ContentHash | None = None,
    ) -> CliInvocationJournal:
        if self.state is not expected:
            raise HarnessError(
                ErrorCode.EFFECT_UNKNOWN, "CLI invocation cannot repeat or skip a stage"
            )
        return replace(
            self, state=target, response_hash=response_hash, store_version=self.store_version + 1
        )
