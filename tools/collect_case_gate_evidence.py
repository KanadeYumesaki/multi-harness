#!/usr/bin/env python3
"""Collect fresh Case/Unit/Gate evidence and report gaps; never issue a Release."""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path

from case_runner import scan_case_wiring
from collect_release_baseline import collect as collect_baseline
from collect_release_baseline import digest
from collect_unit_cases import collect as collect_units
from emit_case_evidence import EvidenceEmissionError, _require_clean_tree
from emit_gate_evidence import emit_formal_gate, required_gate_cases
from emit_unit_area_evidence import AreaEvidenceError, _unit_case_ids, _write_new, verifier
from formal_case_evidence import FormalCaseError, formal_case
from run_case_evidence import collect as collect_cases


def collect(repo: Path, out: Path) -> dict:
    repo = repo.resolve(strict=True)
    out = out.resolve()
    if repo != Path(__file__).resolve().parents[1]:
        raise EvidenceEmissionError("COLLECTOR_NOT_FROM_MEASURED_WORKTREE")
    if repo == out or repo in out.parents or str(out).startswith("/mnt/"):
        raise EvidenceEmissionError("UNSAFE_COLLECTION_DESTINATION")
    if os.stat(out.parent).st_dev != os.stat(repo).st_dev:
        raise EvidenceEmissionError("COLLECTION_FILESYSTEM_MISMATCH")
    commit = _require_clean_tree(repo)
    inputs = {
        p: digest(p) for p in (repo / "registry-snapshot.json", repo / "verify_runtime_go.py")
    }
    # The existing baseline collector reserves out with exist_ok=False and
    # measures schema/migration/environment here, never rebinding old files.
    manifest = collect_baseline(repo, out)
    inputs.update({p: digest(p) for p in (out / "manifest.json", out / "environment.json")})
    snapshot = json.loads((repo / "registry-snapshot.json").read_text())
    scope = manifest["release_scope"]
    units = _unit_case_ids(snapshot, scope)
    wiring = scan_case_wiring(
        registries=repo / "design-source/registries", scope=scope, repo_root=repo
    )
    unit_summary = collect_units(repo, out, out / "manifest.json", out / "units")
    if not unit_summary["manifest_published"]:
        # Keep raw failed runs, then continue classifying remaining requirements.
        candidate = copy.deepcopy(manifest)
    else:
        candidate = json.loads((out / "units/manifest-with-units.json").read_text())
    orchestration = tuple(key for key in sorted(wiring) if key not in units)
    raw_out = out / "development"
    raw_summary = collect_cases(
        repo,
        repo / "design-source/registries",
        scope,
        out / "environment.json",
        raw_out,
        case_ids=orchestration,
    )
    emitted = dict(raw_summary["emitted"])
    failures = dict(raw_summary["failures"])
    rows_by_key = {row["test_id"] + "/" + row["case_id"]: row for row in candidate["test_cases"]}
    classification = {}
    unit_failures = {row["case_id"]: row["reason"] for row in unit_summary["failures"]}
    for key in sorted(snapshot["expectations"]):
        if key not in wiring:
            classification[key] = {"status": "OUT_OF_SCOPE"}
            continue
        item = wiring[key]
        info = {"nodes": list(item.observing_node_ids), "marker_nodes": list(item.marker_node_ids)}
        if not item.wired:
            info.update(status="UNWIRED", reason=item.unwired_reason)
        elif key in units:
            row = rows_by_key[key]
            if row.get("status") == "PASS":
                info.update(status="FORMAL_PASS", evidence_path=row["evidence_path"])
            else:
                info.update(
                    status="UNIT_OBSERVATION_INCOMPLETE",
                    reason=unit_failures.get(key, "UNIT_AREA_NOT_PUBLISHED"),
                )
        elif key in failures:
            info.update(status="RUNNER_FAILURE", reason=failures[key])
        elif key not in emitted:
            info.update(status="OBSERVATION_MISSING", reason="NO_EMITTED_CASE")
        else:
            try:
                body = formal_case(
                    key=key,
                    nodes=item.observing_node_ids,
                    legacy_path=raw_out / emitted[key],
                    execution=raw_out / "executions" / key,
                    fixture=raw_out / "fixtures" / (key + ".json"),
                    repo=repo,
                    root=out,
                    manifest=candidate,
                    expected=snapshot["expectations"][key],
                )
                path = out / "formal-cases" / (key + ".json")
                _write_new(path, body)
                rows_by_key[key].update(
                    status=body["status"],
                    input_fixture_hash=body["input_fixture_hash"],
                    evidence_path=path.relative_to(out).as_posix(),
                    evidence_manifest_hash=digest(path),
                )
                info.update(status="FORMAL_PASS", evidence_path=path.relative_to(out).as_posix())
            except FormalCaseError as exc:
                info.update(
                    status="FORMAL_OBSERVATION_INCOMPLETE",
                    reason=str(exc),
                    raw_execution_path=(raw_out / "executions" / key).relative_to(out).as_posix(),
                )
        classification[key] = info
    gates = {}
    for row in candidate["gates"]:
        gid = row["gate_id"]
        refs = required_gate_cases(snapshot, scope, gid)
        missing = [
            "/".join(pair)
            for pair in refs
            if classification["/".join(pair)]["status"] != "FORMAL_PASS"
        ]
        if missing:
            gates[gid] = {"status": "BLOCKED_CASE_EVIDENCE", "missing_case_ids": missing}
            continue
        path = out / "formal-gates" / (gid + ".json")
        body = emit_formal_gate(
            gid, manifest=candidate, snapshot=snapshot, evidence_root=out, out=path
        )
        row.update(
            status=body["status"],
            evidence_path=path.relative_to(out).as_posix(),
            evidence_manifest_hash=digest(path),
        )
        gates[gid] = {
            "status": "FORMAL_PASS",
            "case_refs": refs,
            "evidence_path": row["evidence_path"],
        }
    if _require_clean_tree(repo) != commit or any(digest(p) != h for p, h in inputs.items()):
        raise EvidenceEmissionError("COLLECTION_SOURCE_BINDING_CHANGED")
    path = out / "candidate-manifest.json"
    _write_new(path, candidate)
    report, code = verifier.verify(
        repo / "design-v1.25-runtime-go.md", path, out, repo / "registry-snapshot.json", scope
    )
    _write_new(out / "verification-report.json", report)
    summary = {
        "contract": "case-gate-collection/1",
        "implementation_commit_sha": commit,
        "release_scope": scope,
        "cases": classification,
        "gates": gates,
        "unit_collection": unit_summary,
        "verifier_exit_code": code,
        "verifier_decision": report["decision"],
        "verifier_errors": report.get("errors", []),
        "verifier_missing": report.get("missing", []),
        "release_issued": False,
        "verifier_source_hash": digest(repo / "verify_runtime_go.py"),
    }
    _write_new(out / "collection.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = collect(args.repo_root, args.out)
    except (EvidenceEmissionError, AreaEvidenceError, OSError, ValueError) as exc:
        print(type(exc).__name__ + ": " + str(exc))
        return 4
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "implementation_commit_sha",
                    "verifier_decision",
                    "verifier_exit_code",
                    "release_issued",
                )
            }
        )
    )
    # Partial collection is never disguised as a successful Release check.
    return result["verifier_exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
