#!/usr/bin/env python3
"""`expectation_descriptor_hash`を期待値内容から決定的に導出する（設計書 §19.1）。

v1.7までの値は「本書統合時に再生成した」とだけ記され、導出式が無かった。
値が導出できないと、レビューアはRegistryが改変されていないことを確認できず、
Verifierが突合する`expectation_descriptor_hash`は単なる不透明Tokenになる。

v1.8では次の式で全Caseから導出する。

    expectation_descriptor_hash = SHA-256(
        "FDE-HARNESS/test-expectation/1/" || CanonicalJSON(ExpectationDescriptor))

Domain Separation Prefixは設計書§19.1が既に宣言している値をそのまま用いる。

ExpectationDescriptorへ含めるのは**期待値そのもの**だけとする。

含める : test_id, case_id, scenario, expected_event_sequence, expected_subject_type,
         expected_subject_id, expected_state, expected_error_code, trace_scope,
         assertions, auto_reexecution_prohibited, release_allowed,
         manual_queue_expected, fault_point, durability_tier
含めない: input_fixture_hash（実Fixture作成後に定まる）
         evidence_status / evidence_manifest_hash（実行結果であり期待値ではない）
         phase_scope（Release Scopeへの割当であり期待値ではない）

Canonical JSONは`tools/build_registry_snapshot.py`と同一の規約
（`sort_keys=True`、区切り最小、`ensure_ascii=False`）を用いる。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Final

import yaml

DOMAIN_PREFIX: Final[str] = "FDE-HARNESS/test-expectation/1/"

DESCRIPTOR_FIELDS: Final[tuple[str, ...]] = (
    "assertions",
    "auto_reexecution_prohibited",
    "case_id",
    "durability_tier",
    "expected_error_code",
    "expected_event_sequence",
    "expected_state",
    "expected_subject_id",
    "expected_subject_type",
    # Event観測Policyは期待値の一部である。Hashへ含めないと、Policyだけを
    # 後から差し替えても期待値が変わっていないように見える（設計書§19.1.1）。
    "event_observation_policy",
    "fault_point",
    "manual_queue_expected",
    "release_allowed",
    "scenario",
    "test_id",
    "trace_scope",
)


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def descriptor(case: dict[str, Any]) -> dict[str, Any]:
    return {field: case.get(field) for field in DESCRIPTOR_FIELDS}


def expectation_hash(case: dict[str, Any]) -> str:
    payload = DOMAIN_PREFIX.encode("utf-8") + canonical(descriptor(case))
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registries", type=Path, default=Path("design-source/registries"))
    parser.add_argument(
        "--check",
        action="store_true",
        help="書き換えず、記録値と導出値の一致だけを検査する",
    )
    args = parser.parse_args(argv)

    path = args.registries / "tests.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases: list[dict[str, Any]] = document["test_cases"]

    mismatched: list[str] = []
    for case in cases:
        expected = expectation_hash(case)
        key = f"{case['test_id']}/{case['case_id']}"
        if case.get("expectation_descriptor_hash") != expected:
            mismatched.append(key)
            if not args.check:
                case["expectation_descriptor_hash"] = expected

    if args.check:
        if mismatched:
            print(
                f"expectation_descriptor_hash mismatch in {len(mismatched)} case(s):",
                file=sys.stderr,
            )
            for key in mismatched[:10]:
                print(f"  {key}", file=sys.stderr)
            print("run: python tools/build_expectation_hashes.py", file=sys.stderr)
            return 1
        print(f"{path}: all {len(cases)} expectation hashes are derived and current")
        return 0

    if mismatched:
        path.write_text(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=10**6),
            encoding="utf-8",
        )
    print(f"{path}: {len(cases)} cases, {len(mismatched)} hash(es) updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
