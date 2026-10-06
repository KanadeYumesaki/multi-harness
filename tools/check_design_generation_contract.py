#!/usr/bin/env python3
"""Canonical design and generated-artifact consistency check.

The v1.6->v1.7 migrator is a bootstrap utility. This check prevents a stale
migration output from silently replacing the reviewed v1.7 execution source.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


REQUIRED_ANCHORS = (
    "Verifier v1.2是正日",
    "tools/check_ci_runtime_boundary.py",
    "tools/check_blocked_records.py",
    "### 26.5.1 BLOCKEDからの復帰",
    "UNTRUSTED_REVIEW_ONLY",
)


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--design", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--spec-manifest", required=True, type=Path)
    parser.add_argument("--readme", type=Path, default=Path("README.md"))
    args = parser.parse_args()
    errors: list[str] = []
    try:
        design_text = args.design.read_text(encoding="utf-8")
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        spec_manifest = json.loads(args.spec_manifest.read_text(encoding="utf-8"))
        readme_text = args.readme.read_text(encoding="utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"input invalid: {exc}", file=sys.stderr)
        return 4

    for anchor in REQUIRED_ANCHORS:
        if anchor not in design_text:
            errors.append(f"design anchor missing: {anchor}")
    design_hash = digest(args.design)
    if snapshot.get("design_sha256") != design_hash:
        errors.append("registry snapshot design_sha256 mismatch")
    if spec_manifest.get("source_hash") != design_hash:
        errors.append("spec manifest source_hash mismatch")
    readme_design = re.search(r"設計書\s+: (sha256:[0-9a-f]{64})", readme_text)
    readme_snapshot = re.search(r"Registry Snapshot: (sha256:[0-9a-f]{64})", readme_text)
    if not readme_design or readme_design.group(1) != design_hash:
        errors.append("README design hash is stale")
    if not readme_snapshot or readme_snapshot.group(1) != snapshot.get("registry_snapshot_hash"):
        errors.append("README registry snapshot hash is stale")
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 3
    print(f"design generation contract ok: {design_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
