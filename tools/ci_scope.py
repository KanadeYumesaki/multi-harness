#!/usr/bin/env python3
"""変更範囲から CI の実行層を決める。**推測で検査を省略しない。**

## 3 層

* `draft` — Draft PR。policy gate、禁止パターン、変更範囲の最小試験、Python 1 版
* `ready` — Ready for Review の PR。draft の全部に spec／generation／reference、
  Integration、安全境界を足す
* `full` — `ci:full` ラベル／`workflow_dispatch`／main push／Release tag／
  Release 候補。Python 全版、全 pytest、license／SBOM、deep
* `deep` — schedule。deep だけを回す

## Fail-Closed

分類できない Path が 1 つでもあれば `full` へ回す。「たぶん関係ない」で省かない。
変更が 1 件も読めないときも `full` にする。**読めていないことを「影響なし」と
書かない。**

## Job を skip しない

出力は「その Job が何をするか」であって「Job を走らせるか」ではない。対象外の
Job は短い not-applicable として成功で終える。Job ごと skip すると required check
が消える。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

#: 安全に直結する Path。ここが動いたら手を抜かない。
SAFETY_PREFIXES = (
    "src/",
    "schemas/",
    "spec/",
    "design-source/",
    "tools/",
    "tests/",
    ".github/",
)

#: 依存の変更。license／SBOM／脆弱性走査を回す。
DEPENDENCY_PATHS = (
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "constraints.txt",
)

#: 正本の変更。spec lint・generation contract・reference currency を回す。
CANON_PREFIXES = ("design-source/", "schemas/", "spec/", "ci/")
CANON_FILES = ("registry-snapshot.json", "runtime-go-manifest.MVP0-A.template.json")
CANON_SUFFIXES = ("-runtime-go.md",)

#: 検査の要らない Path。**ここに入れてよいのは「動いても何も壊れない」ものだけ。**
DOCS_PREFIXES = ("docs/", "runtime-evidence/", "blocked/", "release/", "tasks/")
DOCS_FILES = ("README.md", "AGENTS.md", "CLAUDE.md", "THREAD-START.md")

FULL_PYTHONS = ["3.11", "3.12"]
LIGHT_PYTHONS = ["3.12"]

#: Release 候補とみなす Branch の接頭辞。
RELEASE_BRANCH_PREFIXES = ("release/", "hotfix/")

#: 完全検査を強制するラベル。
FULL_LABEL = "ci:full"


def classify(path: str) -> str:
    """1 つの Path を分類する。どれにも当たらなければ `unknown`。"""
    if path in DEPENDENCY_PATHS:
        return "dependency"
    if path in CANON_FILES or path.endswith(CANON_SUFFIXES):
        return "canon"
    if path.startswith(CANON_PREFIXES):
        return "canon"
    if path.startswith(SAFETY_PREFIXES):
        return "safety"
    if path in DOCS_FILES or path.startswith(DOCS_PREFIXES):
        return "docs"
    return "unknown"


def decide(
    *,
    changed: list[str],
    event: str,
    is_draft: bool,
    labels: list[str],
    ref: str,
    head_ref: str,
    diff_ok: bool,
) -> dict[str, Any]:
    """層と、各 Job が何をするかを決める。"""
    reasons: list[str] = []
    buckets = {path: classify(path) for path in changed}
    kinds = set(buckets.values())

    forced_full = False
    if event == "workflow_dispatch":
        forced_full, reason = True, "workflow_dispatch は完全検査である"
    elif event == "push" and ref == "refs/heads/main":
        forced_full, reason = True, "main への push は完全検査である"
    elif ref.startswith("refs/tags/"):
        forced_full, reason = True, "Release tag は完全検査である"
    elif FULL_LABEL in labels:
        forced_full, reason = True, f"`{FULL_LABEL}` ラベルが付いている"
    elif head_ref.startswith(RELEASE_BRANCH_PREFIXES):
        forced_full, reason = True, "Release 候補 Branch である"
    else:
        reason = ""
    if forced_full:
        reasons.append(reason)

    # --- Fail-Closed。読めない・分類できないものは完全検査へ回す -------------
    if not diff_ok:
        forced_full = True
        reasons.append("変更一覧を読めなかった。影響範囲が不明なので完全検査へ回す")
    elif not changed:
        forced_full = True
        reasons.append("変更が 1 件も読めなかった。影響なしと決めつけない")
    if "unknown" in kinds:
        forced_full = True
        unknown = sorted(p for p, kind in buckets.items() if kind == "unknown")
        reasons.append(f"分類できない Path がある: {unknown[:5]}")

    if forced_full:
        tier = "full"
    elif event == "schedule":
        tier = "deep"
        reasons.append("schedule は deep だけを回す。main は前回から動いていない")
    elif event == "pull_request" and is_draft:
        tier = "draft"
        reasons.append("Draft PR は最小検査で回す")
    else:
        tier = "ready"
        reasons.append("Ready for Review の PR は spec・reference・Integration まで回す")

    canon_touched = "canon" in kinds
    safety_touched = "safety" in kinds
    deps_touched = "dependency" in kinds

    if tier == "full":
        plan = {
            "pythons": FULL_PYTHONS,
            "run_spec": True,
            "run_reference": True,
            "run_supply": True,
            "run_integration": True,
            "run_deep": True,
            "run_full_tests": True,
        }
    elif tier == "deep":
        plan = {
            "pythons": LIGHT_PYTHONS,
            "run_spec": False,
            "run_reference": False,
            "run_supply": False,
            "run_integration": False,
            "run_deep": True,
            "run_full_tests": False,
        }
    elif tier == "ready":
        plan = {
            "pythons": LIGHT_PYTHONS,
            "run_spec": True,
            "run_reference": True,
            "run_supply": deps_touched,
            "run_integration": True,
            "run_deep": False,
            "run_full_tests": True,
        }
    else:  # draft
        plan = {
            "pythons": LIGHT_PYTHONS,
            # 正本が動いたら Draft でも spec を回す。**正本の壊れは後回しにしない。**
            "run_spec": canon_touched,
            "run_reference": canon_touched,
            "run_supply": deps_touched,
            "run_integration": False,
            "run_deep": False,
            "run_full_tests": False,
        }

    return {
        "tier": tier,
        "reasons": reasons,
        "changed_count": len(changed),
        "buckets": {kind: sorted(p for p, k in buckets.items() if k == kind) for kind in kinds},
        "safety_paths": safety_touched,
        "canon_paths": canon_touched,
        "dependency_paths": deps_touched,
        **plan,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed-file", type=Path, help="変更 Path を 1 行 1 件で書いた File")
    parser.add_argument("--event", required=True)
    parser.add_argument("--draft", default="false")
    parser.add_argument("--labels", default="")
    parser.add_argument("--ref", default="")
    parser.add_argument("--head-ref", default="")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)

    diff_ok = args.changed_file is not None and args.changed_file.is_file()
    changed: list[str] = []
    if diff_ok:
        changed = [
            line.strip()
            for line in args.changed_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    result = decide(
        changed=changed,
        event=args.event,
        is_draft=str(args.draft).lower() == "true",
        labels=[x.strip() for x in args.labels.split(",") if x.strip()],
        ref=args.ref,
        head_ref=args.head_ref,
        diff_ok=diff_ok,
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            for key in (
                "tier",
                "run_spec",
                "run_reference",
                "run_supply",
                "run_integration",
                "run_deep",
                "run_full_tests",
            ):
                value = result[key]
                handle.write(f"{key}={str(value).lower()}\n")
            handle.write(f"pythons={json.dumps(result['pythons'])}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
