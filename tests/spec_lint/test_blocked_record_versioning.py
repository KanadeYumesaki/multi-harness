"""BLOCKED Record Schema の Version管理（Owner Decision C-Q4 / Step 3-b）。

`record_version 1.0` はEvidence参照がPath文字列だけであり、**参照先Fileの中身の
差し替えを検出できない**。`1.1` は各EvidenceのSHA-256と、そのHashをいつどういう
根拠で得たか（`hash_provenance`）を持つ。

旧Recordを読めなくしないため`1.0`の受理は維持する。同時に、Hashの裏付けが無い
Recordを監査Evidenceへ昇格させない。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
RECORDS = REPO_ROOT / "blocked" / "records"
SCHEMA = REPO_ROOT / "blocked-record.schema.json"
DESIGN = REPO_ROOT / "design-v1.25-runtime-go.md"
SNAPSHOT = REPO_ROOT / "registry-snapshot.json"


def _validator() -> Draft202012Validator:
    document = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(document)
    return Draft202012Validator(document)


def _records() -> list[tuple[str, dict[str, Any]]]:
    return [
        (path.name, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(RECORDS.glob("BLK-*.json"))
    ]


def _run_validator(
    record_path: Path, *extra: str, root: Path = REPO_ROOT
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - 固定argv、shell不使用
        [
            sys.executable,
            "tools/validate_blocked_record.py",
            str(record_path),
            "--design",
            str(DESIGN),
            "--registry",
            str(SNAPSHOT),
            "--root",
            str(root),
            *extra,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


# --------------------------------------------------------------------------
# Schemaそのもの
# --------------------------------------------------------------------------


def test_schema_accepts_both_record_versions() -> None:
    """1.0を読めなくしない。Version管理の目的はそこにある。"""
    document = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert document["properties"]["record_version"]["enum"] == ["1.0", "1.1"]


def test_every_committed_record_satisfies_the_schema() -> None:
    validator = _validator()
    problems: list[str] = []
    for name, record in _records():
        for error in validator.iter_errors(record):
            problems.append(
                f"{name}: {'/'.join(str(p) for p in error.absolute_path)}: {error.message}"
            )
    assert not problems, "\n".join(problems)


def test_a_synthetic_corpus_satisfies_the_schema(tmp_path: Path) -> None:
    """合成の一覧（正規の検査器が受理したもの）が Schema を満たすこと。

    保存 Record は公開用の配布コピーに無く、上の試験は一覧が空だと何も見ない。
    Schema 自体が記録の形を受け付けることを、空でない一覧で確かめる。
    """
    from synthetic_blocked_records import build_checked_corpus

    written = build_checked_corpus(tmp_path / "records", DESIGN, SNAPSHOT)
    assert written
    validator = _validator()
    problems = [
        f"{name}: {error.message}"
        for name, record in written.items()
        for error in validator.iter_errors(record)
    ]
    assert not problems, "\n".join(problems)


def test_legacy_string_evidence_is_rejected_under_1_1() -> None:
    """1.1を名乗るなら文字列配列は通さない。移行漏れを静かに許さない。"""
    validator = _validator()
    record = {
        "record_version": "1.1",
        "blocker_id": "BLK-20260816-X",
        "task_id": "X",
        "release_scope": "MVP0-A",
        "category": "SPEC_CLARIFICATION",
        "status": "OPEN",
        "observed": {
            "summary": "s",
            "command": ["c"],
            "exit_code": 1,
            "evidence_paths": ["docs/decision/LLM-FIRST-CONTEXT-DECISIONS.md"],
        },
        "next_action": {"action": "a", "acceptance": ["b"], "required_decision": None},
        "owner": "synthetic-owner",
        "created_at": "2026-08-16T00:00:00Z",
        "design_sha256": "sha256:" + "0" * 64,
        "registry_snapshot_hash": "sha256:" + "0" * 64,
    }
    assert list(validator.iter_errors(record)), "1.1でPath文字列配列が通ってしまう"


def test_unknown_provenance_must_not_carry_a_hash() -> None:
    """「後から測った値」をUNKNOWNへ書けないようにする。"""
    validator = _validator()
    entry = {
        "path": "x",
        "sha256": "sha256:" + "a" * 64,
        "hash_provenance": "UNKNOWN",
        "hash_observed_at": None,
    }
    errors = list(
        validator.evolve(
            schema=json.loads(SCHEMA.read_text(encoding="utf-8"))["$defs"]["evidence_reference"]
        ).iter_errors(entry)
    )
    assert errors, "UNKNOWNなのにsha256を持つEntryが通ってしまう"


# --------------------------------------------------------------------------
# 移行済みRecord
# --------------------------------------------------------------------------


def test_hash_provenance_is_consistent_within_a_record() -> None:
    """1 Record内でHashの由来を混ぜないこと。

    作成時に実測したものと、後から測り直したものが同じRecordに混在すると、
    どのEvidenceが「当時のものと同一」なのか区別できなくなる。

    以前この試験は「`supersedes` を持たない＝移行Record」という**代理**で
    母集団を選んでいた。新規発行の非後継Recordが現れるまでは一致していたが、
    両者は別の性質である。判定を性質そのものへ移した。
    """
    for name, record in _records():
        entries = record["observed"]["evidence_paths"]
        if not entries:
            continue
        provenances = {entry["hash_provenance"] for entry in entries}
        assert len(provenances) == 1, f"{name}: Hashの由来が混在している {provenances}"

        provenance = provenances.pop()
        for entry in entries:
            if provenance == "UNKNOWN":
                # 作成時のHashが残っていない。いま測った値を
                # `MEASURED_AT_CREATION` と書けば、証明できない主張になる。
                assert entry["sha256"] is None, f"{name}: {entry['path']}"
                assert entry["hash_observed_at"] is None, f"{name}: {entry['path']}"
            else:
                assert entry["sha256"], f"{name}: {entry['path']}"
                assert entry["hash_observed_at"], f"{name}: {entry['path']}"


def test_open_record_errors_when_its_evidence_changes(tmp_path: Path) -> None:
    """OPEN Recordの根拠Fileが変わったらErrorにすること。

    いま塞がっている事項の根拠が動いたということであり、読み直さずに進めない。
    """
    record_path = _make_record(tmp_path, status="OPEN")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    evidence = tmp_path / record["observed"]["evidence_paths"][0]["path"]
    evidence.write_text("CHANGED\n", encoding="utf-8")

    result = _run_validator(record_path, root=tmp_path)
    assert result.returncode != 0, result.stdout
    assert "evidence content changed" in result.stdout + result.stderr


def test_closed_record_only_notes_when_its_evidence_changes(tmp_path: Path) -> None:
    """閉じたRecordの根拠Fileが変わってもErrorにしないこと。

    閉じたRecordは**その時点の観測の写し**であり、後からRegistryやCoverage台帳が
    動くのは正常な進行である。ここをErrorにすると、Block Recordが一度参照した
    Fileを二度と変更できなくなる。

    ただし監査Evidenceへは昇格しない。中身が変わった以上、
    「当時のものと同一」とは言えないことは OPEN と同じである。
    """
    record_path = _make_record(
        tmp_path,
        status="RESOLVED",
        resolved_at="2026-08-18T00:00:00Z",
        resolution={
            "outcome": "SUPERSEDED_BY_DESIGN_HASH",
            "decided_at": "2026-08-18T00:00:00Z",
            "decided_by": "synthetic-owner",
            "summary": "s",
            "successor_blocker_id": "BLK-20260818-NEXT",
        },
    )
    record = json.loads(record_path.read_text(encoding="utf-8"))
    evidence = tmp_path / record["observed"]["evidence_paths"][0]["path"]
    evidence.write_text("CHANGED\n", encoding="utf-8")

    result = _run_validator(record_path, root=tmp_path)
    combined = result.stdout + result.stderr
    assert "note:" in combined, combined
    assert "audit_evidence=NO" in combined, combined


def test_records_with_unknown_hashes_are_not_audit_evidence() -> None:
    """UNKNOWNを含むRecordは監査Evidenceへ昇格しない。"""
    for path in sorted(RECORDS.glob("BLK-*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        provenances = {entry["hash_provenance"] for entry in record["observed"]["evidence_paths"]}
        if "UNKNOWN" not in provenances:
            continue
        baseline = "CURRENT" if record["design_sha256"] == _design_hash() else "SUPERSEDED"
        result = _run_validator(path, "--baseline", baseline)
        assert "audit_evidence=NO" in result.stdout, result.stdout


def _design_hash() -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(DESIGN.read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# Validatorの振る舞い
# --------------------------------------------------------------------------


def _make_record(tmp_path: Path, **overrides: Any) -> Path:
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("measured at creation\n", encoding="utf-8")
    import hashlib

    record: dict[str, Any] = {
        "record_version": "1.1",
        "blocker_id": "BLK-20260816-FIXTURE",
        "task_id": "FIXTURE",
        "release_scope": "MVP0-A",
        "category": "SPEC_CLARIFICATION",
        "status": "OPEN",
        "observed": {
            "summary": "fixture",
            "command": ["true"],
            "exit_code": 1,
            "evidence_paths": [
                {
                    "path": "evidence.txt",
                    "sha256": "sha256:" + hashlib.sha256(evidence.read_bytes()).hexdigest(),
                    "hash_provenance": "MEASURED_AT_CREATION",
                    "hash_observed_at": "2026-08-16T00:00:00Z",
                }
            ],
        },
        "next_action": {"action": "a", "acceptance": ["b"], "required_decision": None},
        "owner": "synthetic-owner",
        "created_at": "2026-08-16T00:00:00Z",
        "resolved_at": None,
        "design_sha256": _design_hash(),
        "registry_snapshot_hash": snapshot["registry_snapshot_hash"],
        "supersedes": None,
        "resolution": None,
    }
    record.update(overrides)
    path = tmp_path / "BLK-20260816-FIXTURE.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def test_measured_evidence_is_promotable(tmp_path: Path) -> None:
    path = _make_record(tmp_path)
    result = _run_validator(path, root=tmp_path)
    assert result.returncode == 0, result.stdout
    assert "audit_evidence=YES" in result.stdout


def test_changed_evidence_content_is_detected(tmp_path: Path) -> None:
    """Hashを持つ意味はここにある。Path一覧のAppend-onlyでは防げない差し替えを検出する。"""
    path = _make_record(tmp_path)
    (tmp_path / "evidence.txt").write_text("swapped\n", encoding="utf-8")
    result = _run_validator(path, root=tmp_path)
    assert result.returncode == 1
    assert "evidence content changed" in result.stdout


def test_superseded_baseline_is_accepted_only_when_declared(tmp_path: Path) -> None:
    """旧正本へ束縛されたRecordは`--baseline SUPERSEDED`でだけ通る。"""
    path = _make_record(tmp_path, design_sha256="sha256:" + "0" * 64)
    assert _run_validator(path, root=tmp_path).returncode == 1
    assert _run_validator(path, "--baseline", "SUPERSEDED", root=tmp_path).returncode == 0


# --------------------------------------------------------------------------
# Directory全体の規律（BLOCKED-RECOVERY.md 再開条件3）
# --------------------------------------------------------------------------


def _run_check(records: Path, root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - 固定argv、shell不使用
        [
            sys.executable,
            "tools/check_blocked_records.py",
            "--records",
            str(records),
            "--design",
            str(DESIGN),
            "--registry",
            str(SNAPSHOT),
            "--root",
            str(root),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_committed_records_pass_the_directory_check() -> None:
    result = _run_check(RECORDS, REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


def test_open_record_on_a_superseded_baseline_requires_a_successor(tmp_path: Path) -> None:
    """設計書Hashが動いたのに後継Recordを出していない状態を検出する。

    `design_sha256`は不変Fieldであり書き換えられない。したがって
    「旧記録を解決済みにせず新しいRecordを作る」（BLOCKED-RECOVERY.md 再開条件3）
    が唯一の正しい手順であり、それを踏んだかどうかを機械で見る。
    """
    records = tmp_path / "records"
    records.mkdir()
    stale = _make_record(tmp_path, design_sha256="sha256:" + "0" * 64)
    shutil.move(str(stale), records / "BLK-20260816-FIXTURE.json")
    result = _run_check(records, tmp_path)
    assert result.returncode == 1
    assert "再開条件3" in result.stderr

    stale_path = records / "BLK-20260816-FIXTURE.json"
    successor = json.loads(stale_path.read_text(encoding="utf-8"))
    successor["blocker_id"] = "BLK-20260817-FIXTURE"
    successor["design_sha256"] = _design_hash()
    successor["supersedes"] = "BLK-20260816-FIXTURE"
    (records / "BLK-20260817-FIXTURE.json").write_text(
        json.dumps(successor, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # 後継を出しただけでは足りない。旧Recordを開けたままにすると
    # 同一論理TaskでOPENが2件並び、どれが現在の正本Blockerか読めなくなる。
    assert _run_check(records, tmp_path).returncode == 1
    assert "同一論理TaskでOPEN Recordが複数ある" in _run_check(records, tmp_path).stderr

    # 正しい手順は「後継を発行し、旧Recordを閉じる」までを含む。
    stale_record = json.loads(stale_path.read_text(encoding="utf-8"))
    stale_record["status"] = "RESOLVED"
    stale_record["resolved_at"] = "2026-08-17T00:00:00Z"
    stale_record["resolution"] = {
        "outcome": "SUPERSEDED_BY_DESIGN_HASH",
        "decided_at": "2026-08-17T00:00:00Z",
        "decided_by": "fixture",
        "summary": "設計書Hash変更により後継Recordへ引き継いだ",
        "successor_blocker_id": "BLK-20260817-FIXTURE",
    }
    stale_path.write_text(
        json.dumps(stale_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    assert _run_check(records, tmp_path).returncode == 0


# --------------------------------------------------------------------------
# 生成器
# --------------------------------------------------------------------------


def test_created_record_binds_evidence_hashes(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("created\n", encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
        [
            sys.executable,
            "tools/create_blocked_record.py",
            "--task-id",
            "FIXTURE-CREATE",
            "--release-scope",
            "MVP0-A",
            "--category",
            "SPEC_CLARIFICATION",
            "--summary",
            "fixture",
            "--command",
            "true",
            "--exit-code",
            "1",
            "--next-action",
            "a",
            "--acceptance",
            "b",
            "--owner",
            "synthetic-owner",
            "--design",
            str(DESIGN),
            "--registry",
            str(SNAPSHOT),
            "--evidence",
            "evidence.txt",
            "--root",
            str(tmp_path),
            "--out-dir",
            str(tmp_path / "records"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    created = json.loads(Path(result.stdout.strip()).read_text(encoding="utf-8"))
    assert created["record_version"] == "1.1"
    entry = created["observed"]["evidence_paths"][0]
    assert entry["hash_provenance"] == "MEASURED_AT_CREATION"
    assert entry["sha256"].startswith("sha256:")
    assert not list(_validator().iter_errors(created))


def test_missing_evidence_file_is_an_error_only_while_open(tmp_path: Path) -> None:
    """参照先Fileの不在は、OPENならerror、閉じたRecordならnoteであること。

    設計書の改名で閉じたRecordの参照先が消える。ここをerrorにすると
    **Block Recordが一度参照したFileを二度と改名できなくなる。**

    逆に、OPEN Recordで緩めてはならない。いま塞がっている事項の根拠が
    消えているのに進めてしまう。「中身が変わった」と同じ切り分けである。
    """
    root = tmp_path / "repo"
    root.mkdir()
    evidence = root / "gone.md"
    evidence.write_text("evidence\n", encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(evidence.read_bytes()).hexdigest()

    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    record = {
        "record_version": "1.1",
        "blocker_id": "BLK-20260818-TEST-EVIDENCE-GONE",
        "task_id": "TEST-EVIDENCE-GONE",
        "release_scope": "MVP0-A",
        "category": "SUT_FAILURE",
        "status": "OPEN",
        "observed": {
            "summary": "参照先Fileの不在の扱いを確かめる。",
            "command": ["true"],
            "exit_code": 1,
            "evidence_paths": [
                {
                    "path": "gone.md",
                    "sha256": digest,
                    "hash_provenance": "MEASURED_AT_CREATION",
                    "hash_observed_at": "2026-08-18T00:00:00Z",
                }
            ],
        },
        "next_action": {"action": "なし", "acceptance": ["なし"], "required_decision": None},
        "owner": "synthetic-owner",
        "created_at": "2026-08-18T00:00:00Z",
        "resolved_at": None,
        "design_sha256": snapshot["design_sha256"],
        "registry_snapshot_hash": snapshot["registry_snapshot_hash"],
        "supersedes": None,
        "resolution": None,
    }

    def check(status: str) -> subprocess.CompletedProcess[str]:
        payload = json.loads(json.dumps(record))
        payload["status"] = status
        if status == "RESOLVED":
            payload["resolved_at"] = "2026-08-18T01:00:00Z"
            payload["resolution"] = {
                "outcome": "RESOLVED_BY_FIX",
                "decided_at": "2026-08-18T01:00:00Z",
                "decided_by": "synthetic-owner",
                "summary": "解消した。",
                "verified_by": ["確認Command"],
            }
        path = root / "record.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return subprocess.run(  # noqa: S603 - 固定argv、shell不使用
            [
                sys.executable,
                str(REPO_ROOT / "tools" / "validate_blocked_record.py"),
                str(path),
                "--design",
                str(DESIGN),
                "--registry",
                str(SNAPSHOT),
                "--root",
                str(root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    # File があるうちは、どちらの状態でも通る。
    assert check("OPEN").returncode == 0
    evidence.unlink()

    opened = check("OPEN")
    assert opened.returncode != 0, "OPEN で参照先が消えているのに通った"
    assert "evidence file not found" in opened.stdout + opened.stderr

    closed = check("RESOLVED")
    assert closed.returncode == 0, f"閉じたRecordでerrorになった:\n{closed.stdout}\n{closed.stderr}"
    assert "referenced evidence file is gone" in closed.stdout + closed.stderr
    # 裏付けを出せない以上、監査Evidenceへは昇格させない。
    assert "audit_evidence=NO" in closed.stdout
