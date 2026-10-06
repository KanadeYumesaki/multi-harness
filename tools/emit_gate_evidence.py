#!/usr/bin/env python3
"""Gate Evidence Emitter と Release完全性Guard。

## Gateは参照Caseの結果からしか決まらない

Gate の `status` も引数に無い。**参照するCase Evidenceを全部読み、
全てPASSのときだけPASSになる。** 1件でもFAILか欠落があればFAILである。

「Gateは通ったがCaseは落ちている」という状態を作れなくする。

## Release Evidence の完全性

指示が定める通り、次を満たさないRelease Evidenceを作らない。

| 条件 | 理由 |
|---|---|
| Case 110件未満では生成しない | 部分的な証跡でRuntime GOへ接続しない |
| Gate 45件未満では生成しない | 同上 |
| Runtime GO Manifestへの接続も同じ条件 | 途中経過をGOの根拠にしない |

**66件でも80件でも足りない。** 全件そろうまでRelease Evidenceは存在しない。
これは「まだ足りない」を機械が言い続けるための仕組みである。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from emit_case_evidence import (
    EvidenceEmissionError,
    _canonical,
    verify_case_evidence,
)

__all__ = [
    "GateEvidenceResult",
    "assert_release_evidence_complete",
    "emit_gate_evidence",
]

import hashlib

_GATE_HASH_PREFIX: Final[str] = "FDE-HARNESS/gate-evidence/1/"

#: Release Evidence を生成してよい最小件数。Registryから導出する。
_REQUIRED_SCOPE: Final[str] = "MVP0-A"


@dataclass(frozen=True, slots=True)
class GateEvidenceResult:
    gate_id: str
    status: str
    path: Path


def _gate_hash(body: dict[str, Any]) -> str:
    payload = {k: v for k, v in body.items() if k != "evidence_hash"}
    return (
        "sha256:"
        + hashlib.sha256(_GATE_HASH_PREFIX.encode("utf-8") + _canonical(payload)).hexdigest()
    )


def emit_gate_evidence(
    gate_id: str,
    *,
    case_evidence_paths: list[Path],
    evidence_dir: Path,
    repo_root: Path,
    registries: Path,
) -> GateEvidenceResult:
    """Gate Evidenceを1件生成する。

    `status` は参照Case Evidenceの実測結果からだけ決まる。
    """
    evidence_root = evidence_dir.resolve()
    repo = repo_root.resolve()
    if evidence_root == repo or repo in evidence_root.parents:
        raise EvidenceEmissionError(f"EVIDENCE_IN_REPOSITORY: {evidence_root}")

    if not gate_id or "/" in gate_id or ".." in gate_id:
        raise EvidenceEmissionError(f"INVALID_GATE_ID: {gate_id!r}")

    gates = yaml.safe_load((registries / "gates.yaml").read_text(encoding="utf-8"))
    known = {str(g.get("gate_id")) for g in gates.get("gates", [])}
    if gate_id not in known:
        raise EvidenceEmissionError(f"UNKNOWN_GATE: {gate_id} は Registry に無い")

    target = (evidence_root / "gates" / f"{gate_id}.json").resolve()
    if evidence_root not in target.parents:
        raise EvidenceEmissionError(f"PATH_TRAVERSAL: {target}")
    if target.exists():
        raise EvidenceEmissionError(f"EVIDENCE_REUSE: {target} は既に存在する")

    if not case_evidence_paths:
        raise EvidenceEmissionError(
            f"NO_REFERENCED_CASES: {gate_id} が1件もCaseを参照していない。"
            "参照が無いGateをPASSにしない"
        )

    referenced: list[dict[str, Any]] = []
    for path in sorted(case_evidence_paths):
        if not path.is_file():
            raise EvidenceEmissionError(f"MISSING_CASE_EVIDENCE: {path}")
        # 改変されたCase Evidenceの上にGateを立てない。
        verify_case_evidence(path)
        referenced.append(json.loads(path.read_text(encoding="utf-8")))

    failing = sorted(c["case_id"] for c in referenced if c["status"] != "PASS")
    status = "PASS" if not failing else "FAIL"

    body: dict[str, Any] = {
        "evidence_schema_version": "1.2",
        "gate_id": gate_id,
        "status": status,
        "referenced_case_ids": sorted(c["case_id"] for c in referenced),
        "referenced_test_ids": sorted({c["test_id"] for c in referenced}),
        "failing_case_ids": failing,
        "case_evidence_hashes": sorted(c["evidence_hash"] for c in referenced),
        "registry_expectation_match": not failing,
        "producer": "tools/emit_gate_evidence.py",
    }
    body["evidence_hash"] = _gate_hash(body)

    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".json.tmp")
    temp.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(target)
    return GateEvidenceResult(gate_id=gate_id, status=status, path=target)


def required_gate_cases(snapshot: dict, scope: str, gate_id: str) -> list[list[str]]:
    """Derive the entire in-scope reference set, including every Case per Test."""
    required = snapshot["scopes"][scope]
    if gate_id not in required["required_gate_ids"]:
        raise EvidenceEmissionError("GATE_OUT_OF_SCOPE")
    gate = snapshot["gates"][gate_id]
    if gate["test_refs_mode"] == "ALL_IN_SCOPE":
        return sorted(required["required_cases"])
    if gate["test_refs_mode"] != "EXPLICIT":
        raise EvidenceEmissionError("GATE_REFERENCE_MODE_UNKNOWN")
    refs = [pair for pair in required["required_cases"] if pair[0] in gate["test_refs"]]
    if not refs or {pair[0] for pair in refs} != set(gate["test_refs"]):
        raise EvidenceEmissionError("GATE_REFERENCE_SET_EMPTY_OR_MISSING")
    return sorted(refs)


def emit_formal_gate(
    gate_id: str,
    *,
    manifest: dict,
    snapshot: dict,
    evidence_root: Path,
    out: Path,
) -> dict:
    """Emit only after ALL referenced formal Cases pass the unchanged verifier.

    Legacy emitter and saved evidence stay intact. No caller-supplied reference
    list or PASS argument exists on this path.
    """
    from collect_release_baseline import digest, timestamp
    from emit_unit_area_evidence import _validate_binding, _write_new, verifier

    root = evidence_root.resolve(strict=True)
    if root not in out.resolve().parents:
        raise EvidenceEmissionError("GATE_OUTPUT_OUTSIDE_EVIDENCE_ROOT")
    scope = manifest["release_scope"]
    _validate_binding(snapshot, manifest, scope, root)
    refs = required_gate_cases(snapshot, scope, gate_id)
    rows = manifest["test_cases"]
    findings = verifier.Findings()
    verifier.verify_cases(manifest, snapshot, scope, root, findings, {})
    keys = {"/".join(pair) for pair in refs}
    missing = [
        message
        for message in findings.missing
        if any(
            message.startswith((key + ".", key + ":"))
            or message == "case not PASS: " + key
            or message == "required test case not in manifest: " + key
            for key in keys
        )
    ]
    if findings.errors or missing:
        raise EvidenceEmissionError("GATE_CASES_UNVERIFIED: " + str(findings.errors + missing))
    hashes = []
    for test, case in refs:
        selected = [row for row in rows if row["test_id"] == test and row["case_id"] == case]
        if len(selected) != 1 or selected[0].get("status") != "PASS":
            raise EvidenceEmissionError("GATE_CASES_INCOMPLETE")
        row = selected[0]
        path = verifier.resolve_evidence(root, row["evidence_path"], gate_id, findings)
        if path is None or digest(path) != row["evidence_manifest_hash"]:
            raise EvidenceEmissionError("GATE_CASE_HASH_CHANGED")
        hashes.append({"test_id": test, "case_id": case, "evidence_hash": digest(path)})
    now = timestamp()
    body = {
        "evidence_schema_version": "3.0",
        "release_scope": scope,
        "gate_id": gate_id,
        "status": "PASS",
        "case_refs": refs,
        "summary": {
            "case_evidence": hashes,
            "reference_coverage": "ALL_REQUIRED_IN_SCOPE",
            "durability_limit": "T3 device power-loss durability was not measured",
        },
        **{
            field: manifest[field]
            for field in ("implementation_commit_sha", "schema_set_hash", "migration_head")
        },
        "runtime_environment_hash": manifest["runtime_environment"]["environment_manifest_hash"],
        "producer": "tools/emit_gate_evidence.py:emit_formal_gate",
        "test_run_id": gate_id,
        "started_at": now,
        "recorded_at": now,
    }
    _write_new(out, body)
    return body


def verify_gate_evidence(path: Path) -> None:
    body = json.loads(path.read_text(encoding="utf-8"))
    recorded = body.get("evidence_hash")
    derived = _gate_hash(body)
    if recorded != derived:
        raise EvidenceEmissionError(
            f"EVIDENCE_HASH_MISMATCH: {path} recorded={recorded} derived={derived}"
        )


def _required_counts(registries: Path) -> tuple[int, int]:
    """Release に必要な Case 数と Gate 数を Registry から導出する。

    件数を手入力しない（不変条件#18）。
    """
    tests = yaml.safe_load((registries / "tests.yaml").read_text(encoding="utf-8"))["test_cases"]
    gates = yaml.safe_load((registries / "gates.yaml").read_text(encoding="utf-8"))["gates"]
    case_count = sum(1 for row in tests if _REQUIRED_SCOPE in (row.get("phase_scope") or []))
    return case_count, len(gates)


def assert_release_evidence_complete(evidence_dir: Path, registries: Path) -> None:
    """Release Evidence を生成してよい状態かを判定する。

    **足りないうちは例外で止める。** 部分的な証跡をRuntime GO Manifestへ
    接続させない。
    """
    required_cases, required_gates = _required_counts(registries)
    case_files = list((evidence_dir / "cases").rglob("*.json"))
    gate_files = list((evidence_dir / "gates").glob("*.json"))

    problems: list[str] = []
    if len(case_files) < required_cases:
        problems.append(
            f"INCOMPLETE_CASE_EVIDENCE: {len(case_files)}/{required_cases}。"
            "全件そろうまでRelease Evidenceを生成しない"
        )
    if len(gate_files) < required_gates:
        problems.append(
            f"INCOMPLETE_GATE_EVIDENCE: {len(gate_files)}/{required_gates}。"
            "全件そろうまでRelease Evidenceを生成しない"
        )
    if problems:
        raise EvidenceEmissionError("; ".join(problems))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--check-release-complete", type=Path)
    parser.add_argument("--registries", type=Path, default=Path("design-source/registries"))
    args = parser.parse_args(argv)

    try:
        if args.verify is not None:
            verify_gate_evidence(args.verify)
            print(f"gate evidence hash ok: {args.verify}")
            return 0
        if args.check_release_complete is not None:
            assert_release_evidence_complete(args.check_release_complete, args.registries)
            print("release evidence is complete")
            return 0
    except EvidenceEmissionError as exc:
        print(str(exc), file=sys.stderr)
        return 3

    print(
        "このToolはRunnerから import して使う。StatusをCLIから指定する経路は無い。",
        file=sys.stderr,
    )
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
