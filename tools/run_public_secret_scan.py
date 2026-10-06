#!/usr/bin/env python3
"""固定した公開コピーのraw走査・履歴分類・除外後走査を一度の新規採取で結ぶ。

原Repositoryのignoreには触れない。履歴は独立bare mirror、作業木はignore/configを
収録しないGit archiveで走査する。終了値1を成功へ読み替えるのは、新鮮なRedacted
Reportと分類の完全一致を確認したraw段階だけである。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Literal

import check_secret_scan_classification as classifier


class ScanFailed(Exception):
    """値や外部Toolのstderrを含めない原因Code。"""


def _env() -> dict[str, str]:
    # 設定・Baselineの暗黙注入で「検出0」にしない。CLIの認証は使用しない。
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(  # noqa: S603 — 固定Tool操作。Shell無し。
            argv,
            cwd=cwd,
            env=_env(),
            capture_output=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScanFailed("TOOL_START_OR_TIMEOUT") from exc


def _git(root: Path, *args: str) -> bytes:
    result = _run(["/usr/bin/git", *args], root)
    if result.returncode:
        raise ScanFailed("GIT_READ_FAILED")
    return result.stdout


def _binding(repo: Path) -> dict[str, str]:
    if _git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise ScanFailed("SOURCE_NOT_CLEAN")
    return {
        "commit": _git(repo, "rev-parse", "HEAD").decode().strip(),
        "tree": _git(repo, "rev-parse", "HEAD^{tree}").decode().strip(),
        "refs_sha256": hashlib.sha256(
            _git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
        ).hexdigest(),
    }


def _archive(repo: Path, target: Path, commit: str) -> None:
    archive = _git(repo, "archive", "--format=tar", commit)
    target.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        for member in stream.getmembers():
            path = member.name.rstrip("/")
            if not classifier._is_exact_relative_path(path):
                raise ScanFailed("ARCHIVE_PATH_INVALID")
            if member.isdir():
                (target / path).mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ScanFailed("ARCHIVE_NOT_REGULAR")
            if path in {".gitleaksignore", ".gitleaks.toml"}:
                continue
            data = stream.extractfile(member)
            if data is None:
                raise ScanFailed("ARCHIVE_FILE_MISSING")
            destination = target / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data.read())


def _scan(
    scanner: Path,
    cwd: Path,
    kind: Literal["git", "dir"],
    ignore: Path,
    report: Path,
    *,
    raw: bool,
) -> dict[str, Any]:
    if report.exists():
        raise ScanFailed("SCAN_REPORT_ALREADY_EXISTS")
    argv = [
        str(scanner),
        kind,
        "--redact=100",
        "--ignore-gitleaks-allow",
        "--exit-code=1",
        "--gitleaks-ignore-path",
        str(ignore),
        "--report-format",
        "json",
        "--report-path",
        str(report),
    ]
    if kind == "git":
        argv.append("--log-opts=--all")
    argv.append(".")
    result = _run(argv, cwd)
    if result.returncode not in (0, 1) or not report.is_file() or report.is_symlink():
        raise ScanFailed("SCAN_FAILED_OR_REPORT_MISSING")
    try:
        data = report.read_bytes()
        findings = classifier.parse_gitleaks_report(json.loads(data), kind)
    except (SystemExit, UnicodeError, json.JSONDecodeError) as exc:
        raise ScanFailed("SCAN_REPORT_INVALID") from exc
    if (result.returncode == 1) != bool(findings):
        raise ScanFailed("SCAN_RC_FINDINGS_INCONSISTENT")
    if not raw and (result.returncode or findings):
        raise ScanFailed("UNCLASSIFIED_FINDINGS_REMAIN")
    return {
        "kind": kind,
        "raw": raw,
        "rc": result.returncode,
        "findings": len(findings),
        "report": report.name,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, required=True, help="Cleanな公開コピー")
    parser.add_argument("--gitleaks", type=Path, required=True, help="Checksum検証済み実行体")
    parser.add_argument("--out", type=Path, required=True, help="Repository外の新規Directory")
    args = parser.parse_args(argv)
    repo, scanner, out = args.repo.resolve(), args.gitleaks.resolve(), args.out.resolve()
    if out.is_relative_to(repo) or out.exists() or not scanner.is_file():
        print("secret scan input invalid: require a new external output and scanner")
        return 2
    receipt: dict[str, Any] = {"status": "FAILED", "stages": []}
    try:
        binding = _binding(repo)
        receipt["source"] = binding
        out.mkdir(parents=True)
        with tempfile.TemporaryDirectory(prefix="secret-scan-", dir=out) as scratch:
            work = Path(scratch)
            bare = work / "history.git"
            clone = _run(
                [
                    "/usr/bin/git",
                    "clone",
                    "--mirror",
                    "--local",
                    "--no-hardlinks",
                    str(repo),
                    str(bare),
                ],
                work,
            )
            if clone.returncode:
                raise ScanFailed("LOCAL_MIRROR_FAILED")
            tree = work / "tree"
            _archive(repo, tree, binding["commit"])
            empty_ignore = work / "empty.ignore"
            empty_ignore.write_text("")
            ignore = repo / ".gitleaksignore"
            for kind, cwd in [("git", bare), ("dir", tree)]:
                raw_report = out / (kind + "-raw.json")
                stage = _scan(scanner, cwd, kind, empty_ignore, raw_report, raw=True)
                receipt["stages"].append(stage)
                try:
                    code = classifier.main(
                        [
                            "--root",
                            str(repo),
                            "--report-kind",
                            kind,
                            "--gitleaks-report",
                            str(raw_report),
                        ]
                    )
                except SystemExit as exc:
                    raise ScanFailed("CLASSIFICATION_INPUT_INVALID") from exc
                stage["classification_rc"] = code
                if hashlib.sha256(raw_report.read_bytes()).hexdigest() != stage["sha256"]:
                    raise ScanFailed("SCAN_REPORT_CHANGED_DURING_CLASSIFICATION")
                if code:
                    raise ScanFailed("RAW_CLASSIFICATION_MISMATCH")
                receipt["stages"].append(
                    _scan(
                        scanner,
                        cwd,
                        kind,
                        ignore,
                        out / (kind + "-classified.json"),
                        raw=False,
                    )
                )
        if _binding(repo) != binding:
            raise ScanFailed("SOURCE_CHANGED_DURING_SCAN")
        receipt["status"] = "PASS"
    except (ScanFailed, OSError, UnicodeError, tarfile.TarError) as exc:
        receipt["error"] = str(exc) if isinstance(exc, ScanFailed) else type(exc).__name__
        print("public secret scan failed: " + receipt["error"])
    if out.is_dir():
        (out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print("public secret scan: " + receipt["status"])
    return 0 if receipt["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
