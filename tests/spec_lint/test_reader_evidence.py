"""Missing, failed or unobserved reader evidence cannot become a PASS area."""

import json
from pathlib import Path

import pytest

from emit_case_evidence import EvidenceEmissionError
from reader_evidence import (
    READER_TEST_IDS,
    read_test_totals,
    validate_case_observations,
    validate_native,
)


def test_native_pass_requires_nonempty_successful_unique_observations():
    for rows in (
        [],
        [{"scenario": "a", "passed": False}],
        [{"scenario": "a", "passed": True}, {"scenario": "a", "passed": True}],
    ):
        with pytest.raises(EvidenceEmissionError, match="NATIVE_READER_CORPUS_FAILED"):
            validate_native({"passed": True, "observations": rows})


@pytest.mark.parametrize("field", ["tests", "failures", "errors", "skipped"])
def test_reader_suite_refuses_incomplete_execution(tmp_path: Path, field: str):
    values = {"tests": 1, "failures": 0, "errors": 0, "skipped": 0}
    values[field] = 0 if field == "tests" else 1
    xml = tmp_path / "suite.xml"
    attributes = " ".join(f'{key}="{value}"' for key, value in values.items())
    xml.write_text(f"<testsuites><testsuite {attributes}/></testsuites>")
    with pytest.raises(EvidenceEmissionError, match="READER_SUITE_INCOMPLETE"):
        read_test_totals(xml)


@pytest.mark.parametrize("damage", ["missing", "error", "unobserved", "head"])
def test_registry_binding_rejects_incomplete_or_mismatched_observations(damage: str):
    repo = Path(__file__).resolve().parents[2]
    snapshot = json.loads((repo / "registry-snapshot.json").read_text())
    rows = []
    # Synthetic validator inputs only: never written as runtime evidence.
    for test, case in snapshot["scopes"]["MVP0-A"]["required_cases"]:
        if test not in READER_TEST_IDS:
            continue
        key = test + "/" + case
        expected = snapshot["expectations"][key]
        rows.append(
            {
                "case_id": key,
                "recorded": True,
                "ledger_observed": True,
                "actual_subject_id": "synthetic-validator-subject",
                "observed_state": expected["expected_state"],
                "observed_error_code": expected["expected_error_code"],
                "observed_event_sequence": expected["expected_event_sequence"],
                "ledger_head_before": 0,
                "ledger_head_after": len(expected["expected_event_sequence"]),
            }
        )
    assert rows
    assert validate_case_observations(rows, snapshot) == rows
    if damage == "missing":
        rows.pop()
    elif damage == "error":
        rows[0]["observed_error_code"] = None
    elif damage == "unobserved":
        rows[0]["ledger_observed"] = False
    else:
        rows[0]["ledger_head_after"] += 1
    with pytest.raises(EvidenceEmissionError, match="READER_OBSERVATION"):
        validate_case_observations(rows, snapshot)
