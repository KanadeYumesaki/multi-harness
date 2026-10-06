"""Persisted admission distinguishes concurrent losers from later replays."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from harness.domain.hashing import ContentHash
from harness.ports.approval import ApprovalConsumeRequest


@dataclass(frozen=True, slots=True)
class ConsumeTicket:
    ticket_id: str
    grant_id: str
    attempt_id: str
    concurrency_group: str
    request_hash: ContentHash
    state: str = "ADMITTED"


@dataclass(frozen=True, slots=True)
class ConsumeOutcome:
    ticket_id: str
    grant_id: str
    concurrency_group: str
    successful_consumes: int
    failed_consumes: int
    stream_id: str
    head_before: int
    result_hash: ContentHash | None = None
    consume_result_id: str | None = None


class ConsumeStorePort(Protocol):
    def register(self, ticket: ConsumeTicket) -> None: ...
    def get(self, ticket_id: str) -> ConsumeTicket | None: ...
    def winner(self, concurrency_group: str) -> ConsumeTicket | None: ...
    def finish(self, ticket_id: str, state: str) -> None: ...
    def put_result(self, result_id: str, ticket_id: str, digest: ContentHash) -> None: ...
    def result_hash(self, result_id: str) -> ContentHash | None: ...


class ApprovalConsumerPort(Protocol):
    def register_in_transaction(
        self, request: ApprovalConsumeRequest, group: str
    ) -> ConsumeTicket: ...
    def consume_in_transaction(
        self, ticket: ConsumeTicket, request: ApprovalConsumeRequest
    ) -> ConsumeOutcome: ...
