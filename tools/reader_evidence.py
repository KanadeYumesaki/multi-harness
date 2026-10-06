"""Connect native reader observations and controlled boundary tests to an area."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

from case_runner import scan_case_wiring
from emit_case_evidence import EvidenceEmissionError

READER_TEST_IDS = ("AT-INPUT-PATH-001", "AT-PATH-001")


def read_test_totals(xml: Path) -> dict:
    raw = xml.read_bytes()
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise EvidenceEmissionError("UNSAFE_TEST_XML")
    suites = list(ET.fromstring(raw).iter("testsuite"))  # noqa: S314 - local pytest XML; DTD rejected
    totals = {
        key: sum(int(s.attrib[key]) for s in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }
    if totals["tests"] <= 0 or any(totals[k] != 0 for k in ("failures", "errors", "skipped")):
        raise EvidenceEmissionError("READER_SUITE_INCOMPLETE")
    return totals


def validate_case_observations(rows: list[dict], snapshot: dict) -> list[dict]:
    required = {
        test + "/" + case
        for test, case in snapshot["scopes"]["MVP0-A"]["required_cases"]
        if test in READER_TEST_IDS
    }
    selected = [row for row in rows if row.get("case_id") in required]
    if not required or {row["case_id"] for row in selected} != required:
        raise EvidenceEmissionError("READER_OBSERVATIONS_MISSING")
    for row in selected:
        expected = snapshot["expectations"][row["case_id"]]
        checks = (
            row.get("recorded") is True,
            row.get("ledger_observed") is True,
            bool(row.get("actual_subject_id")),
            row.get("observed_state") == expected["expected_state"],
            row.get("observed_error_code") == expected["expected_error_code"],
            row.get("observed_event_sequence") == expected["expected_event_sequence"],
        )
        before, after = row.get("ledger_head_before"), row.get("ledger_head_after")
        if (
            not all(checks)
            or type(before) is not int
            or type(after) is not int
            or before < 0
            or after - before != len(row["observed_event_sequence"])
        ):
            raise EvidenceEmissionError("READER_OBSERVATION_MISMATCH")
    return selected


def validate_native(body: dict) -> None:
    rows = body.get("observations")
    if (
        body.get("passed") is not True
        or not isinstance(rows, list)
        or not rows
        or any(row.get("passed") is not True for row in rows)
        or len({row.get("scenario") for row in rows}) != len(rows)
    ):
        raise EvidenceEmissionError("NATIVE_READER_CORPUS_FAILED")


def collect_reader(repo: Path, out: Path, run: Callable, digest: Callable) -> dict:
    snapshot = json.loads((repo / "registry-snapshot.json").read_text())
    native = []
    for mode, directory in (("auto", "r-a"), ("fallback", "r-f")):
        command = [
            sys.executable,
            "tools/measure_reader_corpus.py",
            "--repo-root",
            str(repo),
            "--out",
            str(out / directory),
            "--mode",
            mode,
        ]
        execution = run("reader-" + mode, command)
        path = out / directory / "result.json"
        body = json.loads(path.read_text())
        validate_native(body)
        native.append(
            {
                "mode": mode,
                "raw_path": str(path.relative_to(out)),
                "raw_hash": digest(path),
                "observation_count": len(body["observations"]),
                "execution": execution,
            }
        )
    # Do not let the old driver's requested-path label stand in for kernel capability.
    if not json.loads((out / "r-a/result.json").read_text())["native_openat2_available"]:
        raise EvidenceEmissionError("OPENAT2_BRANCH_NOT_MEASURED")
    executable = shutil.which("unshare")
    if executable is None:
        raise EvidenceEmissionError("MOUNT_NAMESPACE_UNAVAILABLE")
    mounts = []
    for kind in ("bind", "tmpfs"):
        for mode in ("openat2", "fallback"):
            name = "reader-mount-" + kind + "-" + mode
            base = out / name
            base.mkdir(exist_ok=False)
            execution = run(
                name,
                [
                    executable,
                    "--user",
                    "--map-root-user",
                    "--mount",
                    "--propagation",
                    "private",
                    sys.executable,
                    "tests/integration/filesystem/mount_crossing_driver.py",
                    str(repo),
                    str(base),
                    kind,
                    mode,
                ],
            )
            body = json.loads((out / execution["log_path"]).read_text().strip().splitlines()[-1])
            expected = snapshot["expectations"]["AT-INPUT-PATH-001/MOUNT_CROSSING"]
            if (
                body["crossing_denied"] is not True
                or body["crossing_error"] != expected["expected_error_code"]
                or body["crossing_leaked_bytes"] is not None
                or body["normal_allowed"] is not True
                or body["same_device"] != (kind == "bind")
                or body["observed_event_sequence"] != expected["expected_event_sequence"]
            ):
                raise EvidenceEmissionError("NATIVE_MOUNT_OBSERVATION_MISMATCH")
            mounts.append({"observation": body, "execution": execution})
    files = [
        "tests/integration/filesystem/" + name
        for name in (
            "test_safe_reader.py",
            "test_safe_reader_hardlinks.py",
            "test_read_limits.py",
            "test_capability_edges.py",
            "test_capability_lifecycle.py",
            "test_workspace_boundary.py",
            "test_boundary_fail_closed.py",
            "test_mount_crossing.py",
        )
    ]
    wiring = scan_case_wiring(
        registries=repo / "design-source/registries", scope="MVP0-A", repo_root=repo
    )
    nodes = sorted(
        {
            node
            for case, item in wiring.items()
            if case.partition("/")[0] in READER_TEST_IDS
            for node in item.marker_node_ids
            if node.partition("::")[0] not in files
        }
    )
    xml = out / "reader-tests.xml"
    # AF_UNIX addresses have a small fixed limit independent of evidence path length.
    # Only scratch fixtures are temporary; logs, XML and Ledger observations remain.
    with tempfile.TemporaryDirectory(prefix="hreader-", dir="/tmp") as scratch:
        execution = run(
            "reader-tests",
            [
                sys.executable,
                "-m",
                "pytest",
                *files,
                *nodes,
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                str(Path(scratch) / "t"),
                "--junitxml",
                str(xml),
            ],
        )
    totals = read_test_totals(xml)
    raw = out / "reader-observations.jsonl"
    rows = [json.loads(line) for line in raw.read_text().splitlines() if line.strip()]
    selected = validate_case_observations(rows, snapshot)
    return {
        "native_corpus": native,
        "native_mounts": mounts,
        "controlled_boundary_suite": {
            "totals": totals,
            "execution": execution,
            "raw_result_path": xml.name,
            "raw_result_hash": digest(xml),
        },
        "observed_case_ids": sorted({row["case_id"] for row in selected}),
        "case_observations_path": raw.name,
        "case_observations_hash": digest(raw),
        "registry_snapshot_hash": snapshot["registry_snapshot_hash"],
        "limits": [
            "blocked kernel syscalls are not preempted; late inputs are rejected",
            "network/FUSE/overlay policy tests inject mount metadata; no external mounts",
        ],
    }
