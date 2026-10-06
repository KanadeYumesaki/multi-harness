#!/usr/bin/env python3
"""`backup_restore` の観測・判定・公開に欠陥を注入し、**試験が実際に落ちる**ことを測る。

## 稼働Codeを書き換えない

注入先は `git worktree add --detach` で作る**隔離Worktree**である。測定対象の
作業木・共有の稼働Codeには触れない。各注入の後で元Bytesへ戻し、Hash一致を確かめる。

## 何を注入するか

| 種別 | 欠陥 |
|---|---|
| 本番 | 受付Gateが停止状態を無視して常に通す |
| 本番 | Drainが `cli_invocation_journal` の進行中を数えない |
| 判定 | 欠測（`Missing`）を PASS に数える |
| 判定 | 拒否理由を問わず「Gateが拒否した」と見なす |
| 公開 | 出力先の再利用を拒否しない |
| 公開 | Manifest の Commit 不一致を拒否しない |

注入して試験が**落ちない**なら、その試験は欠陥を検出していない。落ちたことを
終了値とログHashで記録する。注入前の同じ試験が通ることも同時に測る。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

AREA_TESTS = "tests/integration/p3/test_backup_restore_area_observation.py"
COLLECTOR_TESTS = "tests/spec_lint/test_backup_restore_collector.py"
PRODUCT_TESTS = "tests/integration/workbench/test_operation_control.py"


@dataclass(frozen=True)
class Mutant:
    name: str
    kind: str
    file: str
    old: str
    new: str
    tests: tuple[str, ...]
    detects: str


MUTANTS: tuple[Mutant, ...] = (
    Mutant(
        name="PRODUCT_GATE_ADMITS_EVERYTHING",
        kind="production",
        file="src/harness/application/intake_gate.py",
        old='            allowed = mode == "OPEN" or (kind == "effect" and mode == "DRAINING")',
        new="            allowed = True  # mutant: the gate ignores the operation mode",
        tests=(f"{AREA_TESTS}::test_real_observation_satisfies_every_predicate",),
        detects="復元区間・Drain後の通常入口が拒否されないことをCollectorが落とす",
    ),
    Mutant(
        name="PRODUCT_DRAIN_IGNORES_CLI_JOURNAL",
        kind="production",
        file="src/harness/infrastructure/sqlite/operations_repository.py",
        old=(
            "ACTIVE_CLI_INVOCATION_STATES: tuple[str, ...] = (\n"
            '    "PREPARED_DURABLE",\n'
            '    "EXECUTION_ATTEMPTED",\n'
            '    "EFFECT_UNKNOWN",\n'
            ")"
        ),
        new="ACTIVE_CLI_INVOCATION_STATES: tuple[str, ...] = ()  # mutant: claims are not counted",
        tests=(
            f"{AREA_TESTS}::test_real_observation_satisfies_every_predicate",
            f"{PRODUCT_TESTS}::test_drain_counts_a_claimed_send_that_was_never_dispatched",
        ),
        detects="claim済み送信があるのにDrainがACCEPTEDを返すことを落とす",
    ),
    Mutant(
        name="PREDICATE_MISSING_COUNTS_AS_PASS",
        kind="predicate",
        file="tools/backup_restore_predicates.py",
        old='        return _result(check_id, item, requirement, UNVERIFIED, f"MISSING: {exc}")',
        new='        return _result(check_id, item, requirement, PASS, f"MISSING: {exc}")',
        tests=(
            f"{AREA_TESTS}::test_missing_observation_is_unverified_not_pass",
            f"{COLLECTOR_TESTS}::test_area_status_is_never_pass_with_missing_observations",
        ),
        detects="欠測をPASSへ倒す欠陥を試験が検出する",
    ),
    Mutant(
        name="PREDICATE_ACCEPTS_ANY_REFUSAL",
        kind="predicate",
        file="tools/backup_restore_predicates.py",
        old=(
            "    expect(\n"
            "        exception_type == REFUSAL_EXCEPTION,\n"
            '        f"{label}: refused by {exception_type}, not by the operation gate",\n'
            "    )\n"
            '    expect(code == REFUSAL_CODE, f"{label}: refusal code {code!r} is not '
            '{REFUSAL_CODE}")'
        ),
        new="    return  # mutant: any refusal reason is accepted",
        tests=(
            f"{COLLECTOR_TESTS}::test_refusal_must_come_from_the_operation_gate",
            f"{AREA_TESTS}::test_window_refusals_must_be_gate_refusals_from_another_thread",
        ),
        detects="別理由の拒否を保護の証拠にする欠陥を試験が検出する",
    ),
    Mutant(
        name="COLLECTOR_ALLOWS_OUTPUT_REUSE",
        kind="collector",
        file="tools/collect_backup_restore_area.py",
        old=(
            "    if out.exists() or out.is_symlink():\n"
            '        raise AreaCollectionError(f"EVIDENCE_REUSE: {out}")'
        ),
        new=(
            "    if False:  # mutant: output reuse is no longer refused\n"
            '        raise AreaCollectionError(f"EVIDENCE_REUSE: {out}")'
        ),
        tests=(f"{COLLECTOR_TESTS}::test_output_reuse_is_refused",),
        detects="既存Evidenceの再利用を通す欠陥を試験が検出する",
    ),
    Mutant(
        name="COLLECTOR_ALLOWS_COMMIT_MISMATCH",
        kind="collector",
        file="tools/collect_backup_restore_area.py",
        old='    if manifest.get("implementation_commit_sha") != commit:',
        new="    if False:  # mutant: the manifest commit binding is not checked",
        tests=(
            f"{COLLECTOR_TESTS}::test_manifest_commit_mismatch_is_refused",
            f"{COLLECTOR_TESTS}::test_publishes_new_evidence_and_bound_manifest",
        ),
        detects="束縛不一致のManifestへ証跡を付ける欠陥を試験が検出する",
    ),
)


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603 - fixed executable and argv list
        ["/usr/bin/git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=300
    )
    return done.stdout.strip()


def run_tests(worktree: Path, python: Path, tests: tuple[str, ...], log: Path) -> dict[str, Any]:
    argv = [
        str(python),
        "-m",
        "pytest",
        *tests,
        "-q",
        "-p",
        "no:cacheprovider",
    ]
    started = utc_now()
    done = subprocess.run(  # noqa: S603 - argv list, no shell, explicit env
        argv,
        cwd=worktree,
        capture_output=True,
        timeout=3600,
        check=False,
        env={
            "PATH": os.defpath,
            "HOME": str(worktree),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    payload = done.stdout + done.stderr
    log.write_bytes(payload)
    return {
        "command": argv,
        "cwd": str(worktree),
        "returncode": done.returncode,
        "started_at": started,
        "recorded_at": utc_now(),
        "log_path": str(log),
        "log_sha256": sha256_bytes(payload),
        "tail": payload.decode("utf-8", errors="replace").strip().splitlines()[-3:],
    }


def apply_mutant(worktree: Path, mutant: Mutant) -> bytes:
    path = worktree / mutant.file
    original = path.read_bytes()
    text = original.decode("utf-8")
    if text.count(mutant.old) != 1:
        raise SystemExit(f"MUTATION_TARGET_NOT_UNIQUE: {mutant.name} in {mutant.file}")
    path.write_text(text.replace(mutant.old, mutant.new), encoding="utf-8")
    return original


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--worktree", required=True, type=Path, help="新規の隔離Worktree")
    parser.add_argument("--log-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()

    repo = args.repo_root.resolve(strict=True)
    if args.worktree.exists() or args.out.exists():
        raise SystemExit(f"OUTPUT_REUSE: {args.worktree} / {args.out}")
    args.log_dir.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "--detach", str(args.worktree), args.commit)
    worktree = args.worktree.resolve()
    results: list[dict[str, Any]] = []
    try:
        baseline_tests = tuple(sorted({test for mutant in MUTANTS for test in mutant.tests}))
        baseline = run_tests(worktree, args.python, baseline_tests, args.log_dir / "baseline.log")
        for mutant in MUTANTS:
            original = apply_mutant(worktree, mutant)
            try:
                run = run_tests(
                    worktree, args.python, mutant.tests, args.log_dir / f"{mutant.name}.log"
                )
            finally:
                (worktree / mutant.file).write_bytes(original)
            restored = sha256_bytes((worktree / mutant.file).read_bytes())
            results.append(
                {
                    "mutant": mutant.name,
                    "kind": mutant.kind,
                    "file": mutant.file,
                    "detects": mutant.detects,
                    "tests": list(mutant.tests),
                    "run": run,
                    "detected": run["returncode"] != 0,
                    "restored_sha256": restored,
                    "restore_matches_original": restored == sha256_bytes(original),
                }
            )
        clean = git(worktree, "status", "--short")
    finally:
        git(repo, "worktree", "remove", "--force", str(args.worktree))
    report = {
        "contract": "cc-ops-evidence-01/backup-restore-mutation-check/1",
        "repo": str(repo),
        "commit": args.commit,
        "worktree": str(worktree),
        "worktree_clean_after_restore": clean == "",
        "worktree_status_after_restore": clean.splitlines(),
        "baseline": baseline,
        "baseline_passed": baseline["returncode"] == 0,
        "mutants": results,
        "all_detected": all(item["detected"] for item in results)
        and all(item["restore_matches_original"] for item in results),
        "recorded_at": utc_now(),
    }
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("baseline_passed", "all_detected")}))
    return 0 if report["all_detected"] and report["baseline_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
