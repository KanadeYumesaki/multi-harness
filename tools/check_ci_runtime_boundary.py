#!/usr/bin/env python3
"""CIがRuntime GOを誤って宣言・生成していないことを検査する。

CIはSpec／Static／Verifier自己試験の証明だけを担い、WSL2 Runtime Evidenceや
Release Manifestを生成してはならない。Release Gateはこのチェックを呼び出さない。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


FORBIDDEN_RELEASE_GLOB = "runtime-go-release-manifest*.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path("."))
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI実行として検査する（GITHUB_ACTIONSまたはCIも確認）",
    )
    args = parser.parse_args()
    root = args.workspace.resolve()
    if not root.is_dir():
        print(f"workspace is not a directory: {root}", file=sys.stderr)
        return 4
    if args.ci and not (
        os.environ.get("GITHUB_ACTIONS", "").lower() == "true"
        or os.environ.get("CI", "").lower() == "true"
    ):
        print("CI boundary check must run under CI=true or GITHUB_ACTIONS=true", file=sys.stderr)
        return 4

    found = []
    for candidate in root.rglob(FORBIDDEN_RELEASE_GLOB):
        if ".git" not in candidate.parts:
            found.append(candidate)
    if found:
        for path in sorted(found):
            print(f"CI_RUNTIME_ARTIFACT_FORBIDDEN {path}", file=sys.stderr)
        return 3

    print("CI_RUNTIME_BOUNDARY_OK static_only=true runtime_evidence=false release_manifest=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
