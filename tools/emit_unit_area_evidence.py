#!/usr/bin/env python3
"""`unit_case_suite` 領域 Evidence を Unit Case Evidence から導出する。

## 件数もPASS状態も手入力しない

対象 Case は Registry Snapshot の `event_observation_policy == NOT_APPLICABLE`
から引く。状態は対象 Case Evidence を**読み直して**決める。
先にPASS件数を書かず、作った結果を数える（不変条件#18）。

## 1件でもPASSでなければ領域はPASSにしない

領域Evidenceは「その領域の検証が通った」という主張である。根拠の Case が
落ちているのに領域だけPASSにすると、Gate が空の根拠の上に立つ。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import verify_runtime_go as verifier

from build_registry_snapshot import domain_hash

AREA = "unit_case_suite"
POLICY_NOT_APPLICABLE = "NOT_APPLICABLE"


class AreaEvidenceError(RuntimeError):
    """領域Evidenceを作れない。**部分的な生成物を残さない。**"""


def _unit_case_ids(snapshot: dict[str, Any], scope: str) -> list[str]:
    """Snapshot が `NOT_APPLICABLE` と宣言した Case を引く。

    **Registry を直接読まない。** Release 判定と同じ入力から引く。
    """
    required = {f"{t}/{c}" for t, c in snapshot["scopes"][scope]["required_cases"]}
    return sorted(
        key
        for key, exp in snapshot["expectations"].items()
        if key in required and exp.get("event_observation_policy") == POLICY_NOT_APPLICABLE
    )


def _load_case_evidence(
    evidence_root: Path, case_id: str, manifest: dict[str, Any]
) -> tuple[Path, dict[str, Any]]:
    entries = [
        row
        for row in manifest.get("test_cases", [])
        if f"{row.get('test_id')}/{row.get('case_id')}" == case_id
    ]
    if len(entries) != 1:
        raise AreaEvidenceError(f"UNIT_CASE_MANIFEST_ENTRY_MISSING_OR_DUPLICATE: {case_id}")
    findings = verifier.Findings()
    row = entries[0]
    doc = verifier.check_evidence_file(
        evidence_root, row, case_id, row.get("evidence_manifest_hash"), findings
    )
    if not findings.ok or doc is None:
        raise AreaEvidenceError(f"INVALID_UNIT_CASE_EVIDENCE: {findings.errors + findings.missing}")
    path = verifier.resolve_evidence(evidence_root, row["evidence_path"], case_id, findings)
    if path is None or not findings.ok:
        raise AreaEvidenceError(f"INVALID_UNIT_CASE_PATH: {case_id}")
    return path, doc


def _validate_binding(snapshot: dict, manifest: dict, scope: str, root: Path) -> None:
    expected_hash = domain_hash(
        "FDE-HARNESS/registry-snapshot/1/",
        {
            key: value
            for key, value in snapshot.items()
            if key not in ("registry_snapshot_hash", "generated_at")
        },
    )
    if snapshot.get("registry_snapshot_hash") != expected_hash:
        raise AreaEvidenceError("REGISTRY_SNAPSHOT_HASH_MISMATCH")
    for field in ("design_sha256", "registry_snapshot_hash"):
        if not verifier.is_hash(snapshot.get(field)) or manifest.get(field) != snapshot[field]:
            raise AreaEvidenceError(f"MANIFEST_BINDING_MISMATCH: {field}")
    if manifest.get("release_scope") != scope or scope not in snapshot["scopes"]:
        raise AreaEvidenceError("RELEASE_SCOPE_MISMATCH")
    if (
        not isinstance(manifest.get("implementation_commit_sha"), str)
        or not verifier.GIT_SHA_RE.fullmatch(manifest["implementation_commit_sha"])
        or manifest.get("source_tree_clean") is not True
        or not verifier.is_hash(manifest.get("schema_set_hash"))
        or not manifest.get("migration_head")
    ):
        raise AreaEvidenceError("MANIFEST_SOURCE_BINDING_MISSING")
    findings = verifier.Findings()
    verifier.verify_environment(manifest, root, findings)
    if not findings.ok:
        raise AreaEvidenceError(
            f"ENVIRONMENT_BINDING_INVALID: {findings.errors + findings.missing}"
        )


def _write_new(out: Path, body: dict) -> None:
    """Validate first; publish atomically without replacing a saved report."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=out.parent, prefix=".unit-area-", delete=False
    ) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(body, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink()
            raise
    try:
        os.link(temporary, out)  # atomic, EEXIST instead of overwriting evidence
    finally:
        temporary.unlink()
    fd = os.open(out.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def build(
    *,
    evidence_root: Path,
    snapshot_path: Path,
    manifest_path: Path,
    scope: str,
    out: Path,
) -> dict[str, Any]:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _validate_binding(snapshot, manifest, scope, evidence_root)
    case_ids = _unit_case_ids(snapshot, scope)
    if not case_ids:
        raise AreaEvidenceError(f"NO_UNIT_CASES: {scope} に Unit Case が1件も無い")

    # Use the unchanged formal Case verifier. Missing unrelated Cases do not
    # prevent this area, but any malformed supplied Case remains an error.
    findings = verifier.Findings()
    verifier.verify_cases(manifest, snapshot, scope, evidence_root, findings, {})
    unit_missing = [
        message for message in findings.missing if any(case_id in message for case_id in case_ids)
    ]
    if findings.errors or unit_missing:
        raise AreaEvidenceError(f"UNIT_CASE_CONTRACT_INVALID: {findings.errors + unit_missing}")

    hashes: list[str] = []
    failing: list[str] = []
    versions: set[str] = set()
    for case_id in case_ids:
        path, body = _load_case_evidence(evidence_root, case_id, manifest)
        if body.get("evidence_kind") != "UNIT":
            raise AreaEvidenceError(
                f"NOT_A_UNIT_EVIDENCE: {case_id} evidence_kind={body.get('evidence_kind')!r}"
            )
        if body.get("event_observation") != POLICY_NOT_APPLICABLE:
            raise AreaEvidenceError(
                f"UNEXPECTED_EVENT_OBSERVATION: {case_id} "
                f"event_observation={body.get('event_observation')!r}"
            )
        if body.get("observed_event_sequence") is not None:
            raise AreaEvidenceError(f"EVENT_SEQUENCE_NOT_NULL: {case_id}")
        effects = body.get("side_effects")
        fields = (
            "network_calls",
            "process_launches",
            "workspace_commits",
            "external_effects",
            "ledger_effect_attempts",
        )
        if not isinstance(effects, dict) or any(
            type(effects.get(field)) is not int or effects[field] < 0 for field in fields
        ):
            # 未測定を成功として数えない。
            raise AreaEvidenceError(f"SIDE_EFFECTS_NOT_OBSERVED: {case_id}")
        if body.get("status") != "PASS":
            failing.append(case_id)
        # Formal Manifest binds the exact file bytes, not the legacy nested
        # emitter's canonical body self-hash. check_evidence_file verified them.
        hashes.append(verifier.sha256_file(path))
        versions.add(str(body.get("evidence_schema_version")))

    if len(versions) != 1:
        raise AreaEvidenceError(f"MIXED_EVIDENCE_SCHEMA_VERSIONS: {sorted(versions)}")

    now = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    body = {
        "evidence_schema_version": versions.pop(),
        "release_scope": scope,
        "area": AREA,
        # 根拠が落ちていれば領域も落とす。
        "status": "PASS" if not failing else "FAIL",
        "summary": {
            "case_ids": case_ids,
            "case_evidence_hashes": sorted(hashes),
            "unit_case_count": len(case_ids),
            "failing_case_ids": sorted(failing),
            "design_sha256": snapshot["design_sha256"],
            "registry_snapshot_hash": snapshot["registry_snapshot_hash"],
            "human_measured": False,
        },
        "producer": "tools/emit_unit_area_evidence.py",
        "test_run_id": f"unit-case-suite-{scope}",
        "started_at": now,
        "recorded_at": now,
        "implementation_commit_sha": manifest.get("implementation_commit_sha"),
        "schema_set_hash": manifest.get("schema_set_hash"),
        "migration_head": manifest.get("migration_head"),
        "runtime_environment_hash": (manifest.get("runtime_environment") or {}).get(
            "environment_manifest_hash"
        ),
    }

    _write_new(out, body)
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--release-scope", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        body = build(
            evidence_root=args.evidence_root,
            snapshot_path=args.snapshot,
            manifest_path=args.manifest,
            scope=args.release_scope,
            out=args.out,
        )
    except AreaEvidenceError as exc:
        print(f"area evidence not emitted: {exc}", file=sys.stderr)
        return 1

    print(f"wrote {args.out}")
    print(
        f"  area={body['area']} status={body['status']} cases={body['summary']['unit_case_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
