"""Synthetic rejection tests; no fixture here is runtime evidence."""

from __future__ import annotations

import copy
import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from case_observation import CaseObservation

from collect_release_baseline import digest
from emit_case_evidence import EvidenceEmissionError, _evidence_hash
from emit_gate_evidence import emit_formal_gate, required_gate_cases
from emit_unit_area_evidence import verifier
from formal_case_evidence import FormalCaseError, formal_case
from harness.application.approval_plan_orchestrator import AttemptGuardOutcome
from harness.domain.attempt import ActionAttempt
from harness.domain.errors import ErrorCode
from harness.domain.event_order import EventAppendRequest, verify_append_order

REPO = Path(__file__).resolve().parents[2]


def save(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body) + "\n")


@pytest.fixture
def sample(tmp_path):
    key = "AT-EVENT-ORDER-001/SEQUENCE_REGRESSION"
    node = "test_example.py::test_order"
    observation = CaseObservation(key, node)
    request = EventAppendRequest("append-1", "stream", 3, ("PLAN_RESOLVED",), "WAITING_POLICY")
    observation.record_result(verify_append_order(request, current_head=1))
    observation.record_input({"sequence": request.expected_stream_sequence})
    observation.observe_ledger(SimpleNamespace(appended=["PLAN_RESOLVED"], head=1), head_before=1)
    observation.observe_side_effects(
        network_calls=0,
        process_launches=0,
        workspace_commits=0,
        external_effects=0,
        ledger_effect_attempts=0,
    )
    row = observation.as_dict()
    expected = json.loads((REPO / "registry-snapshot.json").read_text())["expectations"][key]
    manifest = {
        "release_scope": "MVP0-A",
        "implementation_commit_sha": "a" * 40,
        "schema_set_hash": "sha256:" + "b" * 64,
        "migration_head": "1",
        "runtime_environment": {"environment_manifest_hash": "sha256:" + "c" * 64},
    }
    fixture = tmp_path / "fixture.json"
    save(fixture, row["input_payload"])
    execution = tmp_path / "execution"
    execution.mkdir()
    raw = execution / "observations.jsonl"
    save(raw, row)
    receipt = {
        "contract": "case-execution-record/1",
        "case_id": key,
        "exit_code": 0,
        "cwd": str(REPO),
        "raw_observations_hash": digest(raw),
        "runner_source_hash": digest(REPO / "tools/case_runner.py"),
        "command": ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", node],
        "started_at": "2026-09-25T00:00:00Z",
        "recorded_at": "2026-09-25T00:00:01Z",
    }
    save(execution / "execution.json", receipt)
    legacy = {
        "case_id": key,
        "status": "PASS",
        "implementation_commit_sha": manifest["implementation_commit_sha"],
        "environment_manifest_hash": manifest["runtime_environment"]["environment_manifest_hash"],
        "input_fixture_hash": digest(fixture),
        "expected": expected,
        "observed": {
            "state": row["observed_state"],
            "event_sequence": row["observed_event_sequence"],
        },
        "side_effects": row["side_effects"],
    }
    legacy["evidence_hash"] = _evidence_hash(legacy)
    save(tmp_path / "legacy.json", legacy)
    return {
        "key": key,
        "nodes": (node,),
        "legacy_path": tmp_path / "legacy.json",
        "execution": execution,
        "fixture": fixture,
        "repo": REPO,
        "root": tmp_path,
        "manifest": manifest,
        "expected": expected,
    }


def test_typed_production_result_and_raw_values_are_preserved(sample):
    doc = formal_case(**sample)
    assert doc["observed_subject_type"] == "EVENT_APPEND_RESULT"
    assert doc["actual_subject_id"] == "append-1"
    assert doc["observed_state"] == "REJECTED"
    assert doc["ledger_head_before"] == doc["ledger_head_after"] == 1
    assert doc["raw_result_hash"] == digest(sample["execution"] / "observations.jsonl")
    f = verifier.Findings()
    verifier.check_event_observation(doc, sample["expected"], sample["key"], f)
    verifier.verify_common_evidence(doc, sample["manifest"], sample["key"], f)
    assert f.ok, (f.errors, f.missing)


@pytest.mark.parametrize(
    "field,value",
    [
        ("typed_result", None),
        ("recorded", False),
        ("ledger_observed", False),
        ("ledger_head_after", None),
        ("ledger_head_before", True),
        ("side_effects", None),
        ("observed_state", "ACCEPTED"),
        ("observed_event_sequence", []),
        ("actual_subject_id", "different"),
        ("node_id", "unexecuted::test"),
        ("input_payload", {"changed": True}),
    ],
)
def test_missing_and_contradictory_raw_values_are_rejected(sample, field, value):
    raw = sample["execution"] / "observations.jsonl"
    row = json.loads(raw.read_text())
    row[field] = value
    save(raw, row)
    receipt_path = sample["execution"] / "execution.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["raw_observations_hash"] = digest(raw)
    save(receipt_path, receipt)
    with pytest.raises(FormalCaseError):
        formal_case(**sample)


@pytest.mark.parametrize(
    "field,value",
    [
        ("exit_code", 1),
        ("exit_code", False),
        ("cwd", "/another-checkout"),
        ("raw_observations_hash", "sha256:" + "0" * 64),
        ("runner_source_hash", "sha256:" + "0" * 64),
        ("command", ["echo", "PASS"]),
        ("command", None),
        ("started_at", "not-a-timestamp"),
    ],
)
def test_execution_receipt_is_bound(sample, field, value):
    path = sample["execution"] / "execution.json"
    body = json.loads(path.read_text())
    body[field] = value
    save(path, body)
    with pytest.raises(FormalCaseError, match="RECEIPT"):
        formal_case(**sample)


def test_durability_is_not_taken_from_registry(sample):
    sample["expected"]["durability_tier"] = "T2_STORAGE_SYNC"
    with pytest.raises(FormalCaseError, match="DURABILITY"):
        formal_case(**sample)


def test_old_commit_is_rejected(sample):
    sample["manifest"]["implementation_commit_sha"] = "d" * 40
    with pytest.raises(FormalCaseError, match="BINDING"):
        formal_case(**sample)


def test_unknown_result_class_is_not_a_subject():
    observation = CaseObservation("synthetic/case", "node")
    with pytest.raises(ValueError, match="UNSUPPORTED"):
        observation.record_result(SimpleNamespace(state="PASS"))
    assert observation.typed_result is None


def test_duplicate_observation_cannot_fake_node_coverage(sample):
    raw = sample["execution"] / "observations.jsonl"
    raw.write_bytes(raw.read_bytes() * 2)
    with pytest.raises(FormalCaseError, match="COVERAGE"):
        formal_case(**sample)


@pytest.fixture
def tree(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "synthetic_verifier_fixture", REPO / "tests/test_verify_runtime_go.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EvidenceTree(tmp_path)


def test_gate_uses_all_cases_per_test_and_unchanged_verifier(tree):
    gid = "MVP0A-GATE-01"
    out = tree.evidence_root / "new-gate.json"
    body = emit_formal_gate(
        gid, manifest=tree.manifest, snapshot=tree.snap, evidence_root=tree.evidence_root, out=out
    )
    assert body["case_refs"] == required_gate_cases(tree.snap, "MVP0-A", gid)
    row = next(row for row in tree.manifest["gates"] if row["gate_id"] == gid)
    row.update(evidence_path=out.name, evidence_manifest_hash=digest(out))
    f = verifier.Findings()
    verifier.verify_gates(tree.manifest, tree.snap, "MVP0-A", tree.evidence_root, f, {})
    assert f.ok, (f.errors, f.missing)
    with pytest.raises(FileExistsError):
        emit_formal_gate(
            gid,
            manifest=tree.manifest,
            snapshot=tree.snap,
            evidence_root=tree.evidence_root,
            out=out,
        )


@pytest.mark.parametrize(
    "fault", ["missing", "failed", "tamper", "duplicate", "old_commit", "missing_raw"]
)
def test_gate_rejects_any_incomplete_or_changed_reference(tree, fault):
    gid = "MVP0A-GATE-01"
    pair = required_gate_cases(tree.snap, "MVP0-A", gid)[0]
    row = next(x for x in tree.manifest["test_cases"] if [x["test_id"], x["case_id"]] == pair)
    path = tree.evidence_root / row["evidence_path"]
    if fault == "missing":
        tree.manifest["test_cases"].remove(row)
    elif fault == "failed":
        row["status"] = "FAIL"
    elif fault == "tamper":
        path.write_bytes(path.read_bytes() + b" ")
    elif fault == "duplicate":
        tree.manifest["test_cases"].append(copy.deepcopy(row))
    else:
        doc = json.loads(path.read_text())
        if fault == "missing_raw":
            (tree.evidence_root / doc["raw_result_path"]).unlink()
        else:
            doc["implementation_commit_sha"] = "d" * 40
            save(path, doc)
            row["evidence_manifest_hash"] = digest(path)
    out = tree.evidence_root / "must-not-publish.json"
    with pytest.raises(EvidenceEmissionError):
        emit_formal_gate(
            gid,
            manifest=tree.manifest,
            snapshot=tree.snap,
            evidence_root=tree.evidence_root,
            out=out,
        )
    assert not out.exists()


def test_all_scope_gate_requires_entire_scope(tree):
    gid = next(
        k
        for k, v in tree.snap["gates"].items()
        if v["test_refs_mode"] == "ALL_IN_SCOPE" and k in tree.scope["required_gate_ids"]
    )
    assert required_gate_cases(tree.snap, "MVP0-A", gid) == sorted(tree.scope["required_cases"])


@pytest.fixture
def attempt_result():
    attempt = ActionAttempt("measured-attempt", "measured-action", 1).block(
        state="BLOCKED_POLICY",
        error_classification="POLICY_DENIED",
        ended_at="2026-09-28T00:00:00Z",
    )
    return AttemptGuardOutcome(
        attempt_id=attempt.attempt_id,
        state=attempt.state,
        error_code=ErrorCode.FAULT_INJECTION_NOT_PERMITTED,
        events=(),
        ledger_head_before=0,
        ledger_head_after=1,
        attempt=attempt,
        effect_invocations=0,
    )


def test_attempt_observation_uses_production_identity(attempt_result):
    observation = CaseObservation("synthetic/case", "node")
    observation.record_result(attempt_result)
    assert observation.actual_subject_id == attempt_result.attempt.attempt_id
    assert observation.observed_state == attempt_result.attempt.state
    assert observation.observed_error_code == attempt_result.error_code.value
    assert observation.typed_result["subject_type"] == "ACTION_ATTEMPT"
    assert observation.typed_result["attempt_record"]["subject_id"] == "measured-attempt"
    assert observation.typed_result["effect_invocations"] == 0
    assert observation.side_effects is None
    assert observation.as_dict()["ledger_observed"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"attempt_id": "other-attempt"},
        {"state": "SUCCEEDED"},
        {"attempt": SimpleNamespace(attempt_id="measured-attempt", state="BLOCKED_POLICY")},
        {"effect_invocations": -1},
        {"effect_invocations": True},
    ],
)
def test_inconsistent_attempt_result_is_not_recorded(attempt_result, changes):
    observation = CaseObservation("synthetic/case", "node")
    with pytest.raises(ValueError, match="ATTEMPT_RESULT_INCONSISTENT"):
        observation.record_result(replace(attempt_result, **changes))
    assert observation.typed_result is None
    assert observation.as_dict()["recorded"] is False


def test_duck_typed_attempt_result_is_not_accepted(attempt_result):
    from dataclasses import asdict

    observation = CaseObservation("synthetic/case", "node")
    with pytest.raises(ValueError, match="UNSUPPORTED"):
        observation.record_result(SimpleNamespace(**asdict(attempt_result)))


@pytest.fixture
def attempt_sample(sample, attempt_result):
    # Synthetic serializer fixture; the SQLite integration cases supply runtime proof.
    key = "AT-FAULT-GUARD-001/POLICY_DENIED"
    expected = json.loads((REPO / "registry-snapshot.json").read_text())["expectations"][key]
    raw = sample["execution"] / "observations.jsonl"
    row = json.loads(raw.read_text())
    observation = CaseObservation(key, row["node_id"])
    observation.record_result(attempt_result)
    row.update(
        {
            k: observation.as_dict()[k]
            for k in (
                "case_id",
                "typed_result",
                "observed_state",
                "observed_error_code",
                "actual_subject_id",
            )
        }
    )
    row.update(
        ledger_head_before=0, ledger_head_after=1, observed_event_sequence=["ACTION_BLOCKED"]
    )
    save(raw, row)
    receipt_path = sample["execution"] / "execution.json"
    receipt = json.loads(receipt_path.read_text())
    receipt.update(case_id=key, raw_observations_hash=digest(raw))
    save(receipt_path, receipt)
    legacy = json.loads(sample["legacy_path"].read_text())
    legacy.update(
        case_id=key,
        expected=expected,
        observed={"state": row["observed_state"], "event_sequence": row["observed_event_sequence"]},
    )
    legacy["evidence_hash"] = _evidence_hash(legacy)
    save(sample["legacy_path"], legacy)
    return {**sample, "key": key, "expected": expected}


def test_formal_attempt_preserves_typed_subject(attempt_sample):
    doc = formal_case(**attempt_sample)
    assert doc["observed_subject_type"] == "ACTION_ATTEMPT"
    assert doc["actual_subject_id"] == "measured-attempt"
    assert doc["durability_tier"] is None


@pytest.mark.parametrize("fault", ["missing_record", "id", "state", "class", "count", "bool_count"])
def test_formal_attempt_rejects_contradictory_nested_record(attempt_sample, fault):
    raw = attempt_sample["execution"] / "observations.jsonl"
    row = json.loads(raw.read_text())
    typed = row["typed_result"]
    if fault == "missing_record":
        del typed["attempt_record"]
    elif fault in ("count", "bool_count"):
        typed["effect_invocations"] = 1 if fault == "count" else False
    else:
        field = {"id": "subject_id", "state": "state", "class": "result_class"}[fault]
        typed["attempt_record"][field] = "contradiction"
    save(raw, row)
    receipt_path = attempt_sample["execution"] / "execution.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["raw_observations_hash"] = digest(raw)
    save(receipt_path, receipt)
    with pytest.raises(FormalCaseError, match="ATTEMPT_RESULT_INCONSISTENT"):
        formal_case(**attempt_sample)
