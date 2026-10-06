"""Synthetic contract tests only; these fixtures are never Release evidence."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from case_observation import CaseObservation
from case_probe import observe_unit_case, registry_case
from ledger_probe import SideEffectProbe

from emit_unit_area_evidence import AreaEvidenceError, _unit_case_ids, build
from test_verify_runtime_go import EvidenceTree, write_json


@pytest.fixture
def tree(tmp_path):
    tree = EvidenceTree(tmp_path / "synthetic-contract")
    units = set(_unit_case_ids(tree.snap, "MVP0-A"))
    for row in tree.manifest["test_cases"]:
        if row["test_id"] + "/" + row["case_id"] not in units:
            continue
        path = tree.evidence_root / row["evidence_path"]
        doc = json.loads(path.read_text())
        doc["side_effects"] = {
            "network_calls": 0,
            "process_launches": 0,
            "workspace_commits": 0,
            "external_effects": 0,
            "ledger_effect_attempts": 0,
        }
        row["evidence_manifest_hash"] = write_json(path, doc)
    write_json(tree.manifest_path, tree.manifest)
    return tree


def emit(tree):
    return build(
        evidence_root=tree.evidence_root,
        snapshot_path=tree.registry_path,
        manifest_path=tree.manifest_path,
        scope="MVP0-A",
        out=tree.root / "new-area.json",
    )


def unit_row(tree):
    units = set(_unit_case_ids(tree.snap, "MVP0-A"))
    return next(
        row for row in tree.manifest["test_cases"] if row["test_id"] + "/" + row["case_id"] in units
    )


def test_area_uses_exact_manifest_file_hashes(tree):
    body = emit(tree)
    wanted = set(body["summary"]["case_ids"])
    hashes = sorted(
        row["evidence_manifest_hash"]
        for row in tree.manifest["test_cases"]
        if row["test_id"] + "/" + row["case_id"] in wanted
    )
    assert body["summary"]["case_evidence_hashes"] == hashes
    assert body["summary"]["unit_case_count"] == len(wanted)
    assert body["status"] == "PASS"


@pytest.mark.parametrize(
    "field,value",
    [
        ("implementation_commit_sha", "a" * 40),
        ("runtime_environment_hash", "sha256:" + "a" * 64),
        ("schema_set_hash", "sha256:" + "a" * 64),
        ("migration_head", "other"),
        ("expectation_descriptor_hash", "sha256:" + "a" * 64),
        ("observed_subject_type", "OTHER_SUBJECT"),
        ("observed_state", "OTHER_STATE"),
        ("event_observation_policy", "REQUIRED_EMPTY"),
        ("observed_event_sequence", []),
        ("assertions", []),
        ("exit_code", 1),
        ("side_effects", None),
        ("side_effects", {"network_calls": 0}),
    ],
)
def test_resealed_but_mismatched_case_is_rejected(tree, field, value):
    row = unit_row(tree)
    path = tree.evidence_root / row["evidence_path"]
    doc = json.loads(path.read_text())
    doc[field] = value
    row["evidence_manifest_hash"] = write_json(path, doc)
    write_json(tree.manifest_path, tree.manifest)
    with pytest.raises(AreaEvidenceError):
        emit(tree)
    assert not (tree.root / "new-area.json").exists()


@pytest.mark.parametrize("damage", ["bytes", "fixture", "raw", "missing", "duplicate"])
def test_case_file_and_reference_integrity(tree, damage):
    row = unit_row(tree)
    path = tree.evidence_root / row["evidence_path"]
    doc = json.loads(path.read_text())
    if damage == "bytes":
        path.write_bytes(path.read_bytes() + b" ")
    elif damage in ("fixture", "raw"):
        key = "input_fixture_path" if damage == "fixture" else "raw_result_path"
        (tree.evidence_root / doc[key]).write_bytes(b"changed")
    elif damage == "missing":
        path.unlink()
    else:
        tree.manifest["test_cases"].append(dict(row))
        write_json(tree.manifest_path, tree.manifest)
    with pytest.raises(AreaEvidenceError):
        emit(tree)
    assert not (tree.root / "new-area.json").exists()


@pytest.mark.parametrize("field", ["design_sha256", "registry_snapshot_hash", "source_tree_clean"])
def test_manifest_binding_rejected(tree, field):
    tree.manifest[field] = False if field == "source_tree_clean" else "sha256:" + "a" * 64
    write_json(tree.manifest_path, tree.manifest)
    with pytest.raises(AreaEvidenceError):
        emit(tree)


def test_saved_output_is_never_overwritten(tree):
    path = tree.root / "new-area.json"
    path.write_bytes(b"saved report")
    with pytest.raises(FileExistsError):
        emit(tree)
    assert path.read_bytes() == b"saved report"
    assert not list(tree.root.glob(".unit-area-*"))


def unit_observation():
    key = "AT-SCHEMA-COMPLETE-001/INVALID_HASH"
    obs = CaseObservation(key, "synthetic-validator-node")
    expected = registry_case(key)
    return obs, {
        "case_id": key,
        "state": expected["expected_state"],
        "subject_id": "synthetic",
        "error_code": expected["expected_error_code"],
        "payload": {"synthetic": True},
    }


def test_unconnected_default_probe_does_not_claim_zero_effects():
    obs, args = unit_observation()
    observe_unit_case(obs, **args)
    assert obs.side_effects is None
    assert obs.as_dict()["ledger_observed"] is False


def test_unmeasured_ledger_effect_count_is_not_filled_with_zero():
    obs, args = unit_observation()
    observe_unit_case(obs, **args, effects=SideEffectProbe())
    assert obs.side_effects is None


def test_explicit_probe_counters_are_preserved():
    obs, args = unit_observation()
    probe = SideEffectProbe()
    probe.launch(["synthetic"])
    observe_unit_case(obs, **args, effects=probe, ledger_effect_attempts=2)
    assert obs.side_effects.process_launches == 1
    assert obs.side_effects.external_effects == 1
    assert obs.side_effects.ledger_effect_attempts == 2
