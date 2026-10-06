"""Bind fresh orchestration observations to the existing formal Case contract."""

from __future__ import annotations

import json
from pathlib import Path

from approval_case_evidence import APPROVAL_RESULT_CLASSES, validate_approval_result
from collect_release_baseline import digest
from emit_case_evidence import EvidenceEmissionError, verify_case_evidence
from emit_unit_area_evidence import verifier
from input_read_case_evidence import INPUT_READ_RESULT_CLASS, validate_input_read


class FormalCaseError(EvidenceEmissionError):
    pass


RESULT_SUBJECTS = {
    "harness.application.approval_plan_orchestrator.AttemptGuardOutcome": "ACTION_ATTEMPT",
    "harness.domain.event_order.AppendOrderVerdict": "EVENT_APPEND_RESULT",
    "harness.domain.manifest_subject.ManifestValidationResult": "MANIFEST_VALIDATION_RESULT",
    "harness.domain.run_terminal.RunTerminalVerdict": "RUN",
}


def formal_case(
    *,
    key: str,
    nodes: tuple[str, ...],
    legacy_path: Path,
    execution: Path,
    fixture: Path,
    repo: Path,
    root: Path,
    manifest: dict,
    expected: dict,
) -> dict:
    """Only new runner outputs are inputs; missing fields never get expected values."""
    verify_case_evidence(legacy_path)
    legacy = json.loads(legacy_path.read_text())
    raw = execution / "observations.jsonl"
    receipt = json.loads((execution / "execution.json").read_text())
    rows = [json.loads(line) for line in raw.read_text().splitlines() if line.strip()]
    rows = [row for row in rows if row.get("case_id") == key]
    actual_nodes = [row.get("node_id") for row in rows]
    if (
        not rows
        or any(row.get("recorded") is not True for row in rows)
        or any(not isinstance(node, str) for node in actual_nodes)
        or len(set(actual_nodes)) != len(actual_nodes)
        or {node.split("[", 1)[0] for node in actual_nodes} != set(nodes)
    ):
        raise FormalCaseError("OBSERVATION_COVERAGE_INCOMPLETE")
    if not isinstance(receipt.get("command"), list) or any(
        not isinstance(arg, str) for arg in receipt["command"]
    ):
        raise FormalCaseError("EXECUTION_RECEIPT_INVALID")
    if (
        receipt.get("contract") != "case-execution-record/1"
        or not all(verifier.valid_timestamp(receipt.get(k)) for k in ("started_at", "recorded_at"))
        or receipt.get("case_id") != key
        or type(receipt.get("exit_code")) is not int
        or receipt["exit_code"] != 0
        or receipt.get("cwd") != str(repo)
        or receipt.get("raw_observations_hash") != digest(raw)
        or receipt.get("runner_source_hash") != digest(repo / "tools/case_runner.py")
        or receipt.get("command", [])[1:]
        != ["-m", "pytest", "-q", "-p", "no:cacheprovider", *nodes]
    ):
        raise FormalCaseError("EXECUTION_RECEIPT_INVALID")
    if (
        legacy.get("case_id") != key
        or legacy.get("status") != "PASS"
        or legacy.get("implementation_commit_sha") != manifest["implementation_commit_sha"]
        or legacy.get("environment_manifest_hash")
        != manifest["runtime_environment"]["environment_manifest_hash"]
        or legacy.get("input_fixture_hash") != digest(fixture)
        or legacy.get("expected", {}).get("expectation_descriptor_hash")
        != expected["expectation_descriptor_hash"]
    ):
        raise FormalCaseError("DEVELOPMENT_EVIDENCE_BINDING_MISMATCH")
    if expected.get("event_observation_policy") == "NOT_APPLICABLE":
        raise FormalCaseError("UNIT_REQUIRES_UNIT_COLLECTOR")
    assertions = []
    first = rows[0]
    fields = (
        "typed_result",
        "observed_state",
        "observed_error_code",
        "actual_subject_id",
        "observed_event_sequence",
        "ledger_head_before",
        "ledger_head_after",
        "side_effects",
        "input_payload",
    )
    for row in rows:
        if any(row.get(field) != first.get(field) for field in fields):
            raise FormalCaseError("OBSERVATIONS_DISAGREE")
        typed = row.get("typed_result")
        if not isinstance(typed, dict):
            raise FormalCaseError("SUBJECT_TYPE_NOT_OBSERVED")
        if typed.get("result_class") == INPUT_READ_RESULT_CLASS:
            try:
                validate_input_read(typed, row)
            except ValueError as exc:
                raise FormalCaseError(str(exc)) from exc
            subject = "INPUT_READ_DECISION"
        elif typed.get("result_class") in APPROVAL_RESULT_CLASSES:
            try:
                subject = validate_approval_result(typed, repo)
                if subject == "APPROVAL_CONSUME_RESULT" and (
                    row.get("ledger_head_before") != typed["approval_result"]["head_before"]
                    or row.get("observed_event_sequence")
                    != [e["event_type"] for e in typed["persisted_binding"]["ledger_records"]]
                ):
                    raise ValueError("APPROVAL_LEDGER_OBSERVATION_MISMATCH")
            except ValueError as exc:
                raise FormalCaseError(str(exc)) from exc
        else:
            subject = RESULT_SUBJECTS.get(typed.get("result_class"))
        if subject is None or typed.get("subject_type") != subject:
            raise FormalCaseError("RESULT_TYPE_UNSUPPORTED")
        if (
            expected.get("durability_tier") is not None
            or typed.get("durability_observation") != "NOT_APPLICABLE"
        ):
            raise FormalCaseError("DURABILITY_NOT_OBSERVED")
        if row.get("ledger_observed") is not True:
            raise FormalCaseError("LEDGER_NOT_OBSERVED")
        before, after = row.get("ledger_head_before"), row.get("ledger_head_after")
        events = row.get("observed_event_sequence")
        if (
            type(before) is not int
            or type(after) is not int
            or before < 0
            or after < before
            or not isinstance(events, list)
            or after != len(events)
        ):
            raise FormalCaseError("LEDGER_HEAD_INVALID")
        effects = row.get("side_effects")
        names = (
            "network_calls",
            "process_launches",
            "workspace_commits",
            "external_effects",
            "ledger_effect_attempts",
        )
        if (
            not isinstance(effects, dict)
            or set(effects) != set(names)
            or any(type(effects[n]) is not int or effects[n] < 0 for n in names)
        ):
            raise FormalCaseError("SIDE_EFFECTS_NOT_OBSERVED")
        if typed["result_class"] == (
            "harness.application.approval_plan_orchestrator.AttemptGuardOutcome"
        ):
            attempt = typed.get("attempt_record")
            invocations = typed.get("effect_invocations")
            if (
                not isinstance(attempt, dict)
                or attempt.get("result_class") != "harness.domain.attempt.ActionAttempt"
                or attempt.get("subject_id") != typed.get("subject_id")
                or attempt.get("state") != typed.get("state")
                or type(invocations) is not int
                or invocations < 0
                or effects["external_effects"] != invocations
            ):
                raise FormalCaseError("ATTEMPT_RESULT_INCONSISTENT")
        checks = {
            "typed subject matches registry": subject == expected["expected_subject_type"],
            "typed result identity matches observation": typed.get("subject_id")
            == row.get("actual_subject_id")
            and bool(row.get("actual_subject_id")),
            "typed result state matches observation": typed.get("state")
            == row.get("observed_state"),
            "typed error matches observation": typed.get("error_code")
            == row.get("observed_error_code"),
            "state matches registry": row.get("observed_state") == expected["expected_state"],
            "error matches registry": row.get("observed_error_code")
            == expected["expected_error_code"],
            "ledger events match registry": events == expected["expected_event_sequence"],
            "fixture equals executed input": row.get("input_payload")
            == json.loads(fixture.read_text()),
            "legacy state matches raw": legacy["observed"]["state"] == row.get("observed_state"),
            "legacy events match raw": legacy["observed"]["event_sequence"] == events,
            "legacy counters match raw": legacy["side_effects"] == effects,
        }
        if not all(checks.values()):
            raise FormalCaseError(
                "OBSERVATION_MISMATCH: " + ",".join(k for k, v in checks.items() if not v)
            )
        assertions.extend(
            {"expression": row["node_id"] + ": " + name, "result": value}
            for name, value in checks.items()
        )
    test_id, case_id = key.split("/")
    return {
        "evidence_schema_version": "3.0",
        "release_scope": manifest["release_scope"],
        "test_id": test_id,
        "case_id": case_id,
        "status": "PASS",
        "producer": "tools/formal_case_evidence.py",
        "test_run_id": key,
        **{
            field: receipt[field]
            for field in ("command", "exit_code", "started_at", "recorded_at", "runner_source_hash")
        },
        **{
            field: manifest[field]
            for field in ("implementation_commit_sha", "schema_set_hash", "migration_head")
        },
        "runtime_environment_hash": manifest["runtime_environment"]["environment_manifest_hash"],
        "input_fixture_path": fixture.relative_to(root).as_posix(),
        "input_fixture_hash": digest(fixture),
        "raw_result_path": raw.relative_to(root).as_posix(),
        "raw_result_hash": digest(raw),
        "expectation_descriptor_hash": expected["expectation_descriptor_hash"],
        "observed_subject_type": first["typed_result"]["subject_type"],
        "observed_state": first["observed_state"],
        "observed_error_code": first["observed_error_code"],
        "actual_subject_id": first["actual_subject_id"],
        "evidence_kind": "ORCHESTRATION",
        "event_observation": "OBSERVED" if first["observed_event_sequence"] else "OBSERVED_EMPTY",
        "event_observation_policy": expected.get("event_observation_policy"),
        "observed_event_sequence": first["observed_event_sequence"],
        "ledger_head_before": first["ledger_head_before"],
        "ledger_head_after": first["ledger_head_after"],
        "durability_tier": None,
        "side_effects": first["side_effects"],
        "assertions": assertions,
        "development_evidence_path": legacy_path.relative_to(root).as_posix(),
        "development_evidence_hash": digest(legacy_path),
    }
