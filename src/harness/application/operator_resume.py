"""Explicit local operator resume and reconciliation. No effect retry.

Review, signed approval consumption, state transition and ledger append share one
SQLite transaction. CAS bytes can be orphaned on rollback, never referenced without
that commit. This is an administrative approval, not a Runtime GO or Release.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timedelta
from typing import Any

from harness.domain.artifact import ArtifactMetadata
from harness.domain.canonical import canonicalize
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.events import EventType
from harness.domain.hashing import ContentHash, hash_canonical
from harness.ports.approval import ApprovalConsumeRequest, ApprovalGrantPort
from harness.ports.approval_consume import ApprovalConsumerPort
from harness.ports.artifact_store import ArtifactStorePort
from harness.ports.effect_execution import ClockPort
from harness.ports.event_ledger import EventLedgerPort, NewEvent
from harness.ports.local_workflow import LocalAuthorityPort
from harness.ports.operation_control import OperationControlTransaction, OperationOwnerPort
from harness.ports.unit_of_work import UnitOfWorkPort


class OperatorResumeService:
    def __init__(
        self,
        *,
        control: OperationControlTransaction,
        owners: OperationOwnerPort,
        identity: str,
        authority: LocalAuthorityPort,
        clock: ClockPort,
        uow: UnitOfWorkPort,
        grants: ApprovalGrantPort,
        consumer: ApprovalConsumerPort,
        artifacts: ArtifactStorePort,
        ledger: EventLedgerPort,
    ) -> None:
        self.control, self.identity = control, identity
        self.owners = owners
        self.authority, self.clock, self.uow = authority, clock, uow
        self.grants, self.consumer = grants, consumer
        self.artifacts, self.ledger = artifacts, ledger

    def _review(self, operation: str, locks: ExitStack) -> dict[str, Any]:
        if operation not in ("resume", "reconcile"):
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "unknown maintenance operation"
            )
        state = self.control.snapshot()
        reservations = [dict(row) for row in self.control.owners()]
        restore = self.control.restore_record()
        owned_rows = [*reservations, *([restore] if restore is not None else [])]
        # A single exclusive directory fence avoids conflicting with our own
        # probes when several abandoned reservations belong to the same DB.
        claim = locks.enter_context(self.owners.fence()) if owned_rows else None
        for row in owned_rows:
            if claim is None or row.get("owner_scope") != claim.scope:
                row["ownership"] = "UNKNOWN"
            else:
                row["ownership"] = "UNHELD" if claim.status == "ACQUIRED" else claim.status
        state["reservations"] = reservations
        state["restore"] = restore
        blockers = []
        if not state["ledger_valid"]:
            blockers.append("LEDGER_VERIFICATION_FAILED")
        if any(
            not self.artifacts.verify(ContentHash.parse(ref)).ok
            for ref in self.control.artifact_references()
        ):
            blockers.append("AUDIT_ARTIFACTS_UNVERIFIED")
        if state["unsettled_records"]:
            blockers.append("UNSETTLED_JOURNALS_OR_APPROVALS")
        if operation == "resume":
            if state["mode"] != "DRAINING":
                blockers.append("REQUIRES_DRAINING")
            if state["active_reservations"]:
                blockers.append("ACTIVE_OR_UNRECONCILED_RESERVATIONS")
            if restore is not None:
                blockers.append("RESTORE_RECONCILIATION_REQUIRED")
        else:
            if state["mode"] not in ("DRAINING", "RESTORING"):
                blockers.append("STOP_INTAKE_BEFORE_RECONCILIATION")
            if not reservations and state["mode"] != "RESTORING":
                blockers.append("NOTHING_TO_RECONCILE")
            if any(row["ownership"] != "UNHELD" for row in reservations):
                blockers.append("RESERVATION_OWNER_ACTIVE_OR_UNKNOWN")
            if state["mode"] == "RESTORING":
                if restore is None:
                    blockers.append("LEGACY_RESTORE_WITHOUT_SOURCE_BINDING")
                elif restore["ownership"] != "UNHELD" or restore["purpose"] != "VERIFY_COPY":
                    blockers.append("RESTORE_OWNER_ACTIVE_OR_UNVERIFIED_PURPOSE")
                elif restore["source_hash"] != state["source_hash"]:
                    blockers.append("RESTORE_SOURCE_CHANGED")
            elif restore is not None:
                blockers.append("RESTORE_STATE_INCONSISTENT")
        target = "OPEN" if operation == "resume" else "DRAINING"
        reason = "maintenance-complete" if operation == "resume" else "reconcile-quiescent-source"
        plan = {
            "contract": "operator-" + operation + "/1",
            "source": self.identity,
            "operation": "RESUME_INTAKE" if operation == "resume" else "RECONCILE_OPERATIONS",
            "target_mode": target,
            "reason": reason,
            "snapshot": state,
        }
        digest = hash_canonical(plan, artifact_type="operator-resume-review", schema_major=1)
        return {
            **plan,
            "review_hash": str(digest),
            "eligible": not blockers,
            "blockers": blockers,
            "resumed": False,
        }

    def inspect(self, operation: str = "resume") -> dict[str, Any]:
        """One consistent observation, not a reservation and not a resume."""
        review = None
        with ExitStack() as locks, self.uow.begin_immediate():
            review = self._review(operation, locks)
        if review is None:
            raise HarnessError(ErrorCode.STORAGE_WRITE_FAILED, "inspection did not complete")
        return review

    def resume(self, *, review_hash: str, auth_session: str, reason: str) -> dict[str, Any]:
        return self._execute(
            operation="resume", review_hash=review_hash, auth_session=auth_session, reason=reason
        )

    def reconcile(self, *, review_hash: str, auth_session: str, reason: str) -> dict[str, Any]:
        return self._execute(
            operation="reconcile", review_hash=review_hash, auth_session=auth_session, reason=reason
        )

    def _execute(
        self, *, operation: str, review_hash: str, auth_session: str, reason: str
    ) -> dict[str, Any]:
        expected_reason = (
            "maintenance-complete" if operation == "resume" else "reconcile-quiescent-source"
        )
        if reason != expected_reason:
            raise HarnessError(ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "unknown maintenance reason")
        try:
            expected = ContentHash.parse(review_hash)
        except ValueError as exc:
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "invalid review hash"
            ) from exc
        subject = self.authority.subject(auth_session)
        committed = None
        # Keep acquired owner locks until after the database commit.
        with ExitStack() as locks, self.uow.begin_immediate():
            review = self._review(operation, locks)
            if review["review_hash"] != str(expected):
                raise HarnessError(
                    ErrorCode.APPROVAL_INVALIDATED, "state changed; inspect and approve again"
                )
            if not review["eligible"]:
                raise HarnessError(
                    ErrorCode.DEPLOY_DRAIN_REQUIRED,
                    "resume blocked: " + ", ".join(review["blockers"]),
                )
            now = self.clock.now()
            expires_at = (
                datetime.fromisoformat(now.replace("Z", "+00:00")) + timedelta(seconds=60)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            operation_id = operation + ":" + expected.hexdigest
            scope = (str(review["operation"]),)
            grant, public_key = self.authority.issue(
                run_id=operation_id,
                plan_content_hash=expected,
                execution_plan_hash=expected,
                scope=scope,
                subject=subject,
                now=now,
                expires_at=expires_at,
            )
            if (
                grant.action_scope != scope
                or grant.plan_content_hash != expected
                or grant.execution_plan_hash != expected
                or grant.approver_subject_id != subject
            ):
                raise HarnessError(
                    ErrorCode.APPROVAL_INVALIDATED, "resume authority binding differs"
                )
            self.grants.issue(grant)
            request = ApprovalConsumeRequest(
                grant.grant_id,
                expected,
                0,
                subject,
                operation_id + ":attempt",
                now,
                self.authority.verify(grant, public_key),
                grant.store_version,
            )
            ticket = self.consumer.register_in_transaction(request, operation_id)
            outcome = self.consumer.consume_in_transaction(ticket, request)
            if outcome.successful_consumes != 1:
                raise HarnessError(ErrorCode.APPROVAL_REPLAY, "resume approval was not consumed")
            # The write lock covers the observation, approval CAS and final mode write.
            if operation == "reconcile":
                for row in review["snapshot"]["reservations"]:
                    self.control.release(row["token"])
                restore = review["snapshot"]["restore"]
                if restore is not None:
                    self.control.finish_restore(restore["token"], "ABANDONED")
            self.control.set_mode(review["target_mode"])
            result = {
                "contract": "operator-" + operation + "-result/1",
                "review": review,
                "grant_id": grant.grant_id,
                "public_key": public_key,
                "subject": subject,
                "ticket_id": ticket.ticket_id,
                "operation_id": operation_id,
                "mode": review["target_mode"],
                "resumed": operation == "resume",
                "reconciled": operation == "reconcile",
                "restore_verified": False if operation == "reconcile" else None,
                "recorded_at": now,
            }
            data = canonicalize(result)
            artifact = self.artifacts.put(
                data,
                ArtifactMetadata("application/json", len(data), "INTERNAL", "SYSTEM_GENERATED"),
                artifact_id=operation_id + ":receipt",
                stored_at=now,
            )
            self.ledger.append(
                [
                    NewEvent(operation_id, event.value, artifact.content_hash, now)
                    for event in (EventType.RECOVERY_STARTED, EventType.RECOVERY_DECIDED)
                ],
                expected_stream_sequence=0,
            )
            committed = {**result, "receipt_hash": str(artifact.content_hash)}
        if committed is None:
            raise HarnessError(ErrorCode.STORAGE_WRITE_FAILED, "resume did not complete")
        return committed
