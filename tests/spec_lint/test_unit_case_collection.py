"""Synthetic collector contract tests; these are not runtime evidence."""

from __future__ import annotations

import copy

import pytest

from collect_unit_cases import UnitCollectionError, validate_observations


def fixture():
    counts = {
        field: 0
        for field in (
            "network_calls",
            "process_launches",
            "workspace_commits",
            "external_effects",
            "ledger_effect_attempts",
        )
    }
    report = {
        "contract": "unit-execution-monitor/1",
        "complete": True,
        "scope": "pytest_python_test_function",
        "audit_probes": 2,
        "violations": [],
        "subject_type": "SCHEMA_VALIDATION_RESULT",
        "durability_observation": "NOT_APPLICABLE",
        "counts": counts,
        "driver_process_launches": 0,
        "child_reports": [],
        "assertions": [{"expression": "synthetic assertion", "result": True}],
    }
    row = {
        "case_id": "AT-SYNTHETIC/CASE",
        "node_id": "test_synthetic.py::test_case",
        "recorded": True,
        "unit_execution": report,
        "side_effects": dict(counts),
        "ledger_observed": False,
        "observed_event_sequence": [],
        "ledger_head_before": None,
        "ledger_head_after": None,
        "observed_state": "REJECTED",
        "observed_error_code": "SYNTHETIC_REJECTION",
        "actual_subject_id": "synthetic",
        "input_payload": {"synthetic": True},
    }
    expected = {
        "expected_subject_type": "SCHEMA_VALIDATION_RESULT",
        "expected_state": "REJECTED",
        "expected_error_code": "SYNTHETIC_REJECTION",
        "event_observation_policy": "NOT_APPLICABLE",
        "durability_tier": None,
    }
    return [row], expected


def validate(records, expected):
    return validate_observations(
        records, "AT-SYNTHETIC/CASE", ("test_synthetic.py::test_case",), expected
    )


def test_complete_observation_preserves_values():
    records, expected = fixture()
    result = validate(records, expected)
    assert result["subject_type"] == records[0]["unit_execution"]["subject_type"]
    assert result["counts"] == records[0]["side_effects"]
    assert all(a["result"] is True for a in result["assertions"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("complete", False),
        ("scope", "after_test"),
        ("audit_probes", 0),
        ("violations", ["hook_lost"]),
        ("subject_type", "OTHER"),
        ("durability_observation", None),
        ("counts", None),
        ("assertions", []),
        ("assertions", [{"expression": "failed", "result": False}]),
        ("driver_process_launches", 1),
    ],
)
def test_incomplete_or_mismatched_monitor_rejected(field, value):
    records, expected = fixture()
    records[0]["unit_execution"][field] = value
    with pytest.raises(UnitCollectionError):
        validate(records, expected)


@pytest.mark.parametrize(
    "field,value",
    [
        ("recorded", False),
        ("case_id", "OTHER"),
        ("node_id", "other::node"),
        ("unit_execution", None),
        ("side_effects", None),
        ("observed_state", "ACCEPTED"),
        ("observed_error_code", None),
        ("ledger_observed", True),
        ("ledger_head_after", 0),
        ("observed_event_sequence", ["invented"]),
    ],
)
def test_raw_observation_is_not_repaired_from_expectations(field, value):
    records, expected = fixture()
    records[0][field] = value
    with pytest.raises(UnitCollectionError):
        validate(records, expected)


@pytest.mark.parametrize("value", [1, -1, True, None])
def test_effect_counts_must_be_actual_zero_integers(value):
    records, expected = fixture()
    records[0]["unit_execution"]["counts"]["network_calls"] = value
    with pytest.raises(UnitCollectionError):
        validate(records, expected)


def test_duplicate_node_is_rejected():
    records, expected = fixture()
    records.append(copy.deepcopy(records[0]))
    with pytest.raises(UnitCollectionError):
        validate(records, expected)


@pytest.mark.parametrize(
    "field,value",
    [("event_observation_policy", "REQUIRED_EMPTY"), ("durability_tier", "T1_PROCESS_KILL")],
)
def test_registry_policy_does_not_implicitly_supply_missing_observation(field, value):
    records, expected = fixture()
    expected[field] = value
    with pytest.raises(UnitCollectionError):
        validate(records, expected)
