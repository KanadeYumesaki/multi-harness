#!/usr/bin/env python3
"""Execute Unit Cases in a clean native checkout and emit formal, bound evidence.

Saved manifests are inputs only. This produces a new candidate manifest, never a
Release approval. Registry expectations validate observations; they do not supply
observed Subject, counters, assertions, or state.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from case_runner import CaseRunnerError, build_pytest_adapters
from collect_release_baseline import digest
from emit_case_evidence import EvidenceEmissionError, _require_clean_tree
from emit_unit_area_evidence import (
    AreaEvidenceError,
    _unit_case_ids,
    _validate_binding,
    _write_new,
    build,
    verifier,
)


class UnitCollectionError(EvidenceEmissionError):
    pass


def validate_observations(
    records: list[dict], key: str, nodes: tuple[str, ...], expected: dict
) -> dict:
    if not records or any(
        row.get("case_id") != key or row.get("recorded") is not True for row in records
    ):
        raise UnitCollectionError("UNIT_OBSERVATION_MISSING")
    actual_nodes = [row.get("node_id") for row in records]
    if any(not isinstance(node, str) for node in actual_nodes) or len(set(actual_nodes)) != len(
        actual_nodes
    ):
        raise UnitCollectionError("UNIT_NODE_MISSING_OR_DUPLICATE")
    if {node.split("[", 1)[0] for node in actual_nodes} != set(nodes):
        raise UnitCollectionError("UNIT_NODE_COVERAGE_MISMATCH")
    if (
        expected.get("event_observation_policy") != "NOT_APPLICABLE"
        or expected.get("durability_tier") is not None
    ):
        raise UnitCollectionError("UNIT_POLICY_UNSUPPORTED")
    assertions = []
    for row in records:
        report = row.get("unit_execution")
        if (
            not isinstance(report, dict)
            or report.get("contract") != "unit-execution-monitor/1"
            or report.get("complete") is not True
        ):
            raise UnitCollectionError("UNIT_MONITOR_INCOMPLETE")
        if (
            report.get("scope") != "pytest_python_test_function"
            or report.get("audit_probes") != 2
            or report.get("violations") != []
        ):
            raise UnitCollectionError("UNIT_MONITOR_SCOPE_INVALID")
        children = report.get("child_reports")
        if (
            not isinstance(children, list)
            or type(report.get("driver_process_launches")) is not int
            or report["driver_process_launches"] != len(children)
        ):
            raise UnitCollectionError("UNIT_CHILD_COVERAGE_MISSING")
        for child in children:
            if (
                child.get("contract") != "unit-execution-monitor/1"
                or child.get("complete") is not True
                or child.get("audit_probes") != 2
                or child.get("violations") != []
                or child.get("scope") != "manifest_validator_cli_main"
                or child.get("child_reports") != []
                or child.get("driver_process_launches") != 0
            ):
                raise UnitCollectionError("UNIT_CHILD_MONITOR_INVALID")
            if child.get("counts") != report.get("counts"):
                raise UnitCollectionError("UNIT_CHILD_COUNTER_MISMATCH")
        counts = report.get("counts")
        fields = (
            "network_calls",
            "process_launches",
            "workspace_commits",
            "external_effects",
            "ledger_effect_attempts",
        )
        if (
            not isinstance(counts, dict)
            or set(counts) != set(fields)
            or any(type(counts.get(field)) is not int or counts[field] != 0 for field in fields)
        ):
            raise UnitCollectionError("UNIT_EFFECTS_NONZERO_OR_UNOBSERVED")
        if row.get("side_effects") != counts:
            raise UnitCollectionError("UNIT_COUNTER_MISMATCH")
        if report.get("durability_observation") != "NOT_APPLICABLE":
            raise UnitCollectionError("UNIT_DURABILITY_UNOBSERVED")
        if (
            row.get("ledger_observed") is not False
            or row.get("observed_event_sequence") != []
            or any(
                row.get(field) is not None for field in ("ledger_head_before", "ledger_head_after")
            )
        ):
            raise UnitCollectionError("UNIT_LEDGER_POLICY_MISMATCH")
        checks = {
            "subject": report.get("subject_type") == expected["expected_subject_type"],
            "state": row.get("observed_state") == expected["expected_state"],
            "error": row.get("observed_error_code") == expected["expected_error_code"],
        }
        observed_assertions = report.get("assertions")
        if (
            not isinstance(observed_assertions, list)
            or not observed_assertions
            or any(
                a.get("result") is not True or not a.get("expression") for a in observed_assertions
            )
        ):
            raise UnitCollectionError("UNIT_ASSERTION_FAILED_OR_MISSING")
        if not all(checks.values()):
            raise UnitCollectionError("UNIT_EXPECTATION_MISMATCH")
        assertions.extend(observed_assertions)
        assertions.extend(
            {"expression": row["node_id"] + ": " + name, "result": value}
            for name, value in checks.items()
        )
    first = records[0]
    for row in records[1:]:
        for field in (
            "input_payload",
            "observed_state",
            "observed_error_code",
            "actual_subject_id",
            "side_effects",
        ):
            if row.get(field) != first.get(field):
                raise UnitCollectionError("UNIT_OBSERVATIONS_DISAGREE")
    return {
        "subject_type": first["unit_execution"]["subject_type"],
        "assertions": assertions,
        "counts": first["side_effects"],
    }


def formal_case(
    *,
    result: Any,
    records: list[dict],
    receipt: dict,
    expected: dict,
    manifest: dict,
    root: Path,
    raw: Path,
    repo: Path,
) -> dict:
    measured = validate_observations(records, result.case_id, result.test_node_ids, expected)
    if (
        receipt.get("contract") != "case-execution-record/1"
        or receipt.get("case_id") != result.case_id
        or receipt.get("exit_code") != 0
        or receipt.get("cwd") != str(repo)
    ):
        raise UnitCollectionError("UNIT_EXECUTION_RECEIPT_INVALID")
    if receipt.get("raw_observations_hash") != digest(raw) or receipt.get(
        "runner_source_hash"
    ) != digest(repo / "tools/case_runner.py"):
        raise UnitCollectionError("UNIT_EXECUTION_HASH_MISMATCH")
    test_id, case_id = result.case_id.split("/")
    return {
        "evidence_schema_version": "3.0",
        "release_scope": manifest["release_scope"],
        "test_id": test_id,
        "case_id": case_id,
        "status": "PASS" if all(a["result"] is True for a in measured["assertions"]) else "FAIL",
        "producer": "tools/collect_unit_cases.py",
        "test_run_id": result.case_id,
        **{
            field: receipt[field]
            for field in ("command", "exit_code", "started_at", "recorded_at", "runner_source_hash")
        },
        **{
            field: manifest[field]
            for field in ("implementation_commit_sha", "schema_set_hash", "migration_head")
        },
        "runtime_environment_hash": manifest["runtime_environment"]["environment_manifest_hash"],
        "input_fixture_path": result.input_fixture_path.relative_to(root).as_posix(),
        "input_fixture_hash": digest(result.input_fixture_path),
        "raw_result_path": raw.relative_to(root).as_posix(),
        "raw_result_hash": digest(raw),
        "expectation_descriptor_hash": expected["expectation_descriptor_hash"],
        "observed_subject_type": measured["subject_type"],
        "observed_state": result.observed_state,
        "observed_error_code": result.observed_error_code,
        "actual_subject_id": result.actual_subject_id,
        "evidence_kind": "UNIT",
        "event_observation": "NOT_APPLICABLE",
        "event_observation_policy": "NOT_APPLICABLE",
        "observed_event_sequence": None,
        "durability_tier": None,
        "side_effects": measured["counts"],
        "assertions": measured["assertions"],
    }


def collect(repo: Path, root: Path, manifest_path: Path, out: Path) -> dict:
    repo, root, manifest_path = (
        repo.resolve(strict=True),
        root.resolve(strict=True),
        manifest_path.resolve(strict=True),
    )
    out = out.resolve()
    if (
        repo != Path(__file__).resolve().parents[1]
        or root not in out.parents
        or root == repo
        or repo in root.parents
        or str(root).startswith("/mnt/")
    ):
        raise UnitCollectionError("UNSAFE_UNIT_EVIDENCE_DESTINATION")
    if os.stat(root).st_dev != os.stat(repo).st_dev:
        raise UnitCollectionError("UNIT_EVIDENCE_FILESYSTEM_MISMATCH")
    snapshot_path = repo / "registry-snapshot.json"
    snapshot = json.loads(snapshot_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    scope = manifest["release_scope"]
    _validate_binding(snapshot, manifest, scope, root)
    commit = _require_clean_tree(repo)
    if commit != manifest["implementation_commit_sha"]:
        raise UnitCollectionError("UNIT_COMMIT_MISMATCH")
    finding = verifier.Findings()
    environment_path = verifier.resolve_evidence(
        root,
        manifest["runtime_environment"]["environment_manifest_path"],
        "unit environment",
        finding,
    )
    if environment_path is None or not finding.ok:
        raise UnitCollectionError("UNIT_ENVIRONMENT_PATH_INVALID")
    environment = json.loads(environment_path.read_text())
    if (
        environment.get("workspace_root") != str(repo)
        or environment.get("is_wsl2") is not True
        or environment.get("workspace_on_linux_native_fs") is not True
    ):
        raise UnitCollectionError("UNIT_NATIVE_ENVIRONMENT_REQUIRED")
    inputs = {path: digest(path) for path in (snapshot_path, manifest_path, environment_path)}
    units = _unit_case_ids(snapshot, scope)
    adapters = build_pytest_adapters(
        registries=repo / "design-source/registries",
        scope=scope,
        repo_root=repo,
        fixtures_dir=out / "fixtures",
    )
    if not units or set(units) - set(adapters):
        raise UnitCollectionError("UNIT_CASE_UNWIRED")
    out.mkdir(parents=True, exist_ok=False)
    candidate = copy.deepcopy(manifest)
    emitted, failures = [], []
    for key in units:
        adapter = replace(adapters[key], execution_records_dir=out / "executions")
        try:
            result = adapter()
            execution = out / "executions" / key
            raw = execution / "observations.jsonl"
            records = [json.loads(line) for line in raw.read_text().splitlines() if line.strip()]
            receipt = json.loads((execution / "execution.json").read_text())
            body = formal_case(
                result=result,
                records=records,
                receipt=receipt,
                expected=snapshot["expectations"][key],
                manifest=manifest,
                root=root,
                raw=raw,
                repo=repo,
            )
            path = out / "cases" / (key + ".json")
            _write_new(path, body)
            rows = [
                row
                for row in candidate["test_cases"]
                if row["test_id"] + "/" + row["case_id"] == key
            ]
            if len(rows) != 1:
                raise UnitCollectionError("UNIT_MANIFEST_ROW_MISSING_OR_DUPLICATE")
            rows[0].update(
                status=body["status"],
                input_fixture_hash=body["input_fixture_hash"],
                evidence_path=path.relative_to(root).as_posix(),
                evidence_manifest_hash=digest(path),
            )
            emitted.append(key)
        except (CaseRunnerError, EvidenceEmissionError) as exc:
            failures.append({"case_id": key, "reason": str(exc)})
    if _require_clean_tree(repo) != commit or any(
        digest(path) != value for path, value in inputs.items()
    ):
        raise UnitCollectionError("UNIT_COLLECTION_BINDING_CHANGED")
    summary = {
        "contract": "unit-case-collection/1",
        "implementation_commit_sha": commit,
        "required_case_ids": units,
        "emitted_case_ids": emitted,
        "failures": failures,
        "release_issued": False,
        "manifest_published": False,
    }
    if not failures:
        draft = out / "cases-manifest.json"
        _write_new(draft, candidate)
        area_path = out / "unit-case-suite.json"
        build(
            evidence_root=root,
            snapshot_path=snapshot_path,
            manifest_path=draft,
            scope=scope,
            out=area_path,
        )
        rows = [
            row for row in candidate["required_evidence_areas"] if row["area"] == "unit_case_suite"
        ]
        if len(rows) != 1:
            raise UnitCollectionError("UNIT_AREA_MISSING_OR_DUPLICATE")
        rows[0].update(
            status="PASS",
            evidence_path=area_path.relative_to(root).as_posix(),
            evidence_manifest_hash=digest(area_path),
        )
        _write_new(out / "manifest-with-units.json", candidate)
        summary["manifest_published"] = True
    _write_new(out / "collection.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = collect(args.repo_root, args.evidence_root, args.manifest, args.out)
    except (EvidenceEmissionError, AreaEvidenceError) as exc:
        print(str(exc))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(bool(result["failures"]))


if __name__ == "__main__":
    raise SystemExit(main())
