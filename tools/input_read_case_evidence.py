"""Validate metadata-only observations of denied input reads."""

from __future__ import annotations

from harness.domain.hashing import ContentHash, hash_canonical

INPUT_READ_RESULT_CLASS = "harness.ports.task_intake.InputReadOutcome"
FIELDS = {
    "result_class",
    "subject_type",
    "subject_id",
    "state",
    "error_code",
    "durability_observation",
    "request_binding",
    "capability_path_hash",
    "payload_present",
    "read_evidence_present",
    "classification_present",
    "masking_denial_present",
    "masker_invocations",
    "result_events",
    "result_head_before",
    "result_head_after",
    "ledger",
    "effects",
}


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError("INPUT_OBSERVATION_INVALID: " + reason)


def validate_input_read(typed: dict, row: dict | None = None) -> None:
    try:
        _validate(typed, row)
    except (KeyError, TypeError) as exc:
        raise ValueError("INPUT_OBSERVATION_INVALID: malformed metadata") from exc


def _validate(typed: dict, row: dict | None) -> None:
    require(set(typed) == FIELDS, "unexpected or missing fields")
    require(
        typed["result_class"] == INPUT_READ_RESULT_CLASS
        and typed["subject_type"] == "INPUT_READ_DECISION",
        "result type",
    )
    require(typed["state"] == "DENIED", "only denied results supported")
    require(
        typed["payload_present"] is False and typed["read_evidence_present"] is False,
        "denied payload or read evidence retained",
    )
    require(
        type(typed["classification_present"]) is bool
        and type(typed["masking_denial_present"]) is bool,
        "presence flags",
    )
    request = typed["request_binding"]
    require(set(request) == {"subject_id", "stream_id", "capability_path_hash"}, "request fields")
    require(
        isinstance(typed["subject_id"], str)
        and bool(typed["subject_id"])
        and typed["subject_id"] == request["subject_id"],
        "request identity",
    )
    require(typed["capability_path_hash"] == request["capability_path_hash"], "request hash")
    ContentHash.parse(typed["capability_path_hash"])
    require(typed["durability_observation"] == "NOT_APPLICABLE", "durability")
    effects = typed["effects"]
    require(
        set(effects)
        == {
            "network_calls",
            "process_launches",
            "workspace_commits",
            "external_effects",
            "artifact_put_attempts",
            "masker_invocations",
        },
        "effect fields",
    )
    require(all(type(x) is int and x == 0 for x in effects.values()), "unexpected effect")
    if typed["masking_denial_present"]:
        require(
            typed["error_code"] is None and typed["classification_present"] is True,
            "masking denial metadata",
        )
        require(
            type(typed["masker_invocations"]) is int
            and typed["masker_invocations"] == effects["masker_invocations"],
            "masker count",
        )
    else:
        require(
            isinstance(typed["error_code"], str)
            and bool(typed["error_code"])
            and typed["classification_present"] is False
            and typed["masker_invocations"] is None,
            "reader denial metadata",
        )
    ledger = typed["ledger"]
    require(set(ledger) == {"head_before", "head_after", "chain_valid", "records"}, "ledger fields")
    before, after = ledger["head_before"], ledger["head_after"]
    require(type(before) is int and type(after) is int and 0 <= before < after, "ledger heads")
    require(
        type(typed["result_head_before"]) is int
        and type(typed["result_head_after"]) is int
        and before == typed["result_head_before"]
        and after == typed["result_head_after"],
        "result heads",
    )
    require(ledger["chain_valid"] is True, "ledger chain")
    records = ledger["records"]
    require(isinstance(records, list) and len(records) == after - before, "ledger coverage")
    events = []
    for entry in records:
        require(set(entry) == {"stream_id", "event_type", "payload_hash"}, "ledger record fields")
        require(entry["stream_id"] == request["stream_id"], "ledger stream")
        expected = str(
            hash_canonical(
                {
                    "event_type": entry["event_type"],
                    "read_decision_id": typed["subject_id"],
                    "capability_path_hash": typed["capability_path_hash"],
                },
                artifact_type="input-read-event",
                schema_major=1,
            )
        )
        require(entry["payload_hash"] == expected, "ledger payload hash")
        events.append(entry["event_type"])
    require(events == typed["result_events"], "result events")
    if row is not None:
        require(
            row.get("ledger_head_before") == before and row.get("ledger_head_after") == after,
            "raw heads",
        )
        require(row.get("observed_event_sequence", [])[before:] == events, "raw events")
        require(
            all(
                row.get("side_effects", {}).get(k) == effects[k]
                for k in (
                    "network_calls",
                    "process_launches",
                    "workspace_commits",
                    "external_effects",
                )
            ),
            "raw effect counters",
        )
