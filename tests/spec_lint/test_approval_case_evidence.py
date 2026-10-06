"""Approval evidence rejection tests. Synthetic fixtures are not release evidence."""

from __future__ import annotations

import copy
import json
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_formal_case_gate_collection as formal_fixtures
from approval_observation import (
    ApprovalEffectProbe,
    record_approval_outcome,
    record_consume_outcome,
)
from case_observation import CaseObservation
from test_formal_case_gate_collection import save

from approval_case_evidence import validate_approval_result
from collect_release_baseline import digest
from emit_case_evidence import _evidence_hash
from formal_case_evidence import FormalCaseError, formal_case
from harness.application.approval_plan_orchestrator import ApprovalPlanOrchestrator, ApprovalRequest
from harness.infrastructure.filesystem.workspace_writer import WorkspaceWriter

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/integration/sqlite"))


@pytest.fixture
def measured(tmp_path):
    import test_approval_consume_coordinator as fixtures
    from test_approval_plan_orchestrator import CountingEffect, _attempt, _consume, _grant

    resources = fixtures.environment.__wrapped__(tmp_path)
    factory, connection, directory, reader = next(resources)
    try:
        first, second = fixtures.admitted_pair(reader)
        outcomes = [reader.consume(*first), reader.consume(*second)]
        loser = outcomes[-1]
        observation = CaseObservation("synthetic/loser", "node")
        record_consume_outcome(observation, loser, reader=reader, outcomes=outcomes)
        result = {"loser": observation.typed_result}
        orchestrator = ApprovalPlanOrchestrator(
            grants=reader.grants,
            ledger=reader.ledger,
            unit_of_work=reader.uow,
            clock=reader.clock,
            effect=CountingEffect(),
        )
        winner = orchestrator.issue_and_consume(
            ApprovalRequest(
                stream_id="other-approval",
                grant=replace(_grant(), grant_id="other", nonce="0123456789abcdef-other"),
                consume=replace(_consume(now="2026-08-20T00:01:00Z"), grant_id="other"),
                concurrency_group="other-group",
            )
        )
        record_approval_outcome(observation, winner, grant=reader.grants.get("other"))
        result["winner"] = observation.typed_result
        replay = orchestrator.consume_only(
            ApprovalRequest(
                stream_id="other-approval",
                grant=replace(_grant(), grant_id="other", nonce="0123456789abcdef-other"),
                consume=replace(_consume(now="2026-08-20T00:02:00Z"), grant_id="other"),
                concurrency_group="other-group",
                attempt=_attempt(),
            )
        )
        record_approval_outcome(observation, replay)
        result["replay"] = observation.typed_result
        result["winner_object"] = winner
        result["loser_object"] = loser
        result["reader"] = reader
        result["outcomes"] = outcomes
        yield result
    finally:
        resources.close()


@pytest.mark.parametrize(
    "kind,subject",
    [
        ("loser", "APPROVAL_CONSUME_RESULT"),
        ("winner", "APPROVAL_GRANT"),
        ("replay", "ACTION_ATTEMPT"),
    ],
)
def test_real_results_keep_distinct_subjects(measured, kind, subject):
    assert validate_approval_result(measured[kind], ROOT) == subject
    assert measured[kind]["subject_type"] == subject


@pytest.mark.parametrize(
    "kind,path,value",
    [
        ("winner", "subject_record.subject_id", "another"),
        ("winner", "subject_record.state", "ISSUED"),
        ("winner", "subject_record.result_class", "harness.domain.attempt.ActionAttempt"),
        ("winner", "approval_result.grant_consumed", False),
        ("winner", "approval_result.successful_consumes", True),
        ("replay", "approval_result.attempt_id", "another"),
        ("replay", "approval_result.attempt_state", "SUCCEEDED"),
        ("replay", "approval_result.error_code", "CLOCK_SKEW_EXCEEDED"),
        ("replay", "approval_result.successful_consumes", 1),
        ("loser", "persisted_binding.store_result_hash", "sha256:" + "0" * 64),
        ("loser", "approval_result.consume_result_id", "another"),
        ("loser", "approval_result.failed_consumes", True),
        ("loser", "persisted_binding.ticket.ticket_id", "another"),
        ("loser", "persisted_binding.ticket.attempt_id", "another"),
        ("loser", "persisted_binding.winner.concurrency_group", "another"),
        ("loser", "persisted_binding.grant.attempt_id", "another"),
        ("loser", "persisted_binding.chain_valid", False),
        ("loser", "persisted_binding.group_results", []),
        ("loser", "persisted_binding.ledger_records", []),
    ],
)
def test_changed_binding_is_rejected(measured, kind, path, value):
    typed = copy.deepcopy(measured[kind])
    target = typed
    keys = path.split(".")
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    with pytest.raises(ValueError, match="APPROVAL_OBSERVATION_INVALID"):
        validate_approval_result(typed, ROOT)


@pytest.mark.parametrize(
    "fault",
    [
        "two_winners",
        "duplicate",
        "ledger_hash",
        "ledger_stream",
        "content_hash",
        "schema",
        "schema_set",
    ],
)
def test_rehashed_and_group_corruption_is_rejected(measured, fault):
    from harness.domain.hashing import hash_bytes

    typed = copy.deepcopy(measured["loser"])
    binding = typed["persisted_binding"]
    if fault == "two_winners":
        binding["group_results"][1].update(successful_consumes=1, failed_consumes=0)
    elif fault == "duplicate":
        binding["group_results"].append(copy.deepcopy(binding["group_results"][0]))
    elif fault in ("ledger_hash", "ledger_stream"):
        binding["ledger_records"][0]["payload_hash" if fault == "ledger_hash" else "stream_id"] = (
            "other"
        )
    else:
        record = json.loads(binding["artifact_utf8"])
        field = {
            "content_hash": "content_hash",
            "schema": "schema_version",
            "schema_set": "schema_set_hash",
        }[fault]
        record[field] = "sha256:" + "0" * 64 if fault != "schema" else "99.0.0"
        binding["artifact_utf8"] = json.dumps(record)
        changed = str(hash_bytes(binding["artifact_utf8"].encode()))
        typed["approval_result"]["result_hash"] = binding["store_result_hash"] = changed
        binding["ledger_records"][0]["payload_hash"] = changed
    with pytest.raises(ValueError):
        validate_approval_result(typed, ROOT)


def test_wrong_runtime_classes_and_missing_grant_are_not_observations(measured):
    observation = CaseObservation("synthetic/case", "node")
    with pytest.raises(ValueError, match="RESULT_NOT_OBSERVED"):
        record_approval_outcome(observation, SimpleNamespace())
    with pytest.raises(ValueError, match="GRANT_NOT_OBSERVED"):
        record_approval_outcome(observation, measured["winner_object"])
    with pytest.raises(ValueError, match="CONSUME_RESULT_NOT_OBSERVED"):
        record_consume_outcome(
            observation, SimpleNamespace(), reader=measured["reader"], outcomes=[]
        )
    assert observation.typed_result is None
    assert observation.as_dict()["recorded"] is False


@pytest.mark.parametrize("kind", ["network", "process", "workspace"])
def test_effect_probe_counts_and_denies_real_entry_points(monkeypatch, kind):
    probe = ApprovalEffectProbe()
    probe.install(monkeypatch)
    with pytest.raises(RuntimeError, match="APPROVAL_EFFECT_ATTEMPT"):
        if kind == "network":
            with socket.socket() as client:
                client.connect(("127.0.0.1", 9))
        elif kind == "process":
            subprocess.Popen(["/nonexistent/approval-probe"])  # noqa: S603 - patched denial probe
        else:
            WorkspaceWriter.commit(None, None)
    assert probe.external_effects == 1
    assert (
        getattr(
            probe,
            {
                "network": "network_calls",
                "process": "process_launches",
                "workspace": "workspace_commits",
            }[kind],
        )
        == 1
    )


@pytest.fixture
def formal_sample(tmp_path, measured):
    sample = formal_fixtures.sample.__wrapped__(tmp_path / "formal")
    key = "AT-APPROVAL-002/CONCURRENT_LOSER"
    typed = measured["loser"]
    expected = json.loads((ROOT / "registry-snapshot.json").read_text())["expectations"][key]
    raw = sample["execution"] / "observations.jsonl"
    row = json.loads(raw.read_text())
    row.update(
        case_id=key,
        typed_result=typed,
        actual_subject_id=typed["subject_id"],
        observed_state=typed["state"],
        observed_error_code=typed["error_code"],
        observed_event_sequence=["APPROVAL_REPLAY_DENIED"],
        ledger_head_before=0,
        ledger_head_after=1,
    )
    save(raw, row)
    path = sample["execution"] / "execution.json"
    receipt = json.loads(path.read_text())
    receipt.update(case_id=key, raw_observations_hash=digest(raw))
    save(path, receipt)
    legacy = json.loads(sample["legacy_path"].read_text())
    legacy.update(
        case_id=key,
        expected=expected,
        observed={"state": typed["state"], "event_sequence": row["observed_event_sequence"]},
    )
    legacy["evidence_hash"] = _evidence_hash(legacy)
    save(sample["legacy_path"], legacy)
    return {**sample, "key": key, "expected": expected}


def test_formal_collector_accepts_complete_loser_binding(formal_sample):
    doc = formal_case(**formal_sample)
    assert doc["observed_subject_type"] == "APPROVAL_CONSUME_RESULT"
    assert doc["durability_tier"] is None


def test_formal_collector_rechecks_persisted_binding(formal_sample):
    raw = formal_sample["execution"] / "observations.jsonl"
    row = json.loads(raw.read_text())
    row["typed_result"]["persisted_binding"]["chain_valid"] = False
    save(raw, row)
    path = formal_sample["execution"] / "execution.json"
    receipt = json.loads(path.read_text())
    receipt["raw_observations_hash"] = digest(raw)
    save(path, receipt)
    with pytest.raises(FormalCaseError, match="ledger chain"):
        formal_case(**formal_sample)


def test_formal_collector_binds_outcome_to_observed_ledger_head(formal_sample):
    raw = formal_sample["execution"] / "observations.jsonl"
    row = json.loads(raw.read_text())
    row["typed_result"]["approval_result"]["head_before"] = 1
    # Keep the serialized group internally consistent; mismatch is with the Ledger observation.
    group = row["typed_result"]["persisted_binding"]["group_results"]
    next(x for x in group if x["failed_consumes"])["head_before"] = 1
    save(raw, row)
    path = formal_sample["execution"] / "execution.json"
    receipt = json.loads(path.read_text())
    receipt["raw_observations_hash"] = digest(raw)
    save(path, receipt)
    with pytest.raises(FormalCaseError, match="APPROVAL_LEDGER_OBSERVATION_MISMATCH"):
        formal_case(**formal_sample)
