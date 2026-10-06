"""raw rc=1の条件付き受理と、失敗/古いReportを緑にしない操作Tool契約。"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import check_secret_scan_classification as classifier
import run_public_secret_scan as runner

pytestmark = pytest.mark.spec_lint
VALUE = "synthetic-" + "abcdefgh" + "-" + "0" * 8
FILE = "tests/test_values.py"


def _repo(tmp_path: Path) -> tuple[Path, str, Path]:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    line = "VALUE = " + json.dumps(VALUE)
    (repo / FILE).write_text("# fixture\n" + line + "\n")
    (repo / ".gitleaksignore").write_text(FILE + ":generic-api-key:2\n")
    (repo / "ci").mkdir()
    record = {
        "path": FILE,
        "rule": "generic-api-key",
        "line": 2,
        "line_sha256": hashlib.sha256(line.encode()).hexdigest(),
        "classification": "SYNTHETIC_TEST_VECTOR",
        "reason": "synthetic fixture",
        "evidence": {"0": classifier.synthetic_evidence(VALUE)},
    }
    (repo / "ci/secret-scan-classification.json").write_text(
        json.dumps(
            {
                "version": 1,
                "findings": [record],
            }
        )
    )
    for args in [
        ("init", "-q"),
        ("add", "."),
        (
            "-c",
            "user.name=Synthetic",
            "-c",
            "user.email=synthetic@example.invalid",
            "commit",
            "-qm",
            "synthetic",
        ),
    ]:
        subprocess.run(  # noqa: S603 — 固定Git操作、Shell無し。
            ["/usr/bin/git", *args], cwd=repo, check=True, timeout=10, capture_output=True
        )
    commit = subprocess.run(  # noqa: S603 — 固定Git読取り。
        ["/usr/bin/git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        timeout=10,
        capture_output=True,
        text=True,
    ).stdout.strip()
    scanner = tmp_path / "fake-scanner"
    scanner.write_text("synthetic scanner fixture")
    return repo, commit, scanner


def _fake_scanner(
    monkeypatch: pytest.MonkeyPatch,
    commit: str,
    *,
    failure: str | None = None,
) -> list[str]:
    original = runner._run
    observed: list[str] = []

    def run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "/usr/bin/git":
            return original(argv, cwd)
        kind = argv[1]
        raw = Path(argv[argv.index("--gitleaks-ignore-path") + 1]).name == "empty.ignore"
        if raw:
            assert not (cwd / ".gitleaksignore").exists()
            assert not (cwd / ".gitleaks.toml").exists()
        observed.append(kind + ("-raw" if raw else "-classified"))
        report = Path(argv[argv.index("--report-path") + 1])
        row: dict[str, Any] = {
            "File": FILE,
            "RuleID": "generic-api-key",
            "StartLine": 2,
            "Secret": "REDACTED",
        }
        if kind == "git":
            row["Commit"] = commit
        findings = [row] if raw else []
        rc = 1 if raw else 0
        if failure and raw:
            if failure == "missing":
                return subprocess.CompletedProcess(argv, 1, b"", b"candidate must not be logged")
            if failure == "malformed":
                report.write_text("{invalid")
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            if failure == "empty-bytes":
                report.write_bytes(b"")
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            if failure == "scanner-error":
                rc = 2
            elif failure == "zero-with-findings":
                rc = 0
            elif failure == "one-without-findings":
                findings = []
            elif failure == "unredacted":
                row["Secret"] = VALUE
            elif failure == "unknown-commit":
                row["Commit"] = "f" * 40
            elif failure == "unclassified":
                row["StartLine"] = 1
        report.write_text(json.dumps(findings))
        return subprocess.CompletedProcess(argv, rc, b"", b"candidate must not be logged")

    monkeypatch.setattr(runner, "_run", run)
    return observed


def test_raw_findings_are_accepted_only_after_classification_in_both_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, commit, scanner = _repo(tmp_path)
    calls = _fake_scanner(monkeypatch, commit)
    out = tmp_path / "evidence"
    code = runner.main(["--repo", str(repo), "--gitleaks", str(scanner), "--out", str(out)])
    receipt = json.loads((out / "receipt.json").read_text())
    assert code == 0 and receipt["status"] == "PASS"
    assert calls == ["git-raw", "git-classified", "dir-raw", "dir-classified"]
    assert [x["rc"] for x in receipt["stages"]] == [1, 0, 1, 0]
    assert [x["classification_rc"] for x in receipt["stages"] if x["raw"]] == [0, 0]
    assert (repo / ".gitleaksignore").is_file()
    assert not list(out.glob("secret-scan-*"))


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "malformed",
        "empty-bytes",
        "scanner-error",
        "zero-with-findings",
        "one-without-findings",
        "unredacted",
        "unknown-commit",
        "unclassified",
    ],
)
def test_failed_scan_or_report_is_not_a_zero_finding_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    repo, commit, scanner = _repo(tmp_path)
    _fake_scanner(monkeypatch, commit, failure=failure)
    out = tmp_path / "evidence"
    code = runner.main(["--repo", str(repo), "--gitleaks", str(scanner), "--out", str(out)])
    assert code != 0
    assert json.loads((out / "receipt.json").read_text())["status"] == "FAILED"
    text = capsys.readouterr().out
    assert VALUE not in text and "candidate must not be logged" not in text


def test_existing_report_directory_is_not_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, commit, scanner = _repo(tmp_path)
    calls = _fake_scanner(monkeypatch, commit)
    out = tmp_path / "evidence"
    out.mkdir()
    (out / "git-raw.json").write_text("[]")
    assert runner.main(["--repo", str(repo), "--gitleaks", str(scanner), "--out", str(out)]) == 2
    assert calls == []


def test_report_changed_during_classification_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, commit, scanner = _repo(tmp_path)
    _fake_scanner(monkeypatch, commit)
    original = classifier.main

    def changed(argv: list[str]) -> int:
        rc = original(argv)
        Path(argv[argv.index("--gitleaks-report") + 1]).write_text("[]")
        return rc

    monkeypatch.setattr(classifier, "main", changed)
    out = tmp_path / "evidence"
    assert runner.main(["--repo", str(repo), "--gitleaks", str(scanner), "--out", str(out)]) == 2
    receipt = json.loads((out / "receipt.json").read_text())
    assert receipt["error"] == "SCAN_REPORT_CHANGED_DURING_CLASSIFICATION"
