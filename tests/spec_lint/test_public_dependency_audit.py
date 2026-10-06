"""公開依存監査は欠測・部分結果・版差をPASSへ変換しない。"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import run_public_dependency_audit as gate

pytestmark = pytest.mark.spec_lint


def write_json(path: Path, value: Any) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "mutation",
    ["missing", "extra", "version", "duplicate", "skipped", "missing-vulns", "vulnerability"],
)
def test_partial_vulnerability_results_do_not_pass(tmp_path: Path, mutation: str) -> None:
    result: dict[str, Any] = {"dependencies": [{"name": "demo", "version": "1.0", "vulns": []}]}
    rows = result["dependencies"]
    if mutation == "missing":
        rows.clear()
    elif mutation == "extra":
        rows.append({"name": "other", "version": "1.0", "vulns": []})
    elif mutation == "version":
        rows[0]["version"] = "2.0"
    elif mutation == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    elif mutation == "skipped":
        rows[0]["skip_reason"] = "service-unavailable"
    elif mutation == "missing-vulns":
        del rows[0]["vulns"]
    elif mutation == "vulnerability":
        rows[0]["vulns"] = [{"id": "synthetic-finding"}]
    with pytest.raises(gate.AuditFailed):
        gate.validate_vulnerabilities(write_json(tmp_path / "audit.json", result), {"demo": "1.0"})


def test_audited_zero_is_bound_to_dependency_identity(tmp_path: Path) -> None:
    result = {"dependencies": [{"name": "Demo_Pkg", "version": "1.0", "vulns": []}]}
    assert gate.validate_vulnerabilities(
        write_json(tmp_path / "audit.json", result), {"demo-pkg": "1.0"}
    ) == {"dependencies": 1, "known_vulnerabilities": 0}


@pytest.mark.parametrize(
    "mutation", ["missing", "version", "duplicate", "format", "schema-version"]
)
def test_sbom_must_cover_all_declared_lock_pins(tmp_path: Path, mutation: str) -> None:
    value: dict[str, Any] = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "components": [{"name": "demo", "version": "1.0"}],
    }
    if mutation == "missing":
        value["components"] = []
    elif mutation == "version":
        value["components"][0]["version"] = "2.0"
    elif mutation == "duplicate":
        value["components"].append(copy.deepcopy(value["components"][0]))
    elif mutation == "format":
        value["bomFormat"] = "not-a-sbom"
    elif mutation == "schema-version":
        value["specVersion"] = "1.0"
    with pytest.raises(gate.AuditFailed):
        gate.validate_sbom(write_json(tmp_path / "sbom.json", value), {"demo": "1.0"})


@pytest.mark.parametrize("data", [b"", b"{", b"[]"])
def test_bad_report_is_not_an_empty_success(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "report.json"
    path.write_bytes(data)
    with pytest.raises(gate.AuditFailed):
        gate.validate_vulnerabilities(path, {})


def test_lock_requires_distribution_hash(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("demo==1.0\n", encoding="utf-8")
    with pytest.raises(gate.AuditFailed, match="HASH_MISSING"):
        gate._lock(tmp_path, "requirements.txt")


@pytest.fixture
def audit_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tools").mkdir()
    (repo / "ci").mkdir()
    lock = "demo==1.0 \\\n    --hash=sha256:" + "a" * 64 + "\n"
    for filename in ["requirements.txt", "requirements-dev.txt"]:
        (repo / filename).write_text(lock, encoding="utf-8")
    for filename in ["tools/check_licenses.py", "tools/run_public_dependency_audit.py"]:
        (repo / filename).write_text("# synthetic tool identity\n", encoding="utf-8")
    (repo / "ci/license-exceptions.yaml").write_text("exceptions: []\n", encoding="utf-8")
    for argv in [
        ["/usr/bin/git", "init", "-q"],
        ["/usr/bin/git", "add", "."],
        [
            "/usr/bin/git",
            "-c",
            "user.name=Synthetic Maintainer",
            "-c",
            "user.email=maintainer@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
    ]:
        subprocess.run(argv, cwd=repo, check=True, capture_output=True, timeout=30)  # noqa: S603 — 固定合成Git操作
    return repo


def fake_stage(
    argv: list[str], repo: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    assert env["HOME"] != str(Path.home())
    assert not any(
        name in env
        for name in [
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GOOGLE_API_KEY",
            "PIP_INDEX_URL",
            "HTTP_PROXY",
        ]
    )
    out_flag = "--out" if "--out" in argv else "--output" if "--output" in argv else "--output-file"
    path = Path(argv[argv.index(out_flag) + 1])
    if "tools/check_licenses.py" in argv:
        filename = argv[argv.index("--requirements") + 1]
        report: dict[str, Any] = {
            "status": "PASS",
            "requirements_sha256": hashlib.sha256((repo / filename).read_bytes()).hexdigest(),
            "runtime_lock_sha256": hashlib.sha256(
                (repo / "requirements.txt").read_bytes()
            ).hexdigest(),
            "exceptions_sha256": hashlib.sha256(
                (repo / "ci/license-exceptions.yaml").read_bytes()
            ).hexdigest(),
            "accepted_exceptions": 0,
            "dependencies": [
                {
                    "name": "demo",
                    "locked_version": "1.0",
                    "installed_version": "1.0",
                    "status": "PASS",
                }
            ],
        }
    elif "pip_audit" in argv:
        assert "--require-hashes" in argv and "--disable-pip" in argv and "--strict" in argv
        assert "--ignore-vuln" not in argv and "--fix" not in argv
        report = {"dependencies": [{"name": "demo", "version": "1.0", "vulns": []}]}
    else:
        assert "--validate" in argv and "--output-reproducible" in argv
        report = {
            "bomFormat": "CycloneDX",
            "specVersion": "1.6",
            "components": [{"name": "demo", "version": "1.0"}],
        }
    write_json(path, report)
    return subprocess.CompletedProcess(argv, 0, b"synthetic stage\n", b"")


def test_pipeline_binds_reports_commands_and_clean_commit(
    audit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "_run", fake_stage)
    out = tmp_path / "evidence"
    result = gate.run_audit(audit_repo, out)
    assert result["status"] == "PASS"
    assert len(result["stages"]) == 8
    assert all(stage["rc"] == 0 and stage["report_sha256"] for stage in result["stages"])
    assert gate._git(audit_repo, "rev-parse", "HEAD") == result["source"]["commit"]
    assert all(lock["sbom"]["reproduced"] for lock in result["locks"])
    with pytest.raises(gate.AuditFailed, match="NEW"):
        gate.run_audit(audit_repo, out)


@pytest.mark.parametrize(
    "failure",
    [
        "service",
        "missing",
        "partial",
        "license-policy",
        "license-version",
        "nonreproducible",
        "source-changed",
        "timeout",
    ],
)
def test_pipeline_does_not_publish_pass_after_stage_failure(
    audit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def defective(
        argv: list[str], repo: Path, env: dict[str, str]
    ) -> subprocess.CompletedProcess[bytes]:
        if failure == "timeout":
            raise gate.AuditFailed("AUDIT_TOOL_START_OR_TIMEOUT")
        result = fake_stage(argv, repo, env)
        if "tools/check_licenses.py" in argv and failure in {"license-policy", "license-version"}:
            path = Path(argv[argv.index("--out") + 1])
            value = json.loads(path.read_text())
            if failure == "license-policy":
                value["runtime_lock_sha256"] = "0" * 64
            else:
                value["dependencies"][0]["installed_version"] = "2.0"
            write_json(path, value)
        if "pip_audit" in argv:
            path = Path(argv[argv.index("--output") + 1])
            if failure == "service":
                return subprocess.CompletedProcess(argv, 1, b"", b"service failure")
            if failure == "missing":
                path.unlink()
            if failure == "partial":
                write_json(path, {"dependencies": []})
        if failure == "nonreproducible" and "repeat" in argv[-1]:
            value = json.loads(Path(argv[-1]).read_text())
            value["version"] = 2
            write_json(Path(argv[-1]), value)
        if failure == "source-changed" and "development-sbom-repeat" in argv[-1]:
            (repo / "requirements.txt").write_text("changed\n", encoding="utf-8")
        return result

    monkeypatch.setattr(gate, "_run", defective)
    out = tmp_path / "evidence"
    with pytest.raises(gate.AuditFailed):
        gate.run_audit(audit_repo, out)
    assert json.loads((out / "receipt.json").read_text())["status"] == "FAIL"


def test_dirty_or_in_repository_output_stops_before_tools(
    audit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("a tool must not run")

    monkeypatch.setattr(gate, "_run", forbidden)
    with pytest.raises(gate.AuditFailed, match="OUTSIDE"):
        gate.run_audit(audit_repo, audit_repo / "evidence")
    (audit_repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(gate.AuditFailed, match="CLEAN"):
        gate.run_audit(audit_repo, tmp_path / "evidence")
