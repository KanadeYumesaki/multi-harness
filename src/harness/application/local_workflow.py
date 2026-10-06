"""Offline local workflow. Application owns every DB transaction and state transition."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from typing import Any

from harness.domain.artifact import ArtifactMetadata
from harness.domain.attempt import ActionAttempt
from harness.domain.canonical import canonicalize
from harness.domain.effect import EffectState, OperationJournal
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.events import EventType
from harness.domain.hashing import ContentHash, hash_bytes, hash_canonical
from harness.domain.input_read import CapabilityScope
from harness.domain.lease import Lease
from harness.domain.plan import PlanAction
from harness.domain.plan_build_payload import freeze_plan_request, read_plan_request
from harness.domain.run_terminal import RunTerminalRequest, evaluate_run_terminal
from harness.domain.timestamps import timestamp_seconds
from harness.ports.approval import ApprovalConsumeRequest, ApprovalGrantPort
from harness.ports.approval_consume import ApprovalConsumerPort
from harness.ports.artifact_store import ArtifactStorePort
from harness.ports.effect import EffectJournalPort
from harness.ports.effect_execution import ClockPort
from harness.ports.event_ledger import EventLedgerPort, NewEvent
from harness.ports.lease import LeaseAcquireRequest, LeasePort
from harness.ports.local_workflow import (
    AttemptStorePort,
    LocalAuthorityPort,
    LocalWorkspacePort,
    WorkflowRecord,
    WorkflowStorePort,
)
from harness.ports.operation_control import OperationAdmissionPort
from harness.ports.plan_builder import PlanBuilderPort
from harness.ports.provider import ProviderPort, ProviderRequest
from harness.ports.storage_commit import StorageCommitGuardPort
from harness.ports.unit_of_work import UnitOfWorkPort


class LocalWorkflowService:
    def __init__(
        self,
        *,
        runs: WorkflowStorePort,
        artifacts: ArtifactStorePort,
        ledger: EventLedgerPort,
        uow: UnitOfWorkPort,
        clock: ClockPort,
        workspace: LocalWorkspacePort,
        authority: LocalAuthorityPort,
        grants: ApprovalGrantPort,
        approval_consumer: ApprovalConsumerPort,
        leases: LeasePort,
        journals: EffectJournalPort,
        attempts: AttemptStorePort,
        builder: PlanBuilderPort,
        provider: ProviderPort,
        guard_factory: Callable[[Lease], StorageCommitGuardPort],
        runtime_identity: Callable[[], str],
        executable_digest: Callable[[str], ContentHash],
        validate_record: Callable[[str, dict[str, Any]], None],
        operation_gate: OperationAdmissionPort | None = None,
    ) -> None:
        self.operation_gate = operation_gate
        self.runs, self.artifacts, self.ledger, self.uow = runs, artifacts, ledger, uow
        self.clock, self.workspace, self.authority = clock, workspace, authority
        self.approval_consumer = approval_consumer
        self.grants, self.leases, self.journals, self.attempts = grants, leases, journals, attempts
        self.builder, self.provider, self.guard_factory = builder, provider, guard_factory
        self.runtime_identity = runtime_identity
        self.executable_digest = executable_digest
        self.validate_record = validate_record

    def capture_input_snapshot(self) -> dict[str, str]:
        """Bind the bytes before the task/context readers begin."""
        return self.workspace.snapshot()

    def create(
        self,
        frozen_request: bytes,
        *,
        expected_input_snapshot: dict[str, str],
        input_artifact_hash: ContentHash,
        model: str,
        executable_path: str,
        executable_hash: str,
        allowed_prefixes: tuple[str, ...],
        token_profile_expires_at: str,
    ) -> dict[str, Any]:
        with (
            self.operation_gate.admission("LocalWorkflowService.create", kind="intake")
            if self.operation_gate is not None
            else nullcontext()
        ):
            build, authority = read_plan_request(frozen_request)
            if len(build.normalized_actions) != 1:
                raise HarnessError(
                    ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                    "local workflow requires one file action",
                )
            action = build.normalized_actions[0]
            target = action.normalized_outputs.get("target_relative_path")
            if action.action_type != "LOCAL_FILE_WRITE" or not isinstance(target, str):
                raise HarnessError(
                    ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "resolved local file target is required"
                )
            if not CapabilityScope(allowed_prefixes).permits(target):
                raise HarnessError(
                    ErrorCode.PATH_OUTSIDE_CAPABILITY, "write target exceeds declared scope"
                )
            snapshot = self.workspace.snapshot()
            if snapshot != expected_input_snapshot:
                raise HarnessError(
                    ErrorCode.APPROVAL_INVALIDATED, "worktree changed while assembling input"
                )
            if target not in snapshot:
                raise HarnessError(
                    ErrorCode.PATH_OUTSIDE_CAPABILITY, "target must already exist in the worktree"
                )
            bound_action = PlanAction(
                action.action_type,
                {**action.normalized_inputs, "expected_before_hash": snapshot[target]},
                action.normalized_outputs,
                action.dependency_keys,
            )
            snapshot_hash = hash_canonical(
                snapshot, artifact_type="local-worktree-snapshot", schema_major=1
            )
            frozen = freeze_plan_request(
                replace(
                    build, workspace_snapshot_hash=snapshot_hash, normalized_actions=(bound_action,)
                ),
                authority,
            )
            plan = self.builder.build(frozen)
            self._require_time(authority.expires_at)
            document: dict[str, Any] = {
                "run_id": authority.run_id,
                "state": "PLANNED",
                "version": 0,
                "frozen_request": json.loads(frozen),
                "plan_content_hash": str(plan.plan_content_hash),
                "execution_plan_hash": str(plan.execution_plan_hash),
                "expires_at": authority.expires_at,
                "workspace_identity": self.workspace.identity(),
                "snapshot": snapshot,
                "target": target,
                "before_hash": snapshot[target],
                "before_size": len(self.workspace.read(target)),
                "allowed_prefixes": list(allowed_prefixes),
                "token_profile_expires_at": token_profile_expires_at,
                "model": model,
                "input_artifact_hash": str(input_artifact_hash),
                "context_bundle_hash": str(build.context_bundle_hash),
                "instruction_hash": str(build.intent_hash),
                "runtime_identity": self.runtime_identity(),
                "executable_path": executable_path,
                "executable_hash": executable_hash,
                "evaluation_policy": {
                    "version": "local-evaluation/1",
                    "maximum_diff_bytes": 16384,
                    "format": "JSON",
                    "forbidden_patterns": ["BEGIN PRIVATE KEY"],
                },
            }
            # Evaluation policy and runtime identity are part of the approval-bound input.
            build, authority = read_plan_request(frozen)
            policy_hash = hash_canonical(
                {
                    "base_policy": str(build.policy_snapshot_hash),
                    "evaluation": document["evaluation_policy"],
                    "runtime": document["runtime_identity"],
                },
                artifact_type="local-workflow-policy",
                schema_major=1,
            )
            frozen = freeze_plan_request(
                replace(build, policy_snapshot_hash=policy_hash), authority
            )
            plan = self.builder.build(frozen)
            document.update(
                frozen_request=json.loads(frozen),
                plan_content_hash=str(plan.plan_content_hash),
                execution_plan_hash=str(plan.execution_plan_hash),
            )
            with self.uow.begin_immediate():
                self._save(document, "PLANNED", EventType.RUN_CREATED)
                self._save(document, "PLANNED", EventType.INTENT_CREATED)
                self._save(document, "PLANNED", EventType.PLAN_RESOLVED)
                document["policy_decision"] = {"outcome": "ALLOW", "approval_required": True}
                self._save(document, "PLANNED", EventType.POLICY_DECIDED)
            return document

    def inspect(self, run_id: str) -> dict[str, Any]:
        record = self.runs.get(run_id)
        if record is None:
            raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "run was not found")
        document = self._load_artifact(record.document_hash)
        if (document.get("run_id"), document.get("state"), document.get("version")) != (
            record.run_id,
            record.state,
            record.version,
        ):
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT,
                "run projection does not match immutable record",
            )
        stream = self.ledger.load_stream(run_id)
        if (
            not self.ledger.verify_chain(run_id).valid
            or not stream
            or stream[-1].payload_hash != record.document_hash
        ):
            raise HarnessError(
                ErrorCode.EVENT_ORDER_VIOLATION, "run projection is not bound to ledger head"
            )
        plan = self.builder.build(canonicalize(document["frozen_request"]))
        if (
            str(plan.execution_plan_hash) != document["execution_plan_hash"]
            or str(plan.plan_content_hash) != document["plan_content_hash"]
        ):
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT, "stored plan does not reproduce"
            )
        return document

    def approve(self, run_id: str, *, plan_hash: str, auth_session: str) -> dict[str, Any]:
        with (
            self.operation_gate.admission("LocalWorkflowService.approve", kind="effect")
            if self.operation_gate is not None
            else nullcontext()
        ):
            subject = self.authority.subject(auth_session)
            document = self.inspect(run_id)
            self._state(document, "PLANNED")
            if document["execution_plan_hash"] != plan_hash:
                raise HarnessError(
                    ErrorCode.APPROVAL_INVALIDATED, "approval names a different execution plan"
                )
            self._require_current(document, unchanged=True)
            grant, public_key = self.authority.issue(
                run_id=run_id,
                plan_content_hash=ContentHash.parse(document["plan_content_hash"]),
                execution_plan_hash=ContentHash.parse(plan_hash),
                scope=(document["target"],),
                subject=subject,
                now=self.clock.now(),
                expires_at=document["expires_at"],
            )
            with self.uow.begin_immediate():
                self.grants.issue(grant)
                document.update(grant_id=grant.grant_id, approver=subject, public_key=public_key)
                self._save(document, "APPROVED", EventType.APPROVAL_ISSUED)
            return document

    def run(self, run_id: str) -> dict[str, Any]:
        with (
            self.operation_gate.admission("LocalWorkflowService.run", kind="effect")
            if self.operation_gate is not None
            else nullcontext()
        ):
            document = self.inspect(run_id)
            self._state(document, "APPROVED")
            self._require_current(document, unchanged=True)
            grant = self.grants.get(document["grant_id"])
            if grant is None or grant.action_scope != (document["target"],):
                raise HarnessError(ErrorCode.APPROVAL_REQUIRED, "matching approval is required")
            attempt_id, action_id = run_id + ":attempt", run_id + ":local-file"
            resource = document["workspace_identity"] + ":" + document["target"]
            with self.uow.begin_immediate():
                consume_request = ApprovalConsumeRequest(
                    grant.grant_id,
                    ContentHash.parse(document["execution_plan_hash"]),
                    0,
                    document["approver"],
                    attempt_id,
                    self.clock.now(),
                    self.authority.verify(grant, document["public_key"]),
                    grant.store_version,
                )
                ticket = self.approval_consumer.register_in_transaction(consume_request, run_id)
                consumed = self.approval_consumer.consume_in_transaction(ticket, consume_request)
                if consumed.successful_consumes != 1:
                    raise HarnessError(ErrorCode.APPROVAL_REPLAY, "approval consumption lost")
                document["approval_ticket_id"] = ticket.ticket_id
                lease = self.leases.acquire(
                    LeaseAcquireRequest(
                        resource,
                        "local-worker",
                        attempt_id,
                        self.clock.now(),
                        document["expires_at"],
                    )
                )
                attempt = ActionAttempt(attempt_id, action_id, 1).with_plan(
                    ContentHash.parse(document["plan_content_hash"]),
                    ContentHash.parse(document["execution_plan_hash"]),
                )
                attempt = attempt.ready().claim(
                    worker_id="local-worker", claim_id=run_id + ":claim"
                )
                attempt = attempt.with_lease(
                    lease_id=lease.lease_id, fencing_token=lease.fencing_token
                )
                attempt = attempt.with_runtime_attestation(
                    ContentHash.parse(document["runtime_identity"])
                )
                attempt = attempt.start(self.clock.now())
                self.attempts.create(attempt)
                document.update(
                    lease_id=lease.lease_id,
                    fencing_token=lease.fencing_token,
                    resource=resource,
                    attempt_id=attempt_id,
                    action_id=action_id,
                )
                self._save(document, "RUNNING", EventType.APPROVAL_CONSUMED)
                self._save(document, "RUNNING", EventType.ACTION_CLAIMED)
                self._save(document, "RUNNING", EventType.LEASE_ACQUIRED)
                self._save(document, "RUNNING", EventType.RUNTIME_ATTESTED)
                self._save(document, "RUNNING", EventType.ACTION_STARTED)
            # Provider is reachable only after the durable, single-transaction approval/claim/lease.
            response = self.provider.propose(
                ProviderRequest(
                    "mock",
                    document["model"],
                    ContentHash.parse(document["context_bundle_hash"]),
                    ContentHash.parse(document["input_artifact_hash"]),
                    ContentHash.parse(document["instruction_hash"]),
                )
            )
            if (
                response.network_used
                or response.billing_mode != "FREE"
                or response.provider_id != "mock"
            ):
                raise HarnessError(
                    ErrorCode.RUNTIME_SPEC_MISMATCH, "offline provider contract violated"
                )
            self._validate_output(response.artifact_bytes, document)
            if hash_bytes(response.artifact_bytes) != response.artifact_hash:
                raise HarnessError(
                    ErrorCode.ARTIFACT_CONTENT_CONFLICT, "provider artifact hash mismatch"
                )
            with self.uow.begin_immediate():
                document["proposed_artifact_hash"] = str(self._put(response.artifact_bytes))
                self._save(document, "RUNNING", EventType.INPUT_ARTIFACT_CLASSIFIED)
            self._require_current(document, unchanged=True)
            guard = self.guard_factory(lease)
            prepared = self.workspace.prepare(
                relative_path=document["target"],
                effect_id=run_id + ":effect",
                before_hash=ContentHash.parse(document["before_hash"]),
                replacement=response.artifact_bytes,
                guard=guard,
            )
            prepared["workspace_snapshot"] = document["snapshot"]
            journal = OperationJournal(
                operation_journal_id=run_id + ":journal",
                operation_id=run_id + ":operation",
                effect_id=run_id + ":effect",
                run_id=run_id,
                action_id=action_id,
                attempt_id=attempt_id,
                operation_type="LOCAL_FILE_COMMIT",
                target_resource_identity=resource,
                fencing_token=lease.fencing_token,
                before_hash=ContentHash.parse(document["before_hash"]),
                expected_after_hash=response.artifact_hash,
                prepared_event_id=f"{run_id}:{self.ledger.stream_head(run_id) + 1}",
                prepared_at=self.clock.now(),
                durability_level="STORAGE_SYNC",
            )
            with self.uow.begin_immediate():
                self.journals.create_prepared(journal)
                updated = attempt.prepare_effect(journal.operation_journal_id)
                self.attempts.update(updated, expected_store_version=attempt.store_version)
                document.update(prepared=prepared, effect_id=journal.effect_id)
                self._save(document, "PREPARED_DURABLE", EventType.ACTION_PREPARED)
            attempted = journal.mark_execution_attempted(
                event_id=f"{run_id}:{self.ledger.stream_head(run_id) + 1}", at=self.clock.now()
            )
            with self.uow.begin_immediate():
                self.journals.update(attempted, expected_store_version=journal.store_version)
                self._save(document, "EXECUTION_ATTEMPTED", EventType.EXECUTION_ATTEMPTED)
            observed = self.workspace.commit(prepared, guard)
            return self._complete_effect(document, attempted, observed)

    def _complete_effect(
        self, document: dict[str, Any], journal: OperationJournal, observed: ContentHash
    ) -> dict[str, Any]:
        if observed != journal.expected_after_hash:
            raise HarnessError(ErrorCode.EFFECT_UNKNOWN, "effect could not be reconciled")
        if self.leases.current_fencing_token(document["resource"]) != journal.fencing_token:
            raise HarnessError(
                ErrorCode.STALE_FENCING_TOKEN, "receipt requires current fencing token"
            )
        if journal.state is EffectState.EXECUTION_ATTEMPTED:
            observed_journal = journal.mark_observed(
                observed_hash=observed, observation_method="FD_HASH_READ", at=self.clock.now()
            )
            with self.uow.begin_immediate():
                self.journals.update(observed_journal, expected_store_version=journal.store_version)
                self._save(document, "EFFECT_OBSERVED", EventType.EFFECT_OBSERVED)
        elif journal.state in (EffectState.EFFECT_OBSERVED, EffectState.RECEIPT_DURABLE):
            observed_journal = journal
        else:
            raise HarnessError(
                ErrorCode.UNRECONCILED_EFFECT_PRESENT, "journal cannot be reconciled"
            )
        receipt = {
            "receipt_id": document["run_id"] + ":receipt",
            "effect_id": journal.effect_id,
            "operation_journal_id": journal.operation_journal_id,
            "run_id": journal.run_id,
            "action_id": journal.action_id,
            "attempt_id": journal.attempt_id,
            "effect_type": "WORKSPACE_WRITE",
            "effect_subject_type": "WORKSPACE_FILE",
            "effect_subject_id": document["target"],
            "target_resource_identity": journal.target_resource_identity,
            "before_hash": str(journal.before_hash),
            "expected_after_hash": str(journal.expected_after_hash),
            "observed_hash": str(observed),
            "observation_method": "FD_HASH_READ",
            "confirmation_level": "LOCAL_OBSERVED",
            "fencing_token": journal.fencing_token,
            "prepared_event_id": journal.prepared_event_id,
            "execution_attempted_event_id": journal.execution_attempted_event_id,
            "observed_at": self.clock.now(),
            "durability_level": "STORAGE_SYNC",
        }
        receipt.update(
            schema_name="EffectReceipt",
            schema_version="1.0.0",
            record_id=receipt["receipt_id"],
            created_at=self.clock.now(),
            producer="local-effect-reconciler/1",
        )
        receipt["receipt_hash"] = str(
            hash_canonical(receipt, artifact_type="effect-receipt", schema_major=1)
        )
        receipt["content_hash"] = str(
            hash_canonical(receipt, artifact_type="effect-receipt-record", schema_major=1)
        )
        self.validate_record("EffectReceipt", receipt)
        if journal.state is not EffectState.RECEIPT_DURABLE:
            with self.uow.begin_immediate():
                document["effect_receipt_hash"] = str(self._put(canonicalize(receipt)))
                stored = observed_journal.store_receipt(receipt["receipt_id"])
                self.journals.update(stored, expected_store_version=observed_journal.store_version)
                self._save(document, "RECEIPT_DURABLE", EventType.EFFECT_RECEIPT_STORED)
        self._verify_receipt(document)
        attempt = self.attempts.get(document["attempt_id"])
        if attempt is None:
            raise HarnessError(ErrorCode.UNRECONCILED_EFFECT_PRESENT, "attempt missing")
        with self.uow.begin_immediate():
            stored_attempt = attempt.with_receipt(receipt["receipt_id"])
            self.attempts.update(stored_attempt, expected_store_version=attempt.store_version)
            updated = stored_attempt.succeed(self.clock.now())
            self.attempts.update(updated, expected_store_version=stored_attempt.store_version)
            self._save(document, "COMMITTED", EventType.ACTION_COMMITTED)
        return self.evaluate(document["run_id"])

    def evaluate(self, run_id: str) -> dict[str, Any]:
        with (
            self.operation_gate.admission("LocalWorkflowService.evaluate", kind="effect")
            if self.operation_gate is not None
            else nullcontext()
        ):
            document = self.inspect(run_id)
            self._state(document, "COMMITTED", "AWAITING_RELEASE")
            self._require_current(document, unchanged=False)
            snapshot = self.workspace.snapshot()
            target = document["target"]
            expected = dict(document["snapshot"])
            expected[target] = document["proposed_artifact_hash"]
            receipt = self._verify_receipt(document)
            journal = self.journals.get_by_effect_id(document["effect_id"])
            attempt = self.attempts.get(document["attempt_id"])
            checks = {
                "expected_file_exists": target in snapshot,
                "workspace_changes_within_plan": snapshot == expected,
                "expected_file_hash": snapshot.get(target) == document["proposed_artifact_hash"],
                "ledger_chain": self.ledger.verify_chain(run_id).valid,
                "effect_receipt": receipt["observed_hash"] == document["proposed_artifact_hash"]
                and journal is not None
                and journal.state is EffectState.RECEIPT_DURABLE
                and journal.receipt_id == receipt["receipt_id"],
                "no_unresolved_action": attempt is not None and attempt.state == "SUCCEEDED",
            }
            payload = self.workspace.read(target) if target in snapshot else b""
            checks.update(self._output_checks(payload, document))
            result = {
                "checks": checks,
                "passed": all(checks.values()),
                "artifact_hash": str(hash_bytes(payload)),
                "policy": document["evaluation_policy"],
                "execution_plan_hash": document["execution_plan_hash"],
            }
            with self.uow.begin_immediate():
                document["evaluation_hash"] = str(self._put(canonicalize(result)))
                self._save(
                    document,
                    "AWAITING_RELEASE" if result["passed"] else "EVALUATION_FAILED",
                    EventType.EVALUATION_COMPLETED,
                )
            return document

    def release(self, run_id: str, *, evaluation_hash: str, auth_session: str) -> dict[str, Any]:
        with (
            self.operation_gate.admission("LocalWorkflowService.release", kind="effect")
            if self.operation_gate is not None
            else nullcontext()
        ):
            subject = self.authority.subject(auth_session)
            document = self.inspect(run_id)
            self._state(document, "AWAITING_RELEASE")
            if document["evaluation_hash"] != evaluation_hash:
                raise HarnessError(
                    ErrorCode.APPROVAL_INVALIDATED, "release names a different evaluation"
                )
            # Freshly re-evaluate the filesystem. A saved PASS cannot authorize changed bytes.
            document = self.evaluate(run_id)
            if (
                document["state"] != "AWAITING_RELEASE"
                or document["evaluation_hash"] != evaluation_hash
            ):
                raise HarnessError(
                    ErrorCode.UNRECONCILED_EFFECT_PRESENT, "release evidence changed"
                )
            decision = {
                "run_id": run_id,
                "subject": subject,
                "evaluation_hash": evaluation_hash,
                "execution_plan_hash": document["execution_plan_hash"],
                "decision": "RELEASE",
                "decided_at": self.clock.now(),
            }
            journal = self.journals.get_by_effect_id(document["effect_id"])
            unreconciled = int(journal is None or journal.state is not EffectState.RECEIPT_DURABLE)
            verdict = evaluate_run_terminal(
                RunTerminalRequest(run_id, "COMPLETED", run_id + ":release", unreconciled),
                now=self.clock.now(),
            )
            if verdict.blocked:
                raise HarnessError(
                    ErrorCode.UNRECONCILED_EFFECT_PRESENT, "run cannot enter terminal state"
                )
            with self.uow.begin_immediate():
                document["release_decision_hash"] = str(self._put(canonicalize(decision)))
                self._save(document, "COMPLETED", EventType.RELEASE_DECIDED)
            return document

    def recover(self, run_id: str) -> dict[str, Any]:
        with (
            self.operation_gate.admission("LocalWorkflowService.recover", kind="effect")
            if self.operation_gate is not None
            else nullcontext()
        ):
            document = self.inspect(run_id)
            # Never repeat an ambiguous write. This path only reconciles observed bytes.
            self._state(
                document,
                "EXECUTION_ATTEMPTED",
                "PREPARED_DURABLE",
                "EFFECT_OBSERVED",
                "RECEIPT_DURABLE",
                "COMMITTED",
            )
            if document["state"] == "COMMITTED":
                return self.evaluate(run_id)
            journal = self.journals.get_by_effect_id(document["effect_id"])
            observed = hash_bytes(self.workspace.read(document["target"]))
            if journal is None or observed != journal.expected_after_hash:
                raise HarnessError(
                    ErrorCode.EFFECT_UNKNOWN,
                    "recovery cannot prove expected effect; no replay performed",
                )
            if journal.state not in (
                EffectState.EXECUTION_ATTEMPTED,
                EffectState.EFFECT_OBSERVED,
                EffectState.RECEIPT_DURABLE,
            ):
                raise HarnessError(
                    ErrorCode.EFFECT_UNKNOWN,
                    "unexpected effect before attempted event; repair required",
                )
            with self.uow.begin_immediate():
                self._save(document, document["state"], EventType.RECOVERY_STARTED)
                document["recovery_decision"] = "EXPECTED_EFFECT_OBSERVED_NO_REPLAY"
                self._save(document, document["state"], EventType.RECOVERY_DECIDED)
            return self._complete_effect(document, journal, observed)

    def _verify_receipt(self, document: dict[str, Any]) -> dict[str, Any]:
        receipt = self._load_artifact(ContentHash.parse(document["effect_receipt_hash"]))
        self.validate_record("EffectReceipt", receipt)
        content = {key: value for key, value in receipt.items() if key != "content_hash"}
        semantic = {key: value for key, value in content.items() if key != "receipt_hash"}
        journal = self.journals.get_by_effect_id(document["effect_id"])
        if (
            receipt["content_hash"]
            != str(hash_canonical(content, artifact_type="effect-receipt-record", schema_major=1))
            or receipt["receipt_hash"]
            != str(hash_canonical(semantic, artifact_type="effect-receipt", schema_major=1))
            or journal is None
            or journal.receipt_id != receipt["receipt_id"]
            or journal.operation_journal_id != receipt["operation_journal_id"]
            or journal.effect_id != receipt["effect_id"]
            or journal.run_id != receipt["run_id"]
            or journal.action_id != receipt["action_id"]
            or journal.target_resource_identity != receipt["target_resource_identity"]
            or receipt["effect_subject_id"] != document["target"]
            or journal.prepared_event_id != receipt["prepared_event_id"]
            or journal.execution_attempted_event_id != receipt["execution_attempted_event_id"]
            or journal.run_id != document["run_id"]
            or journal.attempt_id != receipt["attempt_id"]
            or receipt["before_hash"] != str(journal.before_hash)
            or receipt["expected_after_hash"] != str(journal.expected_after_hash)
            or receipt["observed_hash"] != str(journal.observed_hash)
            or receipt["fencing_token"] != journal.fencing_token
        ):
            raise HarnessError(
                ErrorCode.UNRECONCILED_EFFECT_PRESENT, "effect receipt does not match journal"
            )
        entries = {
            f"{entry.stream_id}:{entry.sequence_number}": entry.event_type
            for entry in self.ledger.load_stream(document["run_id"])
        }
        if (
            entries.get(receipt["prepared_event_id"]) != EventType.ACTION_PREPARED.value
            or entries.get(receipt["execution_attempted_event_id"])
            != EventType.EXECUTION_ATTEMPTED.value
        ):
            raise HarnessError(
                ErrorCode.EVENT_ORDER_VIOLATION, "receipt event references do not resolve"
            )
        return receipt

    def _require_current(self, document: dict[str, Any], *, unchanged: bool) -> None:
        if unchanged:
            self._require_time(document["expires_at"])
            self._require_time(document["token_profile_expires_at"])
        if str(self.executable_digest(document["executable_path"])) != document["executable_hash"]:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH, "declared runtime executable changed"
            )
        if (
            self.workspace.identity() != document["workspace_identity"]
            or self.runtime_identity() != document["runtime_identity"]
        ):
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH, "workspace or runtime changed since plan"
            )
        if unchanged and self.workspace.snapshot() != document["snapshot"]:
            raise HarnessError(ErrorCode.APPROVAL_INVALIDATED, "worktree changed since plan")

    def _require_time(self, expires_at: str) -> None:
        if timestamp_seconds(self.clock.now()) >= timestamp_seconds(expires_at):
            raise HarnessError(ErrorCode.CLOCK_SKEW_EXCEEDED, "plan has expired")

    @staticmethod
    def _state(document: dict[str, Any], *allowed: str) -> None:
        if document["state"] not in allowed:
            raise HarnessError(
                ErrorCode.APPROVAL_REQUIRED, "operation is not allowed in current run state"
            )

    def _validate_output(self, payload: bytes, document: dict[str, Any]) -> None:
        if not all(self._output_checks(payload, document).values()):
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                "Mock output violates UTF-8, JSON, pattern, or diff policy",
            )

    @staticmethod
    def _output_checks(payload: bytes, document: dict[str, Any]) -> dict[str, bool]:
        policy = document["evaluation_policy"]
        checks = {
            "utf8_valid": False,
            "json_object_valid": False,
            "forbidden_patterns_absent": False,
            "diff_size_within_limit": len(payload) + document["before_size"]
            <= policy["maximum_diff_bytes"],
        }
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            return checks
        checks["utf8_valid"] = True
        checks["forbidden_patterns_absent"] = not any(
            pattern in text for pattern in policy["forbidden_patterns"]
        )
        try:
            checks["json_object_valid"] = isinstance(json.loads(text), dict)
        except json.JSONDecodeError:
            checks["json_object_valid"] = False
        return checks

    def _put(self, payload: bytes) -> ContentHash:
        digest = hash_bytes(payload)
        self.artifacts.put(
            payload,
            ArtifactMetadata("application/json", len(payload), "SYNTHETIC", "UNTRUSTED_INPUT"),
            artifact_id=str(digest),
            stored_at=self.clock.now(),
        )
        return digest

    def _load_artifact(self, digest: ContentHash) -> dict[str, Any]:
        self.artifacts.verify(digest).raise_if_repair_required()
        payload = self.artifacts.get(digest)
        if hash_bytes(payload) != digest:
            raise HarnessError(ErrorCode.ARTIFACT_CONTENT_CONFLICT, "stored artifact changed")
        result = json.loads(payload)
        if not isinstance(result, dict):
            raise HarnessError(ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "record must be an object")
        return result

    def _save(self, document: dict[str, Any], state: str, event: EventType) -> None:
        previous = int(document["version"])
        run_states = {
            "PLANNED": "WAITING_APPROVAL",
            "APPROVED": "READY",
            "AWAITING_RELEASE": "WAITING_RELEASE",
            "COMPLETED": "COMPLETED",
            "EVALUATION_FAILED": "BLOCKED_REPAIR_REQUIRED",
        }
        document.update(
            state=state, run_state=run_states.get(state, "RUNNING"), version=previous + 1
        )
        digest = self._put(canonicalize(document))
        self.runs.save(
            WorkflowRecord(document["run_id"], state, previous + 1, digest),
            expected_version=previous,
        )
        self.ledger.append(
            [NewEvent(document["run_id"], event.value, digest, self.clock.now())],
            expected_stream_sequence=self.ledger.stream_head(document["run_id"]),
        )
