"""Read typed approval outcomes and their persisted records, not expectations."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from threading import Lock

from ledger_probe import SideEffectProbe

from approval_case_evidence import validate_approval_result
from harness.application.approval_plan_orchestrator import ApprovalOutcome
from harness.domain.approval import ApprovalGrant
from harness.domain.attempt import ActionAttempt
from harness.domain.hashing import ContentHash
from harness.ports.approval_consume import ConsumeOutcome, ConsumeTicket

ROOT = Path(__file__).resolve().parents[2]


def grant_record(grant):
    if type(grant) is not ApprovalGrant:
        raise ValueError("APPROVAL_GRANT_NOT_OBSERVED")
    return {
        "result_class": "harness.domain.approval.ApprovalGrant",
        "subject_id": grant.grant_id,
        "state": grant.status.value,
        "attempt_id": grant.attempt_id,
    }


def consume_record(result):
    if type(result) not in (ConsumeOutcome, ConsumeTicket):
        raise ValueError("CONSUME_RESULT_NOT_OBSERVED")
    return {
        f.name: str(value) if isinstance(value := getattr(result, f.name), ContentHash) else value
        for f in fields(result)
    }


def publish(observation, typed):
    subject = validate_approval_result(typed, ROOT)
    typed["subject_type"] = subject
    typed["durability_observation"] = "NOT_APPLICABLE"
    observation.record(
        state=typed["state"], subject_id=typed["subject_id"], error_code=typed["error_code"]
    )
    observation.typed_result = typed


def record_approval_outcome(observation, result, *, grant=None):
    if type(result) is not ApprovalOutcome:
        raise ValueError("APPROVAL_RESULT_NOT_OBSERVED")
    if result.attempt is not None:
        if type(result.attempt) is not ActionAttempt:
            raise ValueError("APPROVAL_ATTEMPT_NOT_OBSERVED")
        subject = {
            "result_class": "harness.domain.attempt.ActionAttempt",
            "subject_id": result.attempt.attempt_id,
            "state": result.attempt.state,
        }
    else:
        subject = grant_record(grant)
    publish(
        observation,
        {
            "result_class": "harness.application.approval_plan_orchestrator.ApprovalOutcome",
            "subject_id": subject["subject_id"],
            "state": subject["state"],
            "error_code": result.error_code.value if result.error_code else None,
            "subject_record": subject,
            "approval_result": {
                "grant_id": result.grant_id,
                "state": result.state,
                "error_code": result.error_code.value if result.error_code else None,
                "successful_consumes": result.successful_consumes,
                "grant_consumed": result.grant_consumed,
                "attempt_id": result.attempt.attempt_id if result.attempt else None,
                "attempt_state": result.attempt_state,
            },
        },
    )


def record_consume_outcome(observation, result, *, reader, outcomes):
    import json

    if type(result) is not ConsumeOutcome or not result.consume_result_id:
        raise ValueError("CONSUME_RESULT_NOT_OBSERVED")
    digest = reader.store.result_hash(result.consume_result_id)
    raw = reader.artifacts.get(digest)
    record = json.loads(raw)
    publish(
        observation,
        {
            "result_class": "harness.ports.approval_consume.ConsumeOutcome",
            "subject_id": record["consume_result_id"],
            "state": record["state"],
            "error_code": record["error_code"],
            "approval_result": consume_record(result),
            "persisted_binding": {
                "artifact_utf8": raw.decode("utf-8"),
                "store_result_hash": str(digest),
                "ticket": consume_record(reader.store.get(result.ticket_id)),
                "winner": consume_record(reader.store.winner(result.concurrency_group)),
                "grant": grant_record(reader.grants.get(result.grant_id)),
                "group_results": [consume_record(x) for x in outcomes],
                "chain_valid": reader.ledger.verify_chain(result.stream_id).valid,
                "ledger_records": [
                    {
                        "stream_id": e.stream_id,
                        "event_type": e.event_type,
                        "payload_hash": str(e.payload_hash),
                    }
                    for e in reader.ledger.load_stream(result.stream_id)
                ],
            },
        },
    )


class ApprovalEffectProbe(SideEffectProbe):
    """Observe selected Python I/O entry points across the competing threads.

    This is a test monitor, not an OS sandbox or a claim about arbitrary native code.
    Attempted effects are counted and stopped before real I/O.
    """

    def install(self, monkeypatch):
        import socket
        import subprocess

        from harness.infrastructure.filesystem.workspace_writer import WorkspaceWriter

        lock = Lock()

        def deny(field):
            def called(*args, **kwargs):
                with lock:
                    setattr(self, field, getattr(self, field) + 1)
                raise RuntimeError("APPROVAL_EFFECT_ATTEMPT: " + field)

            return called

        for name in ("connect", "connect_ex", "sendto", "sendmsg"):
            monkeypatch.setattr(socket.socket, name, deny("network_calls"))
        monkeypatch.setattr(subprocess, "Popen", deny("process_launches"))
        monkeypatch.setattr(WorkspaceWriter, "commit", deny("workspace_commits"))
