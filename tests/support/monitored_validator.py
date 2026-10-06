"""Instrument the manifest CLI SUT in its own Python process.

Only this fixed driver is excluded from the parent SUT counters. The child runs
argparse and the real __main__ path; its complete monitor report is mandatory.
CLI stdout is captured in memory and transported after the observed call.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path

import unit_execution_monitor as monitoring


def run_validator(directory: Path) -> subprocess.CompletedProcess[str]:
    parent = monitoring._ACTIVE
    if parent is None:
        raise RuntimeError("UNIT_PARENT_MONITOR_REQUIRED")
    child = None
    parent.suspend_driver()
    try:
        with tempfile.TemporaryDirectory(prefix="unit-validator-") as scratch:
            report_path = Path(scratch) / "report.json"
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run(  # noqa: S603 - fixed local validator driver, list argv
                [
                    sys.executable,
                    "-B",
                    str(Path(__file__).resolve()),
                    str(directory.resolve()),
                    str(report_path),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                env=env,
            )
            if report_path.is_file():
                child = json.loads(report_path.read_text())
                if child.get("exit_code") != result.returncode or any(
                    child.get(name + "_hash") != hashlib.sha256(value.encode()).hexdigest()
                    for name, value in (("stdout", result.stdout), ("stderr", result.stderr))
                ):
                    child = None
            return result
    finally:
        parent.resume_driver(child)


def main() -> int:
    directory, report_path = map(Path, sys.argv[1:])
    validator = Path(__file__).resolve().parents[2] / "tools/validate_test_manifest.py"
    sys.argv = [str(validator), "--registries", str(directory)]
    stdout, stderr = io.StringIO(), io.StringIO()
    monitor = monitoring.UnitExecutionMonitor()
    code = 0
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        monitor.start()
        try:
            runpy.run_path(str(validator), run_name="__main__")
        except SystemExit as exc:
            if type(exc.code) is not int:
                raise RuntimeError("UNEXPECTED_VALIDATOR_EXIT") from exc
            code = exc.code
        finally:
            report = monitor.finish()
    report["scope"] = "manifest_validator_cli_main"
    report["exit_code"] = code
    report["stdout_hash"] = hashlib.sha256(stdout.getvalue().encode()).hexdigest()
    report["stderr_hash"] = hashlib.sha256(stderr.getvalue().encode()).hexdigest()
    report_path.write_text(json.dumps(report), encoding="utf-8")
    sys.stdout.write(stdout.getvalue())
    sys.stderr.write(stderr.getvalue())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
