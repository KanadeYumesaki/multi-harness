"""Validate measured approval subjects without using Case expectations."""

from __future__ import annotations

import json
from pathlib import Path

from harness.domain.errors import HarnessError
from harness.domain.hashing import hash_bytes, hash_canonical
from harness.domain.schema_set import compute_schema_set_hash
from harness.infrastructure.schema.registry import CoreSchemaRegistry

APPROVAL_RESULT_CLASSES = {
    "harness.application.approval_plan_orchestrator.ApprovalOutcome",
    "harness.ports.approval_consume.ConsumeOutcome",
}


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError("APPROVAL_OBSERVATION_INVALID: " + reason)


def validate_approval_result(typed: dict, repo: Path) -> str:
    """Recheck raw-result bindings, including persisted loser artifact bytes."""
    try:
        return _validate(typed, repo)
    except (KeyError, TypeError, HarnessError) as exc:
        raise ValueError("APPROVAL_OBSERVATION_INVALID: malformed binding") from exc


def _validate(typed: dict, repo: Path) -> str:
    result = typed["approval_result"]
    require(isinstance(result, dict), "result missing")
    if typed["result_class"].endswith(".ApprovalOutcome"):
        require(typed["result_class"] in APPROVAL_RESULT_CLASSES, "unknown class")
        require(type(result["successful_consumes"]) is int, "counter type")
        require(type(result["grant_consumed"]) is bool, "consumed type")
        require(bool(result["grant_id"]), "grant id missing")
        require(result["state"] == typed["state"], "outcome state")
        require(result["error_code"] == typed["error_code"], "outcome error")
        subject = typed["subject_record"]
        require(subject["subject_id"] == typed["subject_id"], "subject id")
        require(subject["state"] == typed["state"], "subject state")
        if subject["result_class"] == "harness.domain.attempt.ActionAttempt":
            require(result["grant_consumed"] is False, "blocked grant consumed")
            require(result["successful_consumes"] == 0, "blocked success count")
            require(result["attempt_id"] == subject["subject_id"], "attempt id")
            require(result["attempt_state"] == subject["state"], "attempt state")
            require(bool(result["error_code"]), "missing denial")
            return "ACTION_ATTEMPT"
        require(subject["result_class"] == "harness.domain.approval.ApprovalGrant", "subject class")
        require(
            result["grant_consumed"] is True and result["successful_consumes"] == 1,
            "grant not consumed",
        )
        require(result["grant_id"] == subject["subject_id"], "grant id")
        require(subject["state"] == "CONSUMED" and result["error_code"] is None, "grant state")
        require(bool(subject["attempt_id"]), "grant consumption identity missing")
        return "APPROVAL_GRANT"

    require(typed["result_class"] == "harness.ports.approval_consume.ConsumeOutcome", "class")
    binding = typed["persisted_binding"]
    raw = binding["artifact_utf8"].encode("utf-8")
    digest = str(hash_bytes(raw))
    require(digest == result["result_hash"] == binding["store_result_hash"], "artifact hash")
    record = json.loads(raw)
    registry = CoreSchemaRegistry(repo)
    registry.validate_or_raise("ApprovalConsumeResult", str(record["schema_version"]), record)
    require(record["schema_name"] == "ApprovalConsumeResult", "schema name")
    require(
        record["schema_set_hash"]
        == str(compute_schema_set_hash((registry.active_ref("ApprovalConsumeResult"),))),
        "schema binding",
    )
    require(
        record["content_hash"]
        == str(
            hash_canonical(
                {k: v for k, v in record.items() if k != "content_hash"},
                artifact_type="approval-consume-result",
                schema_major=1,
            )
        ),
        "content hash",
    )
    require(
        record["consume_result_id"] == result["consume_result_id"] == typed["subject_id"],
        "result identity",
    )
    require(
        record["state"] == typed["state"] and record["error_code"] == typed["error_code"],
        "record state or error",
    )
    ticket, winner, grant = binding["ticket"], binding["winner"], binding["grant"]
    require(ticket["ticket_id"] == result["ticket_id"], "ticket identity")
    require(ticket["state"] == "REJECTED" and winner["state"] == "CONSUMED", "ticket state")
    require(record["attempt_id"] == ticket["attempt_id"], "loser attempt")
    require(
        record["grant_id"]
        == result["grant_id"]
        == ticket["grant_id"]
        == winner["grant_id"]
        == grant["subject_id"],
        "grant binding",
    )
    require(
        record["concurrency_group"]
        == result["concurrency_group"]
        == ticket["concurrency_group"]
        == winner["concurrency_group"],
        "group binding",
    )
    require(
        grant["result_class"] == "harness.domain.approval.ApprovalGrant"
        and grant["state"] == "CONSUMED"
        and grant["attempt_id"] == winner["attempt_id"],
        "persisted winner",
    )
    require(result["consume_result_id"] == result["ticket_id"] + ":result", "result ticket")
    for field in ("successful_consumes", "failed_consumes"):
        require(type(result[field]) is int and result[field] == record[field], "loser counter")
    group = binding["group_results"]
    require(isinstance(group, list) and len(group) >= 2, "group observations missing")
    require(
        all(
            type(x["successful_consumes"]) is int
            and type(x["failed_consumes"]) is int
            and (x["successful_consumes"], x["failed_consumes"]) in ((1, 0), (0, 1))
            and x["concurrency_group"] == result["concurrency_group"]
            and x["grant_id"] == result["grant_id"]
            for x in group
        ),
        "group outcomes",
    )
    require(len({x["ticket_id"] for x in group}) == len(group), "duplicate participants")
    require(sum(x["successful_consumes"] for x in group) == 1, "group success count")
    require(result in group, "loser absent from group")
    winning = next(x for x in group if x["successful_consumes"] == 1)
    require(winning["ticket_id"] == winner["ticket_id"], "winner absent from group")
    require(binding["chain_valid"] is True, "ledger chain")
    events = binding["ledger_records"]
    require(result["stream_id"] == "approval-result:" + result["ticket_id"], "result stream")
    require(
        len(events) == 1
        and events[0]["stream_id"] == result["stream_id"]
        and events[0]["payload_hash"] == digest
        and events[0]["event_type"] == "APPROVAL_REPLAY_DENIED",
        "ledger reference",
    )
    return "APPROVAL_CONSUME_RESULT"
