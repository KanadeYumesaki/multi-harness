"""DCR-1-A: persisted per-attempt loser results, never replay counters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, replace
from typing import Any

from harness.domain.approval import ApprovalStatus
from harness.domain.artifact import ArtifactMetadata
from harness.domain.canonical import canonicalize
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.events import EventType
from harness.domain.hashing import ContentHash, hash_bytes, hash_canonical
from harness.ports.approval import ApprovalConsumeRequest, ApprovalGrantPort
from harness.ports.approval_consume import ConsumeOutcome, ConsumeStorePort, ConsumeTicket
from harness.ports.artifact_store import ArtifactStorePort
from harness.ports.effect_execution import ClockPort
from harness.ports.event_ledger import EventLedgerPort, NewEvent
from harness.ports.unit_of_work import UnitOfWorkPort


class ApprovalConsumeCoordinator:
    def __init__(
        self,
        *,
        clock: ClockPort,
        grants: ApprovalGrantPort,
        store: ConsumeStorePort,
        artifacts: ArtifactStorePort,
        ledger: EventLedgerPort,
        uow: UnitOfWorkPort,
        schema_set_hash: ContentHash,
        validate: Callable[[str, dict[str, Any]], None],
    ) -> None:
        self.clock = clock
        self.grants, self.store, self.artifacts, self.ledger, self.uow = (
            grants,
            store,
            artifacts,
            ledger,
            uow,
        )
        self.schema_set_hash, self.validate = schema_set_hash, validate

    def register(self, request: ApprovalConsumeRequest, group: str) -> ConsumeTicket:
        with self.uow.begin_immediate():
            ticket = self.register_in_transaction(request, group)
        return ticket

    def consume(self, ticket: ConsumeTicket, request: ApprovalConsumeRequest) -> ConsumeOutcome:
        with self.uow.begin_immediate():
            outcome = self.consume_in_transaction(ticket, request)
        return outcome

    def register_in_transaction(self, request: ApprovalConsumeRequest, group: str) -> ConsumeTicket:
        self._require_transaction()
        if not group or len(group) > 128 or any(ord(c) < 32 for c in group):
            raise HarnessError(ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "invalid concurrency group")
        grant = self.grants.get(request.grant_id)
        if (
            grant is None
            or grant.status is not ApprovalStatus.ISSUED
            or grant.store_version != request.expected_store_version
        ):
            raise HarnessError(
                ErrorCode.APPROVAL_REPLAY, "admission requires the current issued grant"
            )
        # Validate authority without mutating the persisted grant.
        grant.consume(
            now=self.clock.now(),
            expected_execution_plan_hash=request.expected_execution_plan_hash,
            current_revocation_epoch=request.current_revocation_epoch,
            actor_id=request.actor_id,
            attempt_id=request.attempt_id,
            signature_valid=request.signature_valid,
        )
        digest = self._request_hash(request)
        ticket_id = str(
            hash_canonical(
                {"request_hash": str(digest), "group": group},
                artifact_type="approval-consume-ticket",
                schema_major=1,
            )
        )
        ticket = ConsumeTicket(ticket_id, request.grant_id, request.attempt_id, group, digest)
        self.store.register(ticket)
        stream = "approval:" + group
        if self.ledger.stream_head(stream) == 0:
            # Seed the dedicated stream with the actual persisted grant, never a test event.
            digest = self._put(
                {
                    "grant_id": grant.grant_id,
                    "status": grant.status.value,
                    "execution_plan_hash": str(grant.execution_plan_hash),
                    "issued_at": grant.issued_at,
                    "concurrency_group": group,
                },
                self.clock.now(),
            )
            self.ledger.append(
                [NewEvent(stream, EventType.APPROVAL_ISSUED.value, digest, self.clock.now())],
                expected_stream_sequence=0,
            )
        return ticket

    def consume_in_transaction(
        self, ticket: ConsumeTicket, request: ApprovalConsumeRequest
    ) -> ConsumeOutcome:
        self._require_transaction()
        persisted = self.store.get(ticket.ticket_id)
        if (
            persisted != ticket
            or ticket.state != "ADMITTED"
            or ticket.request_hash != self._request_hash(request)
        ):
            raise HarnessError(
                ErrorCode.APPROVAL_REPLAY, "unknown, changed, or already consumed admission"
            )
        stream = "approval:" + ticket.concurrency_group
        head = self.ledger.stream_head(stream)
        try:
            self.grants.consume(replace(request, now=self.clock.now()))
        except HarnessError as error:
            if error.code is not ErrorCode.APPROVAL_REPLAY:
                raise
            winner = self.store.winner(ticket.concurrency_group)
            grant = self.grants.get(request.grant_id)
            if (
                winner is None
                or grant is None
                or grant.status is not ApprovalStatus.CONSUMED
                or grant.attempt_id != winner.attempt_id
                or winner.grant_id != ticket.grant_id
            ):
                raise HarnessError(
                    ErrorCode.APPROVAL_REPLAY, "replay has no admitted same-group winner"
                ) from error
            result_id = ticket.ticket_id + ":result"
            stream = "approval-result:" + ticket.ticket_id
            head = self.ledger.stream_head(stream)
            record: dict[str, Any] = {
                "schema_name": "ApprovalConsumeResult",
                "schema_version": "1.0.0",
                "record_id": result_id,
                "consume_result_id": result_id,
                "grant_id": ticket.grant_id,
                "attempt_id": ticket.attempt_id,
                "concurrency_group": ticket.concurrency_group,
                "schema_set_hash": str(self.schema_set_hash),
                "state": "REJECTED",
                "error_code": "APPROVAL_REPLAY",
                "successful_consumes": 0,
                "failed_consumes": 1,
                "created_at": self.clock.now(),
                "producer": "approval-consume-coordinator/1",
            }
            record["content_hash"] = str(
                hash_canonical(record, artifact_type="approval-consume-result", schema_major=1)
            )
            self.validate("ApprovalConsumeResult", record)
            digest = self._put(record, self.clock.now())
            self.store.put_result(result_id, ticket.ticket_id, digest)
            self.store.finish(ticket.ticket_id, "REJECTED")
            self.ledger.append(
                [
                    NewEvent(
                        stream, EventType.APPROVAL_REPLAY_DENIED.value, digest, self.clock.now()
                    )
                ],
                expected_stream_sequence=head,
            )
            return ConsumeOutcome(
                ticket.ticket_id,
                ticket.grant_id,
                ticket.concurrency_group,
                0,
                1,
                stream,
                head,
                digest,
                result_id,
            )
        self.store.finish(ticket.ticket_id, "CONSUMED")
        digest = self._put(
            {
                "ticket_id": ticket.ticket_id,
                "grant_id": ticket.grant_id,
                "attempt_id": ticket.attempt_id,
                "concurrency_group": ticket.concurrency_group,
                "successful_consumes": 1,
            },
            request.now,
        )
        self.ledger.append(
            [NewEvent(stream, EventType.APPROVAL_CONSUMED.value, digest, self.clock.now())],
            expected_stream_sequence=head,
        )
        return ConsumeOutcome(
            ticket.ticket_id, ticket.grant_id, ticket.concurrency_group, 1, 0, stream, head
        )

    def _put(self, document: dict[str, Any], now: str) -> ContentHash:
        payload = canonicalize(document)
        digest = hash_bytes(payload)
        self.artifacts.put(
            payload,
            ArtifactMetadata("application/json", len(payload), "SYNTHETIC", "UNTRUSTED_INPUT"),
            artifact_id=str(digest),
            stored_at=now,
        )
        return digest

    def _require_transaction(self) -> None:
        if not self.uow.in_transaction():
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH, "approval consumption requires caller transaction"
            )

    @staticmethod
    def _request_hash(request: ApprovalConsumeRequest) -> ContentHash:
        values = asdict(request)
        values["expected_execution_plan_hash"] = str(request.expected_execution_plan_hash)
        return hash_canonical(values, artifact_type="approval-consume-request", schema_major=1)
