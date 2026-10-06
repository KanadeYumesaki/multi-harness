#!/usr/bin/env python3
"""v1.7レビュー是正の最小否定系試験（stdlib only）。"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=True, check=False)


def test_verifier_integrity_detects_tamper() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        target = base / "verify_runtime_go.py"
        expected = base / "expected.sha256"
        target.write_text("original\n", encoding="utf-8")
        digest = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
        expected.write_text(f"{digest}  {target.name}\n", encoding="utf-8")
        ok = run(PYTHON, str(ROOT / "tools/check_verifier_integrity.py"),
                 "--target", str(target), "--expected", str(expected))
        assert ok.returncode == 0, ok.stderr
        trusted = run(PYTHON, str(ROOT / "tools/check_verifier_integrity.py"),
                      "--target", str(target), "--expected", str(expected),
                      "--trusted-hash", digest)
        assert trusted.returncode == 0, trusted.stderr
        rejected = run(PYTHON, str(ROOT / "tools/check_verifier_integrity.py"),
                       "--target", str(target), "--expected", str(expected),
                       "--trusted-hash", "sha256:" + "0" * 64)
        assert rejected.returncode == 3 and "TRUST_ANCHOR" in rejected.stderr
        target.write_text("tampered\n", encoding="utf-8")
        bad = run(PYTHON, str(ROOT / "tools/check_verifier_integrity.py"),
                  "--target", str(target), "--expected", str(expected))
        assert bad.returncode == 3 and "VERIFIER_SOURCE_TAMPERED" in bad.stderr


def test_ci_boundary_rejects_release_manifest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "runtime-go-release-manifest.json").write_text("{}", encoding="utf-8")
        env = os.environ.copy()
        env["CI"] = "true"
        result = run(PYTHON, str(ROOT / "tools/check_ci_runtime_boundary.py"),
                     "--workspace", str(base), "--ci", env=env)
        assert result.returncode == 3 and "CI_RUNTIME_ARTIFACT_FORBIDDEN" in result.stderr


def test_blocked_record_round_trip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        records = Path(tmp) / "records"
        command = [PYTHON, str(ROOT / "tools/create_blocked_record.py"),
                   "--task-id", "AT-HARDENING-001", "--release-scope", "MVP0-A",
                   "--category", "SPEC_CLARIFICATION", "--summary", "missing contract",
                   "--command", "pytest", "tests", "--exit-code", "2",
                   "--next-action", "ask owner", "--acceptance", "design updated",
                   "--owner", "reviewer", "--design",
                   str(ROOT / "design-v1.25-runtime-go.md"),
                   "--registry", str(ROOT / "registry-snapshot.json"),
                   "--out-dir", str(records)]
        created = run(*command)
        assert created.returncode == 0, created.stderr
        record = Path(created.stdout.strip())
        checked = run(PYTHON, str(ROOT / "tools/validate_blocked_record.py"), str(record),
                      "--design", str(ROOT / "design-v1.25-runtime-go.md"),
                      "--registry", str(ROOT / "registry-snapshot.json"))
        assert checked.returncode == 0, checked.stdout + checked.stderr


if __name__ == "__main__":
    # direct execution is useful in environments without pytest
    for test in (test_verifier_integrity_detects_tamper, test_ci_boundary_rejects_release_manifest,
                 test_blocked_record_round_trip):
        test()
    print("hardening contract tests: 3/3")
