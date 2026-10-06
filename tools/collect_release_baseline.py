#!/usr/bin/env python3
"""Release基礎領域を新規領域へ実測する。既存ReportとCaseは変更しない。"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

from emit_case_evidence import EvidenceEmissionError, _require_clean_tree


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def timestamp() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def measure_migration(database: Path) -> dict:
    from harness.infrastructure.sqlite.connection_factory import ConnectionFactory
    from harness.infrastructure.sqlite.migrations import migrate

    if database.exists():
        raise EvidenceEmissionError("FRESH_DATABASE_ALREADY_EXISTS")
    factory = ConnectionFactory(database)
    first = migrate(factory, recorded_at=timestamp())
    second = migrate(factory, recorded_at=timestamp())
    connection = factory.connect()
    try:
        versions = [
            row[0]
            for row in connection.execute("SELECT version FROM schema_migration ORDER BY version")
        ]
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        full_integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    finally:
        connection.close()
    ok = first == second == max(versions) and integrity == "ok" and full_integrity == ["ok"]
    if not ok:
        raise EvidenceEmissionError("MIGRATION_MEASUREMENT_FAILED")
    return {
        "migration_head": str(first),
        "fresh_install_passed": ok,
        "idempotent_second_install": first == second,
        "applied_versions": versions,
        "quick_check": integrity,
        "integrity_check": full_integrity,
    }


def measure_schemas(repo: Path) -> dict:
    from jsonschema import Draft202012Validator

    from harness.infrastructure.schema.registry import CoreSchemaRegistry

    sys.path.insert(0, str(repo / "tests/support"))
    from schema_fixtures import build_valid_instance

    registry = CoreSchemaRegistry(repo)
    checked = []
    for ref in registry.refs:
        path = repo / "schemas/core" / ref.schema_name / (ref.schema_version + ".schema.json")
        schema = json.loads(path.read_text())
        instance = build_valid_instance(schema)
        Draft202012Validator(schema).validate(instance)
        checked.append(
            {
                "schema_name": ref.schema_name,
                "schema_version": ref.schema_version,
                "schema_hash": digest(path),
            }
        )
    return {
        "schema_set_hash": registry.schema_set_hash(),
        "validated_schema_count": len(registry.schema_names),
        "validated_versions": checked,
    }


def measure_source_static(repo: Path, run: Callable[[str, list[str]], dict]) -> dict:
    """Bind successful checker executions to the measured tracked source bytes."""
    from package_release_zip import _git_executable, _source_tree_hash, should_include

    commit = _require_clean_tree(repo)
    result = subprocess.run(  # noqa: S603 - fixed Git command
        [_git_executable(), "ls-files", "-z"], cwd=repo, capture_output=True, timeout=30, check=True
    )
    tracked = [Path(name.decode("utf-8")) for name in result.stdout.split(b"\0") if name]
    required = [
        "src/harness/" + name
        for name in ("domain", "ports", "infrastructure", "application", "presentation")
    ]
    if any(not (repo / name).is_dir() for name in required):
        raise EvidenceEmissionError("STATIC_SOURCE_TARGET_MISSING")

    def snapshot() -> tuple[str, int]:
        entries = []
        for relative in tracked:
            if not should_include(relative):
                continue
            path = repo / relative
            if path.is_symlink() or not path.is_file():
                raise EvidenceEmissionError("STATIC_SOURCE_NOT_REGULAR")
            entries.append((relative.as_posix(), path.read_bytes()))
        tracked_set = {path.as_posix() for path in tracked}
        for path in (repo / "src").rglob("*.py"):
            if path.relative_to(repo).as_posix() not in tracked_set:
                raise EvidenceEmissionError("STATIC_UNTRACKED_SOURCE")
        return _source_tree_hash(entries), len(entries)

    before, file_count = snapshot()
    checks = [
        ("source-forbidden", [sys.executable, "tools/check_forbidden_patterns.py", "src"]),
        ("source-format", [sys.executable, "-m", "ruff", "format", "--check", "."]),
        ("source-lint", [sys.executable, "-m", "ruff", "check", "."]),
        ("source-types", [sys.executable, "-m", "mypy", "--strict", *required]),
        (
            "source-layers",
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/spec_lint/test_layer_dependencies.py",
                "-q",
                "-p",
                "no:cacheprovider",
            ],
        ),
    ]
    records = []
    for name, command in checks:
        record = run(name, command)
        if record.get("exit_code") != 0 or record.get("command") != command:
            raise EvidenceEmissionError("STATIC_CHECK_NOT_SUCCESSFUL")
        records.append(record)
    after, after_count = snapshot()
    if after != before or after_count != file_count or _require_clean_tree(repo) != commit:
        raise EvidenceEmissionError("STATIC_SOURCE_CHANGED_DURING_MEASUREMENT")
    return {
        "implementation_commit_sha": commit,
        "source_tree_clean": True,
        "source_tree_hash": before,
        "source_tree_hash_version": "1.0",
        "source_file_count": file_count,
        "checks": records,
        "hash_scope": "tracked files selected by package_release_zip.should_include",
    }


def collect(repo: Path, out: Path) -> dict:
    repo = repo.resolve(strict=True)
    out = out.resolve()
    commit = _require_clean_tree(repo)
    if repo != Path(__file__).resolve().parent.parent:
        raise EvidenceEmissionError("COLLECTOR_NOT_FROM_MEASURED_WORKTREE")
    if out == repo or repo in out.parents or str(out).startswith("/mnt/"):
        raise EvidenceEmissionError("UNSAFE_EVIDENCE_DESTINATION")
    if os.stat(out.parent).st_dev != os.stat(repo).st_dev:
        raise EvidenceEmissionError("EVIDENCE_ON_DIFFERENT_FILESYSTEM")
    out.mkdir(exist_ok=False)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(str(repo / p) for p in ("src", "tools"))
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    def run(name: str, args: list[str]) -> dict:
        start = timestamp()
        result = subprocess.run(  # noqa: S603 - fixed internal command lists
            args, cwd=repo, env=env, capture_output=True, timeout=300, check=False
        )
        log = out / (name + ".log")
        log.write_bytes(result.stdout + result.stderr)
        record = {
            "command": args,
            "exit_code": result.returncode,
            "started_at": start,
            "recorded_at": timestamp(),
            "log_path": log.name,
            "log_hash": digest(log),
        }
        (out / (name + "-execution.json")).write_text(json.dumps(record, indent=2) + "\n")
        if result.returncode:
            raise EvidenceEmissionError("BASELINE_COMMAND_FAILED: " + name)
        return record

    # 同じ集合のSchemaを実際に検証してから、その集合のHashを採る。
    schema_start = timestamp()
    schemas = measure_schemas(repo)
    migration_start = timestamp()
    migration = measure_migration(out / "fresh.sqlite3")
    from measure_backup_restore import measure_backup_restore

    backup = measure_backup_restore(out / "backup-probe", timestamp())
    (out / "backup-probe.json").write_text(json.dumps(backup, indent=2) + "\n")
    manifest_path = out / "manifest.json"
    run(
        "template",
        [
            sys.executable,
            "tools/build_manifest_template.py",
            "--registry",
            "registry-snapshot.json",
            "--release-scope",
            "MVP0-A",
            "--out",
            str(manifest_path),
        ],
    )
    manifest = json.loads(manifest_path.read_text())
    manifest.update(
        implementation_commit_sha=commit,
        source_tree_clean=True,
        schema_set_hash=schemas["schema_set_hash"],
        migration_head=migration["migration_head"],
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    run(
        "environment",
        [
            sys.executable,
            "tools/collect_runtime_environment.py",
            "--workspace",
            str(repo),
            "--manifest",
            str(manifest_path),
            "--python-lock",
            str(repo / "requirements-dev.txt"),
            "--out-root",
            str(out / "raw"),
        ],
    )
    raw_env = out / "raw/environment/runtime-environment.json"
    environment = json.loads(raw_env.read_text())
    # 別の新規文書へ現行共通契約を付ける。生の環境観測はBytes保持する。
    environment.update(
        evidence_schema_version="3.0",
        raw_environment_path=str(raw_env.relative_to(out)),
        raw_environment_hash=digest(raw_env),
    )
    environment["mountinfo_path"] = "raw/" + environment["mountinfo_path"]
    environment_path = out / "environment.json"
    environment_path.write_text(json.dumps(environment, indent=2) + "\n")
    manifest["runtime_environment"].update(
        environment_manifest_path=environment_path.name,
        environment_manifest_hash=digest(environment_path),
        is_wsl2=environment["is_wsl2"],
        workspace_on_linux_native_fs=environment["workspace_on_linux_native_fs"],
        python_version=environment["python_version"],
    )

    def area(name: str, summary: dict, start: str) -> None:
        body = {
            "evidence_schema_version": "3.0",
            "area": name,
            "status": "PASS",
            "release_scope": "MVP0-A",
            "implementation_commit_sha": commit,
            "schema_set_hash": schemas["schema_set_hash"],
            "migration_head": migration["migration_head"],
            "runtime_environment_hash": digest(environment_path),
            "summary": summary,
            "producer": "tools/collect_release_baseline.py",
            "test_run_id": out.name + "/" + name,
            "started_at": start,
            "recorded_at": timestamp(),
        }
        path = out / (name + ".json")
        path.write_text(json.dumps(body, indent=2) + "\n")
        row = next(a for a in manifest["required_evidence_areas"] if a["area"] == name)
        row.update(
            status=body["status"], evidence_path=path.name, evidence_manifest_hash=digest(path)
        )

    from reader_evidence import collect_reader

    reader_start = timestamp()
    # The existing observation fixture writes actual Ledger observations. Restrict
    # this sink to the reader collection; unrelated commands must not append to it.
    previous_sink = env.get("CASE_OBSERVATION_SINK")
    env["CASE_OBSERVATION_SINK"] = str(out / "reader-observations.jsonl")
    try:
        reader_summary = collect_reader(repo, out, run, digest)
    finally:
        if previous_sink is None:
            env.pop("CASE_OBSERVATION_SINK", None)
        else:
            env["CASE_OBSERVATION_SINK"] = previous_sink
    area("linux_safe_reader", reader_summary, reader_start)
    static_start = timestamp()
    static = measure_source_static(repo, run)
    area("source_static_analysis", static, static_start)
    area("core_schema_suite", schemas, schema_start)
    area("sqlite_migration", migration, migration_start)
    lint = run(
        "spec-lint",
        [
            sys.executable,
            "tools/lint_spec.py",
            "--design",
            "design-v1.25-runtime-go.md",
            "--registries",
            "design-source/registries",
            "--snapshot",
            "registry-snapshot.json",
            "--spec-dir",
            "spec",
            "--emit-evidence",
            str(out / "raw-spec-lint.json"),
        ],
    )
    lint_body = json.loads((out / "raw-spec-lint.json").read_text())
    if lint_body["status"] != "PASS" or lint_body["summary"]["violation_count"] != 0:
        raise EvidenceEmissionError("SPEC_LINT_RESULT_MISMATCH")
    area(
        "spec_lint",
        {**lint_body["summary"], "raw_result_hash": digest(out / "raw-spec-lint.json")},
        lint["started_at"],
    )
    xml = out / "verifier-tests.xml"
    test = run(
        "verifier-tests",
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_verify_runtime_go.py",
            "-q",
            "-p",
            "no:cacheprovider",
            "--junitxml",
            str(xml),
        ],
    )
    # 自分で起動したpytestの生成物だけ。外部XML・DTD・Entity宣言を拒否する。
    xml_bytes = xml.read_bytes()
    if b"<!DOCTYPE" in xml_bytes.upper() or b"<!ENTITY" in xml_bytes.upper():
        raise EvidenceEmissionError("UNSAFE_TEST_XML")
    suites = list(ET.fromstring(xml_bytes).iter("testsuite"))  # noqa: S314 - DTD rejected above
    totals = {
        k: sum(int(s.attrib[k]) for s in suites) for k in ("tests", "failures", "errors", "skipped")
    }
    if not totals["tests"] or any(totals[k] for k in ("failures", "errors", "skipped")):
        raise EvidenceEmissionError("VERIFIER_SELF_TEST_INCOMPLETE")
    area(
        "verifier_self_test",
        {
            "tests_run": totals["tests"],
            "failures": totals["failures"],
            "errors": totals["errors"],
            "verifier_source_sha256": digest(repo / "verify_runtime_go.py"),
            "raw_result_hash": digest(xml),
        },
        test["started_at"],
    )
    row = next(a for a in manifest["required_evidence_areas"] if a["area"] == "environment")
    row.update(
        status=environment["status"],
        evidence_path=environment_path.name,
        evidence_manifest_hash=digest(environment_path),
    )
    if _require_clean_tree(repo) != commit:
        raise EvidenceEmissionError("SOURCE_CHANGED_DURING_COLLECTION")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    result = collect(args.repo_root, args.out)
    print(
        json.dumps(
            {
                "release_decision": result["release_decision"],
                "areas": [
                    a["area"] for a in result["required_evidence_areas"] if a["status"] == "PASS"
                ],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
