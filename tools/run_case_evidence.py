#!/usr/bin/env python3
"""Native Caseを実行し、新規Evidenceと実行記録を採取する。Releaseは発行しない。"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from pathlib import Path

from case_runner import build_pytest_adapters, run_scope
from emit_case_evidence import EvidenceEmissionError, _require_clean_tree


def collect(
    repo: Path,
    registries: Path,
    scope: str,
    environment: Path,
    out: Path,
    *,
    case_ids: tuple[str, ...] | None = None,
) -> dict:
    repo = repo.resolve(strict=True)
    out = out.resolve()
    if out == repo or repo in out.parents or str(out).startswith("/mnt/"):
        raise EvidenceEmissionError("UNSAFE_EVIDENCE_DESTINATION")
    if registries.resolve() != repo / "design-source/registries":
        raise EvidenceEmissionError("REGISTRIES_OUTSIDE_MEASURED_REPOSITORY")
    if os.stat(out.parent).st_dev != os.stat(repo).st_dev:
        raise EvidenceEmissionError("EVIDENCE_ON_DIFFERENT_FILESYSTEM")
    commit = _require_clean_tree(repo)
    env = json.loads(environment.read_text(encoding="utf-8"))
    if (
        env.get("implementation_commit_sha") != commit
        or env.get("is_wsl2") is not True
        or env.get("workspace_on_linux_native_fs") is not True
        or env.get("workspace_root") != str(repo)
    ):
        raise EvidenceEmissionError("ENVIRONMENT_BINDING_MISMATCH")
    # 出力全体を新規予約。既存の記録・Fixture・Caseは上書きしない。
    out.mkdir(parents=True, exist_ok=False)
    adapters = build_pytest_adapters(
        registries=registries, scope=scope, repo_root=repo, fixtures_dir=out / "fixtures"
    )
    adapters = {
        key: replace(adapter, execution_records_dir=out / "executions")
        for key, adapter in adapters.items()
    }
    result = run_scope(
        registries=registries,
        scope=scope,
        adapters=adapters,
        evidence_dir=out,
        repo_root=repo,
        environment_manifest=environment,
        case_ids=case_ids,
    )
    statuses = {}
    for case, path in result.emitted:
        statuses[case] = json.loads(path.read_text())["status"]
    summary = {
        "contract": "case-collection/1",
        "implementation_commit_sha": commit,
        "scope": scope,
        "case_statuses": statuses,
        "emitted": [[case, str(path.relative_to(out))] for case, path in result.emitted],
        "skipped_unwired": list(result.skipped_unwired),
        "failures": list(result.failures),
        "release_manifest_generated": False,
    }
    (out / "collection.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--registries", type=Path, required=True)
    parser.add_argument("--release-scope", required=True)
    parser.add_argument("--environment-manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = collect(
        args.repo_root, args.registries, args.release_scope, args.environment_manifest, args.out
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(
        bool(
            result["failures"]
            or result["skipped_unwired"]
            or any(status != "PASS" for status in result["case_statuses"].values())
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
