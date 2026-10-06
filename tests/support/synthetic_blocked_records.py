"""合成の Block Record 一式を作る（公開側の試験用）。

## なぜ要るか

`blocked/records/` は開発中の塞がりの記録であり、公開用の配布コピーに収録しない。
配布コピーでは一覧が空なので、「現行 Record に規則が成り立つ」試験は何も見ない
まま通る。規則が実際に働くこと（成り立つ一覧は通し、崩れた一覧は落とす）は、
一覧を合成して確かめる。

## 正規の検査器を通す

作った一覧は CI と同じ `tools/check_blocked_records.py`（`validate_blocked_record.py`
を含む）で受理されることを、生成直後に確かめる。受理されない一覧を「整合した
一覧」として試験へ渡さない。

## 何ではないか

実在の塞がり・Owner の判断・Runtime Evidence ではない。`owner` と `decided_by` は
`synthetic-owner` であり、全 Record の `observed.summary` に合成の印を持つ。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER = REPO_ROOT / "tools/check_blocked_records.py"
MARK = "SYNTHETIC-TEST-INPUT"
STAMP = "2026-01-01T00:00:00Z"
OLD_DESIGN = "sha256:" + "a" * 64
OLD_REGISTRY = "sha256:" + "b" * 64

CHAIN_OLD = "BLK-20260101-SYNTHETIC-CHAIN-R1"
CHAIN_HEAD = "BLK-20260102-SYNTHETIC-CHAIN-R2"
FIXED = "BLK-20260103-SYNTHETIC-FIXED"
OTHER_OPEN = "BLK-20260104-SYNTHETIC-OTHER"


def current_binding(design: Path, registry: Path) -> tuple[str, str]:
    design_hash = "sha256:" + hashlib.sha256(design.read_bytes()).hexdigest()
    snapshot = json.loads(registry.read_text(encoding="utf-8"))
    return design_hash, str(snapshot["registry_snapshot_hash"])


def _record(
    blocker_id: str,
    task_id: str,
    *,
    status: str,
    design: str,
    registry: str,
    supersedes: str | None = None,
    resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "record_version": "1.1",
        "blocker_id": blocker_id,
        "task_id": task_id,
        "release_scope": "MVP0-A",
        "category": "DECISION_REQUIRED",
        "status": status,
        "observed": {"summary": f"{MARK} {blocker_id}", "command": ["true"], "exit_code": 0},
        "next_action": {
            "action": f"{MARK}: 合成の次手",
            "acceptance": [f"{MARK}: {blocker_id} の受入条件"],
            "required_decision": None,
        },
        "owner": "synthetic-owner",
        "created_at": STAMP,
        "resolved_at": STAMP if status == "RESOLVED" else None,
        "design_sha256": design,
        "registry_snapshot_hash": registry,
        "supersedes": supersedes,
        "resolution": resolution,
    }


def corpus(design_hash: str, registry_hash: str) -> list[dict[str, Any]]:
    """後継連鎖 1 本・解消済み 1 件・別 Task の OPEN 1 件からなる整合した一覧。"""
    return [
        _record(
            CHAIN_OLD,
            "SYNTHETIC-CHAIN",
            status="RESOLVED",
            design=OLD_DESIGN,
            registry=OLD_REGISTRY,
            resolution={
                "outcome": "SUPERSEDED_BY_DESIGN_HASH",
                "decided_at": STAMP,
                "decided_by": "synthetic-owner",
                "summary": f"{MARK}: 設計書 Hash の変更で後継へ引き継いだ",
                "successor_blocker_id": CHAIN_HEAD,
            },
        ),
        _record(
            CHAIN_HEAD,
            "SYNTHETIC-CHAIN",
            status="OPEN",
            design=design_hash,
            registry=registry_hash,
            supersedes=CHAIN_OLD,
        ),
        _record(
            FIXED,
            "SYNTHETIC-FIXED",
            status="RESOLVED",
            design=design_hash,
            registry=registry_hash,
            resolution={
                "outcome": "RESOLVED_BY_FIX",
                "decided_at": STAMP,
                "decided_by": "synthetic-owner",
                "summary": f"{MARK}: 修正で解消した",
                "verified_by": [f"{MARK}: 合成の確認 Command"],
            },
        ),
        _record(
            OTHER_OPEN,
            "SYNTHETIC-OTHER",
            status="OPEN",
            design=design_hash,
            registry=registry_hash,
        ),
    ]


def write_corpus(directory: Path, records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    directory.mkdir(parents=True, exist_ok=True)
    written: dict[str, dict[str, Any]] = {}
    for record in records:
        path = directory / f"{record['blocker_id']}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written[str(path)] = record
    return written


def run_checker(directory: Path, design: Path, registry: Path) -> subprocess.CompletedProcess[str]:
    """CI と同じ検査器を、同じ引数の形で合成の一覧へ当てる。"""
    return subprocess.run(  # noqa: S603 - 固定 argv、shell 不使用
        [
            sys.executable,
            str(CHECKER),
            "--records",
            str(directory),
            "--design",
            str(design),
            "--registry",
            str(registry),
            "--root",
            str(REPO_ROOT),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def build_checked_corpus(
    directory: Path, design: Path, registry: Path
) -> dict[str, dict[str, Any]]:
    """整合した一覧を書き、正規の検査器が受理することを確かめてから返す。"""
    design_hash, registry_hash = current_binding(design, registry)
    written = write_corpus(directory, corpus(design_hash, registry_hash))
    result = run_checker(directory, design, registry)
    if result.returncode != 0:
        raise AssertionError(
            "synthetic block records were not accepted by the checker:\n"
            + result.stdout
            + result.stderr
        )
    return written
