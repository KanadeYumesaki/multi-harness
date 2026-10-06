"""Input evidence rejection tests; synthetic envelopes are not runtime proof."""

from __future__ import annotations

import copy
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_formal_case_gate_collection as formal_fixtures
from input_read_observation import InputReadEffectProbe, capture_input_read
from test_formal_case_gate_collection import save

from collect_release_baseline import digest
from emit_case_evidence import _evidence_hash
from formal_case_evidence import FormalCaseError, formal_case
from harness.infrastructure.artifact.store import ArtifactStore
from harness.infrastructure.masking.mock_masker import StaticPhraseMasker
from input_read_case_evidence import validate_input_read

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/integration/filesystem"))


@pytest.fixture
def measured(tmp_path):
    import test_input_read_orchestrator as fixtures

    workspace = fixtures.workspace.__wrapped__(tmp_path)
    connections = fixtures.connection.__wrapped__(tmp_path)
    connection = next(connections)
    resources = fixtures.orchestrator.__wrapped__(workspace, connection)
    try:
        orchestrator, ledger, _ = next(resources)
        request = fixtures._request("../outside.txt")
        effects = InputReadEffectProbe()
        with pytest.MonkeyPatch.context() as patch:
            effects.install(patch)
            result = orchestrator.read(request)
        typed = capture_input_read(result, request, ledger, effects, head_before=0)
        yield {
            "typed": typed,
            "result": result,
            "request": request,
            "ledger": ledger,
            "effects": effects,
        }
    finally:
        resources.close()
        connections.close()


def test_actual_sqlite_denial_is_metadata_only(measured):
    typed = measured["typed"]
    validate_input_read(typed)
    assert typed["payload_present"] is False
    assert typed["read_evidence_present"] is False
    assert typed["ledger"]["records"]
    assert "../outside.txt" not in json.dumps(typed)


@pytest.mark.parametrize(
    "path,value",
    [
        ("subject_id", "other"),
        ("state", "ALLOWED"),
        ("payload_present", True),
        ("read_evidence_present", True),
        ("classification_present", True),
        ("masking_denial_present", True),
        ("masker_invocations", 0),
        ("error_code", None),
        ("result_head_before", True),
        ("result_head_after", 99),
        ("result_events", []),
        ("request_binding.capability_path_hash", "different"),
        ("request_binding.stream_id", "other"),
        ("ledger.chain_valid", False),
        ("ledger.head_before", -1),
        ("ledger.records", []),
        ("effects.artifact_put_attempts", 1),
        ("effects.masker_invocations", 1),
        ("effects.process_launches", False),
        ("effects.network_calls", 1),
    ],
)
def test_metadata_contradictions_are_rejected(measured, path, value):
    typed = copy.deepcopy(measured["typed"])
    target = typed
    keys = path.split(".")
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    with pytest.raises(ValueError):
        validate_input_read(typed)


@pytest.mark.parametrize("fault", ["hash", "stream", "extra_body", "missing_probe", "record_extra"])
def test_missing_observation_and_unbound_ledger_are_rejected(measured, fault):
    typed = copy.deepcopy(measured["typed"])
    if fault == "hash":
        typed["ledger"]["records"][0]["payload_hash"] = "sha256:" + "0" * 64
    elif fault == "stream":
        typed["ledger"]["records"][0]["stream_id"] = "other"
    elif fault == "extra_body":
        typed["payload"] = "synthetic-body-must-not-be-exported"
    elif fault == "missing_probe":
        del typed["effects"]["artifact_put_attempts"]
    else:
        typed["ledger"]["records"][0]["payload"] = "unapproved-field"
    with pytest.raises(ValueError):
        validate_input_read(typed)


def test_capture_refuses_duck_type_and_retained_bytes_without_echoing_body(measured):
    for result in (
        SimpleNamespace(),
        replace(measured["result"], payload=b"synthetic-private-body"),
    ):
        with pytest.raises(ValueError) as caught:
            capture_input_read(
                result, measured["request"], measured["ledger"], measured["effects"], head_before=0
            )
        assert "synthetic-private-body" not in str(caught.value)


@pytest.mark.parametrize(
    "field,method",
    [
        ("artifact_put_attempts", ArtifactStore.put),
        ("masker_invocations", StaticPhraseMasker.propose_spans),
    ],
)
def test_effect_probe_denies_before_cas_or_masker_work(monkeypatch, field, method):
    probe = InputReadEffectProbe()
    probe.install(monkeypatch)
    owner = ArtifactStore if field == "artifact_put_attempts" else StaticPhraseMasker
    with pytest.raises(RuntimeError, match="INPUT_EFFECT_ATTEMPT"):
        getattr(owner, method.__name__)(None)
    assert getattr(probe, field) == 1


@pytest.fixture
def formal_sample(tmp_path, measured):
    sample = formal_fixtures.sample.__wrapped__(tmp_path / "formal")
    key = "AT-PATH-001/LINUX_ESCAPE"
    typed = measured["typed"]
    expected = json.loads((ROOT / "registry-snapshot.json").read_text())["expectations"][key]
    raw = sample["execution"] / "observations.jsonl"
    row = json.loads(raw.read_text())
    row.update(
        case_id=key,
        typed_result=typed,
        actual_subject_id=typed["subject_id"],
        observed_state=typed["state"],
        observed_error_code=typed["error_code"],
        observed_event_sequence=typed["result_events"],
        ledger_head_before=0,
        ledger_head_after=typed["result_head_after"],
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
        observed={"state": typed["state"], "event_sequence": typed["result_events"]},
    )
    legacy["evidence_hash"] = _evidence_hash(legacy)
    save(sample["legacy_path"], legacy)
    return {**sample, "key": key, "expected": expected}


def test_formal_collector_accepts_bound_input_denial(formal_sample):
    doc = formal_case(**formal_sample)
    assert doc["observed_subject_type"] == "INPUT_READ_DECISION"
    assert doc["durability_tier"] is None


@pytest.mark.parametrize("fault", ["hash", "raw_events", "raw_heads", "raw_effects"])
def test_formal_collector_rechecks_metadata_after_raw_hash_refresh(formal_sample, fault):
    raw = formal_sample["execution"] / "observations.jsonl"
    row = json.loads(raw.read_text())
    if fault == "hash":
        row["typed_result"]["ledger"]["records"][0]["payload_hash"] = "sha256:" + "0" * 64
    elif fault == "raw_events":
        row["observed_event_sequence"][-1] = "INPUT_READ_ALLOWED"
    elif fault == "raw_heads":
        row["ledger_head_before"] = 1
    else:
        row["side_effects"]["network_calls"] = 1
    save(raw, row)
    path = formal_sample["execution"] / "execution.json"
    receipt = json.loads(path.read_text())
    receipt["raw_observations_hash"] = digest(raw)
    save(path, receipt)
    with pytest.raises(FormalCaseError):
        formal_case(**formal_sample)
