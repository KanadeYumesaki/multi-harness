#!/usr/bin/env python3
"""Cleanな公開対象の固定依存・License・脆弱性照合・SBOMを新規採取する。

利用者のProvider認証・pip設定は渡さない。サービスへ送るのはLockのPackage名と版。
Python依存だけの検査であり、外部CLI/OS/Browserの監査やRuntime GOではない。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from check_licenses import applies_here, locked_requirements


class AuditFailed(Exception):
    """秘密値/外部stderrを含めない原因Code。"""


def _hash(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise AuditFailed("INPUT_OR_REPORT_NOT_REGULAR")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(  # noqa: S603 — 固定git操作、Shell無し。
            ["/usr/bin/git", *args],
            cwd=repo,
            capture_output=True,
            check=False,
            timeout=30,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": "/nonexistent",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            },
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuditFailed("GIT_READ_FAILED") from exc
    if result.returncode:
        raise AuditFailed("GIT_READ_FAILED")
    return result.stdout.decode("utf-8").strip()


def _binding(repo: Path) -> dict[str, Any]:
    if _git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise AuditFailed("SOURCE_NOT_CLEAN")
    inputs = [
        "requirements.txt",
        "requirements-dev.txt",
        "ci/license-exceptions.yaml",
        "tools/check_licenses.py",
        "tools/run_public_dependency_audit.py",
    ]
    return {
        "commit": _git(repo, "rev-parse", "HEAD"),
        "tree": _git(repo, "rev-parse", "HEAD^{tree}"),
        "files": [{"path": p, "sha256": _hash(repo / p)} for p in inputs],
    }


def _lock(repo: Path, filename: str) -> tuple[dict[str, str], dict[str, str]]:
    path = repo / filename
    _hash(path)
    text = path.read_text(encoding="utf-8")
    entries = locked_requirements(text)
    # License検査と同じ入口。加えて各Pinの配布物Hashを強制する。
    lines = text.splitlines()
    starts = [
        i
        for i, line in enumerate(lines)
        if line.strip() and not line.lstrip().startswith(("#", "-"))
    ]
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        if not any(
            re.fullmatch(r"--hash=sha256:[0-9a-f]{64}", line.strip().removesuffix("\\").strip())
            for line in lines[start + 1 : end]
        ):
            raise AuditFailed("LOCK_DISTRIBUTION_HASH_MISSING")
    all_pins = {canonicalize_name(e.name): e.version for e in entries}
    applicable = {canonicalize_name(e.name): e.version for e in entries if applies_here(e.marker)}
    return all_pins, applicable


def _json(path: Path) -> dict[str, Any]:
    _hash(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise AuditFailed("REPORT_JSON_INVALID") from exc
    if not isinstance(value, dict):
        raise AuditFailed("REPORT_JSON_NOT_OBJECT")
    return value


def _pins(rows: Any) -> dict[str, str]:
    if not isinstance(rows, list):
        raise AuditFailed("REPORT_DEPENDENCIES_NOT_LIST")
    pins: dict[str, str] = {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("name"), str)
            or not isinstance(row.get("version"), str)
        ):
            raise AuditFailed("REPORT_PACKAGE_IDENTITY_MISSING")
        name = canonicalize_name(row["name"], validate=True)
        if name in pins:
            raise AuditFailed("REPORT_PACKAGE_DUPLICATE")
        pins[name] = str(Version(row["version"]))
    return pins


def validate_vulnerabilities(path: Path, expected: dict[str, str]) -> dict[str, int]:
    result = _json(path)
    rows = result.get("dependencies")
    if _pins(rows) != {k: str(Version(v)) for k, v in expected.items()}:
        raise AuditFailed("VULNERABILITY_REPORT_LOCK_MISMATCH")
    findings = 0
    for row in rows:
        if row.get("skip_reason") or not isinstance(row.get("vulns"), list):
            raise AuditFailed("VULNERABILITY_PACKAGE_UNVERIFIED")
        findings += len(row["vulns"])
    if findings:
        raise AuditFailed("KNOWN_VULNERABILITIES_FOUND")
    return {"dependencies": len(rows), "known_vulnerabilities": findings}


def validate_sbom(path: Path, expected: dict[str, str]) -> dict[str, int]:
    result = _json(path)
    if result.get("bomFormat") != "CycloneDX" or result.get("specVersion") != "1.6":
        raise AuditFailed("SBOM_FORMAT_MISMATCH")
    if _pins(result.get("components")) != {k: str(Version(v)) for k, v in expected.items()}:
        raise AuditFailed("SBOM_LOCK_MISMATCH")
    return {"components": len(expected)}


def _run(argv: list[str], repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(  # noqa: S603 — 固定argv、Shell無し、pip再解決無し。
            argv,
            cwd=repo,
            env=env,
            capture_output=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuditFailed("AUDIT_TOOL_START_OR_TIMEOUT") from exc


def run_audit(repo: Path, out: Path) -> dict[str, Any]:
    repo = repo.resolve(strict=True)
    out = out.absolute()
    if out.exists() or out.is_symlink() or out.resolve().is_relative_to(repo):
        raise AuditFailed("OUTPUT_MUST_BE_NEW_AND_OUTSIDE_REPOSITORY")
    before = _binding(repo)
    locks = {name: _lock(repo, name) for name in ["requirements.txt", "requirements-dev.txt"]}
    out.mkdir(parents=True, exist_ok=False)
    home = out / "isolated-home"
    home.mkdir()
    env = {
        "PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
        "HOME": str(home),
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    receipt: dict[str, Any] = {
        "contract": "public-dependency-audit/1",
        "status": "FAIL",
        "source": before,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "tool_versions": {
            name: metadata.version(name) for name in ["pip-audit", "cyclonedx-bom", "packaging"]
        },
        "stages": [],
        "locks": [],
        "scope": "Python lock inputs only; not Runtime GO or legal certification",
    }

    def stage(argv: list[str], name: str, report: Path) -> None:
        if report.exists():
            raise AuditFailed("REPORT_ALREADY_EXISTS")
        result = _run(argv, repo, env)
        log = out / (name + ".log")
        log.write_bytes(result.stdout + result.stderr)
        receipt["stages"].append(
            {
                "name": name,
                "argv": [
                    arg.replace(str(out), "<evidence>")
                    .replace(str(repo), "<repo>")
                    .replace(sys.executable, "python")
                    for arg in argv
                ],
                "rc": result.returncode,
                "log": log.name,
                "log_sha256": _hash(log),
                "report": report.name,
                "report_sha256": _hash(report) if report.exists() else None,
            }
        )
        if result.returncode:
            raise AuditFailed("AUDIT_STAGE_FAILED:" + name)
        _hash(report)

    try:
        for filename, (declared, applicable) in locks.items():
            label = "runtime" if filename == "requirements.txt" else "development"
            license_path = out / (label + "-licenses.json")
            stage(
                [
                    sys.executable,
                    "tools/check_licenses.py",
                    "--requirements",
                    filename,
                    "--out",
                    str(license_path),
                ],
                label + "-licenses",
                license_path,
            )
            license_report = _json(license_path)
            license_rows = license_report.get("dependencies", [])
            if license_report.get("status") != "PASS" or license_report.get(
                "requirements_sha256"
            ) != _hash(repo / filename):
                raise AuditFailed("LICENSE_REPORT_FAILED_OR_UNBOUND")
            if _pins(
                [{"name": r.get("name"), "version": r.get("locked_version")} for r in license_rows]
            ) != {k: str(Version(v)) for k, v in declared.items()}:
                raise AuditFailed("LICENSE_REPORT_LOCK_MISMATCH")
            if license_report.get("runtime_lock_sha256") != _hash(
                repo / "requirements.txt"
            ) or license_report.get("exceptions_sha256") != _hash(
                repo / "ci/license-exceptions.yaml"
            ):
                raise AuditFailed("LICENSE_POLICY_BINDING_MISMATCH")
            for row in license_rows:
                expected_statuses = (
                    {"PASS", "ACCEPTED_BUILD_TOOLING_EXCEPTION"}
                    if row["name"] in applicable
                    else {"OUT_OF_SCOPE"}
                )
                if row["name"] in applicable and (
                    not isinstance(row.get("installed_version"), str)
                    or Version(row["installed_version"]) != Version(applicable[row["name"]])
                ):
                    raise AuditFailed("LICENSE_INSTALLED_VERSION_MISMATCH")
                if row.get("status") not in expected_statuses:
                    raise AuditFailed("LICENSE_PACKAGE_UNVERIFIED")
            audit_path = out / (label + "-vulnerabilities.json")
            stage(
                [
                    sys.executable,
                    "-m",
                    "pip_audit",
                    "--strict",
                    "--require-hashes",
                    "--no-deps",
                    "--disable-pip",
                    "--vulnerability-service",
                    "pypi",
                    "--timeout",
                    "15",
                    "--progress-spinner",
                    "off",
                    "--format",
                    "json",
                    "--desc",
                    "off",
                    "--aliases",
                    "off",
                    "-r",
                    filename,
                    "--output",
                    str(audit_path),
                ],
                label + "-vulnerabilities",
                audit_path,
            )
            vulnerabilities = validate_vulnerabilities(audit_path, applicable)
            sbom_path = out / (label + "-sbom.json")
            sbom_second = out / (label + "-sbom-repeat.json")
            for suffix, target in [("", sbom_path), ("-repeat", sbom_second)]:
                stage(
                    [
                        sys.executable,
                        "-m",
                        "cyclonedx_py",
                        "requirements",
                        filename,
                        "--spec-version",
                        "1.6",
                        "--output-reproducible",
                        "--validate",
                        "--output-file",
                        str(target),
                    ],
                    label + "-sbom" + suffix,
                    target,
                )
                validate_sbom(target, declared)
            if _hash(sbom_path) != _hash(sbom_second):
                raise AuditFailed("SBOM_NOT_REPRODUCIBLE")
            receipt["locks"].append(
                {
                    "path": filename,
                    "sha256": _hash(repo / filename),
                    "declared": len(declared),
                    "applicable": len(applicable),
                    "out_of_scope": sorted(set(declared) - set(applicable)),
                    "license_exceptions": license_report["accepted_exceptions"],
                    "vulnerability_audit": vulnerabilities,
                    "sbom": {
                        "components": len(declared),
                        "sha256": _hash(sbom_path),
                        "reproduced": True,
                    },
                }
            )
        if _binding(repo) != before:
            raise AuditFailed("SOURCE_CHANGED_DURING_AUDIT")
        receipt["status"] = "PASS"
    except (AuditFailed, OSError, ValueError, KeyError, TypeError) as exc:
        receipt["error"] = str(exc) if isinstance(exc, AuditFailed) else type(exc).__name__
        raise AuditFailed(receipt["error"]) from exc
    finally:
        (out / "receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--out", type=Path, required=True, help="new directory outside the repository"
    )
    args = parser.parse_args(argv)
    try:
        receipt = run_audit(args.repo, args.out)
    except (AuditFailed, OSError, ValueError, InvalidVersion) as exc:
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "reason": str(exc) if isinstance(exc, AuditFailed) else type(exc).__name__,
                }
            )
        )
        return 1
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "commit": receipt["source"]["commit"],
                "locks": receipt["locks"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
