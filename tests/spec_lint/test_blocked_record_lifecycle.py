"""Block Recordのライフサイクル整合（Phase 1）。

Block Recordは「いま何が塞がっているか」の唯一の一覧である。旧記録を閉じ忘れると
OPENが複数並び、**どれが現在の正本Blockerなのか読む人が判断できなくなる**。

実際に次の2つが同時に起きていた。

* `BLK-20260816-TASK-MVP0A-003-LLM-FIRST` が `resolution` を持つのに `status=OPEN`
* 設計書Hash変更で発行した後継Recordと旧Recordが**両方OPEN**

ここで固定するのは次である。

1. 同一論理TaskでOPENは高々1件
2. 後継関係が双方向に整合している
3. 検出Logicが実際に働く（Tool自身へ改変を注入して確かめる）

## 一覧が空でも何かを見る

`blocked/records/` は開発中の塞がりの記録であり、公開用の配布コピーに収録しない。
一覧が空なら「現行 Record に規則が成り立つ」試験は何も見ずに通る。そこで規則を
関数にし、現行の一覧と、正規の検査器が受理した合成の一覧
（`tests/support/synthetic_blocked_records.py`）の両方へ当てる。検出側は合成の一覧を
崩して確かめる。保存 Record 固有の事実は `tests/private_history/` の試験が持つ。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
RECORDS_DIR = REPO_ROOT / "blocked" / "records"
DESIGN = REPO_ROOT / "design-v1.25-runtime-go.md"
SNAPSHOT = REPO_ROOT / "registry-snapshot.json"

sys.path.insert(0, str(REPO_ROOT / "tools"))
from check_blocked_records import _check_single_open_per_logical_task  # noqa: E402


def _load_records() -> dict[str, dict[str, Any]]:
    return {
        str(path): json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(RECORDS_DIR.glob("*.json"))
    }


def test_checker_reports_no_error_for_the_current_records() -> None:
    """CIと同じToolを同じ引数で呼ぶ。Logicを再実装しない。"""
    result = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
        [
            sys.executable,
            "tools/check_blocked_records.py",
            "--design",
            "design-v1.25-runtime-go.md",
            "--registry",
            "registry-snapshot.json",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def test_successor_links_are_bidirectional() -> None:
    """`supersedes` と `resolution.successor_blocker_id` が食い違わないこと。"""
    _assert_successor_links_are_bidirectional(_load_records())


def _assert_successor_links_are_bidirectional(records: dict[str, dict[str, Any]]) -> None:
    by_id = {record["blocker_id"]: record for record in records.values()}

    for record in records.values():
        successor_id = (record.get("resolution") or {}).get("successor_blocker_id")
        if not successor_id:
            continue
        assert successor_id in by_id, f"{record['blocker_id']}: 未知の後継 {successor_id}"
        assert record["status"] != "OPEN", f"{record['blocker_id']}: 後継を持つのに OPEN のまま"

    for record in records.values():
        predecessor_id = record.get("supersedes")
        if not predecessor_id:
            continue
        assert predecessor_id in by_id, f"{record['blocker_id']}: 未知の前任 {predecessor_id}"
        back = (by_id[predecessor_id].get("resolution") or {}).get("successor_blocker_id")
        if back:
            assert back == record["blocker_id"], (
                f"{predecessor_id} の successor_blocker_id={back} が "
                f"{record['blocker_id']} と一致しない"
            )


# 設計書 §23.7。引き継ぎ系は後継必須、解消系は後継を持たず検証根拠を持つ。
_SUPERSEDING = frozenset({"SUPERSEDED_BY_RENUMBERING", "SUPERSEDED_BY_DESIGN_HASH"})
_RESOLVING = frozenset({"RESOLVED_BY_FIX"})


def test_resolved_records_carry_an_outcome() -> None:
    """`RESOLVED`は理由Codeを持つ。閉じた理由が分からない記録を残さない。"""
    _assert_resolved_records_carry_an_outcome(_load_records())


def _assert_resolved_records_carry_an_outcome(records: dict[str, dict[str, Any]]) -> None:
    for record in records.values():
        if record["status"] != "RESOLVED":
            continue
        resolution = record.get("resolution") or {}
        outcome = resolution.get("outcome")
        assert outcome in _SUPERSEDING | _RESOLVING, (
            f"{record['blocker_id']}: 未知の outcome {outcome!r}"
        )
        assert record.get("resolved_at"), f"{record['blocker_id']}: resolved_at が無い"
        if outcome in _SUPERSEDING:
            assert resolution.get("successor_blocker_id"), (
                f"{record['blocker_id']}: 引き継ぎなのに後継が記録されていない"
            )
        else:
            # 解消は引き継ぎではない。存在しない後継を指させない。
            assert not resolution.get("successor_blocker_id"), (
                f"{record['blocker_id']}: 解消なのに後継を指している"
            )


def test_superseding_outcome_is_not_used_for_an_actual_fix() -> None:
    """解消を`SUPERSEDED_BY_DESIGN_HASH`で代用していないこと。

    引き継ぎ系Outcomeは後継を必ず持つ。後継が無いのに引き継ぎ系を名乗る
    Recordがあれば、それは解消を引き継ぎに見せかけている。
    """
    _assert_superseding_outcomes_have_a_successor(_load_records())


def _assert_superseding_outcomes_have_a_successor(records: dict[str, dict[str, Any]]) -> None:
    by_id = {record["blocker_id"]: record for record in records.values()}
    for record in records.values():
        resolution = record.get("resolution") or {}
        if resolution.get("outcome") not in _SUPERSEDING:
            continue
        successor_id = resolution["successor_blocker_id"]
        assert successor_id in by_id, (
            f"{record['blocker_id']}: 引き継ぎ先 {successor_id} が実在しない"
        )


# ---------------------------------------------------------------------------
# 合成の一覧（正規の検査器が受理したもの）で規則と検出を測る
# ---------------------------------------------------------------------------


@pytest.fixture
def synthetic_records(tmp_path: Path) -> dict[str, dict[str, Any]]:
    from synthetic_blocked_records import build_checked_corpus

    return build_checked_corpus(tmp_path / "records", DESIGN, SNAPSHOT)


def test_the_rules_hold_for_a_consistent_synthetic_corpus(
    synthetic_records: dict[str, dict[str, Any]],
) -> None:
    """規則が成り立つ一覧で、規則が何かを見て通ること。**空の一覧で通さない。**"""
    from synthetic_blocked_records import CHAIN_HEAD, OTHER_OPEN

    assert _check_single_open_per_logical_task(synthetic_records) == 0
    open_ids = sorted(r["blocker_id"] for r in synthetic_records.values() if r["status"] == "OPEN")
    assert open_ids == sorted([CHAIN_HEAD, OTHER_OPEN])
    _assert_successor_links_are_bidirectional(synthetic_records)
    _assert_resolved_records_carry_an_outcome(synthetic_records)
    _assert_superseding_outcomes_have_a_successor(synthetic_records)
    fixed = [
        r
        for r in synthetic_records.values()
        if (r.get("resolution") or {}).get("outcome") in _RESOLVING
    ]
    assert fixed, "RESOLVED_BY_FIX の Record が無い。試験が何も見ていない"
    for record in fixed:
        verified = record["resolution"].get("verified_by")
        assert isinstance(verified, list) and verified
        assert all(isinstance(item, str) and item.strip() for item in verified)


def test_duplicate_open_in_the_same_chain_is_detected(
    synthetic_records: dict[str, dict[str, Any]],
) -> None:
    """旧Recordを閉じ忘れた状態を検出できること。読み込んだ辞書だけを改変する。"""
    from synthetic_blocked_records import CHAIN_OLD

    for record in synthetic_records.values():
        if record["blocker_id"] == CHAIN_OLD:
            record["status"] = "OPEN"
    assert _check_single_open_per_logical_task(synthetic_records) == 1


def test_duplicate_open_with_same_task_id_is_detected(
    synthetic_records: dict[str, dict[str, Any]],
) -> None:
    """後継Linkが無くても`task_id`が同じならまとめて検出すること。"""
    from synthetic_blocked_records import OTHER_OPEN

    base = next(r for r in synthetic_records.values() if r["blocker_id"] == OTHER_OPEN)
    duplicate = {**base, "blocker_id": "BLK-20260105-SYNTHETIC-OTHER-DUP"}
    synthetic_records["synthetic-duplicate.json"] = duplicate
    assert _check_single_open_per_logical_task(synthetic_records) == 1


def test_the_checker_rejects_a_broken_synthetic_chain(tmp_path: Path) -> None:
    """CI と同じ検査器が、崩した一覧を拒否すること（後継を持つのに OPEN）。"""
    from synthetic_blocked_records import (
        CHAIN_OLD,
        corpus,
        current_binding,
        run_checker,
        write_corpus,
    )

    records = corpus(*current_binding(DESIGN, SNAPSHOT))
    for record in records:
        if record["blocker_id"] == CHAIN_OLD:
            record["status"] = "OPEN"
            record["resolved_at"] = None
    directory = tmp_path / "records"
    write_corpus(directory, records)
    result = run_checker(directory, DESIGN, SNAPSHOT)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "successor_blocker_id を持つのに status=OPEN" in result.stderr
