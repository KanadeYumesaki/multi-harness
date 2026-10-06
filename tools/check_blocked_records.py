#!/usr/bin/env python3
"""Repository内のBLOCKED Recordを一括検証する。

記録が無い場合は正常（初回状態）とし、重複IDがないことと、各Recordが
「作成時点の正本」へ正しく束縛されていることを検査する。

## 設計書Hashが動いたRecordの扱い

`design_sha256`／`registry_snapshot_hash`はRecord作成時点の正本への束縛であり、
**不変Field**である。設計書が改訂されても書き換えない。したがって
「全Recordが現在の設計書と一致する」という以前の前提は、設計書を1度でも
改訂した時点で成立しない。

`BLOCKED-RECOVERY.md` 再開条件3はこの場合の手順を定めている。

    design_sha256 または Registry Snapshot が変わった場合、
    旧記録を解決済みにせず新しいRecordを作る。

本Toolはそれを機械検査する。

| Recordの状態 | 判定 |
|---|---|
| 現在の正本へ束縛 | `CURRENT`。Hash一致を要求する |
| 旧正本へ束縛・`RESOLVED`／`CANCELLED` | `SUPERSEDED`。歴史的記録として許容 |
| 旧正本へ束縛・`OPEN`・後継Recordあり | `SUPERSEDED`。後継が現正本へ束縛されていることを要求 |
| 旧正本へ束縛・`OPEN`・後継Recordなし | **失敗**。再開条件3が未実施 |

後継Recordは`supersedes`に旧`blocker_id`を持つ。旧Record側は書き換えない
（不変条件#1「訂正は上書きでなく追記で行う」）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=Path, default=Path("blocked/records"))
    parser.add_argument("--design", required=True, type=Path)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    if not args.records.exists():
        print("blocked records: none")
        return 0
    if not args.records.is_dir():
        print(f"blocked records path is not a directory: {args.records}", file=sys.stderr)
        return 4

    validator = Path(__file__).with_name("validate_blocked_record.py")
    design_hash = "sha256:" + hashlib.sha256(args.design.read_bytes()).hexdigest()
    snapshot_hash = json.loads(args.registry.read_text(encoding="utf-8")).get(
        "registry_snapshot_hash")

    seen: set[str] = set()
    failures = 0
    paths = sorted(args.records.glob("*.json"))
    records: dict[str, dict] = {}
    for record_path in paths:
        if not record_path.name.startswith("BLK-"):
            print(f"{record_path}: filename must start with BLK-", file=sys.stderr)
            failures += 1
            continue
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
            blocker_id = record.get("blocker_id")
        except (OSError, json.JSONDecodeError) as exc:
            print(f"{record_path}: invalid JSON: {exc}", file=sys.stderr)
            failures += 1
            continue
        if blocker_id in seen:
            print(f"{record_path}: duplicate blocker_id {blocker_id}", file=sys.stderr)
            failures += 1
        seen.add(blocker_id)
        records[str(record_path)] = record

    # 後継関係。旧blocker_id -> 後継Record。
    #
    # 後継は2箇所で表明されうる。
    #   * 後継側の `supersedes`（現行。設計書Hash変更による再発行）
    #   * 旧側の `resolution.successor_blocker_id`（C-1改番のように旧Recordを終了させた場合）
    # どちらも「この記録の後を継ぐのはどれか」を指す。両方を辿る。
    by_id = {r.get("blocker_id"): r for r in records.values()}
    successors: dict[str, list[dict]] = {}
    for record in records.values():
        predecessor = record.get("supersedes")
        if predecessor:
            successors.setdefault(predecessor, []).append(record)
        declared = (record.get("resolution") or {}).get("successor_blocker_id")
        if declared and declared in by_id:
            successors.setdefault(record.get("blocker_id"), []).append(by_id[declared])

    def bound_to_current(record: dict) -> bool:
        return (
            record.get("design_sha256") == design_hash
            and record.get("registry_snapshot_hash") == snapshot_hash
        )

    def chain_reaches_current(record: dict, visited: set[str]) -> bool:
        """後継を辿って現正本へ束縛されたRecordに到達するか。"""
        blocker_id = record.get("blocker_id")
        if blocker_id in visited:
            return False
        visited.add(blocker_id)
        for successor in successors.get(blocker_id, []):
            if bound_to_current(successor) or chain_reaches_current(successor, visited):
                return True
        return False

    for record_path, record in records.items():
        bound_current = bound_to_current(record)
        baseline = "CURRENT" if bound_current else "SUPERSEDED"
        if not bound_current and record.get("status") == "OPEN":
            # 再開条件3。現正本へ束縛された後継Recordが要る。
            if not chain_reaches_current(record, set()):
                print(
                    f"{record_path}: OPEN record is bound to a superseded baseline and no "
                    f"successor chain reaches the current one. "
                    f"BLOCKED-RECOVERY.md 再開条件3: 旧記録を解決済みにせず新しいRecordを作る "
                    f"(supersedes={record.get('blocker_id')})",
                    file=sys.stderr,
                )
                failures += 1
        predecessor = record.get("supersedes")
        if predecessor and predecessor not in seen:
            print(f"{record_path}: supersedes unknown blocker_id {predecessor}", file=sys.stderr)
            failures += 1

        # 後継関係の双方向整合。
        #
        # 片方向だけだと「旧が後継を指すのに後継が知らない」「後継が旧を指すのに
        # 旧が閉じていない」を見逃す。どちらも監査上は連鎖が切れている状態である。
        declared_successor = (record.get("resolution") or {}).get("successor_blocker_id")
        if declared_successor:
            if declared_successor not in by_id:
                print(
                    f"{record_path}: resolution.successor_blocker_id が未知の "
                    f"blocker_id を指す: {declared_successor}",
                    file=sys.stderr,
                )
                failures += 1
            elif record.get("status") == "OPEN":
                print(
                    f"{record_path}: successor_blocker_id を持つのに status=OPEN のままである。"
                    "後継へ引き継いだ記録は RESOLVED または CANCELLED で閉じる",
                    file=sys.stderr,
                )
                failures += 1
        if predecessor and predecessor in by_id:
            back = (by_id[predecessor].get("resolution") or {}).get("successor_blocker_id")
            if back and back != record.get("blocker_id"):
                print(
                    f"{record_path}: supersedes={predecessor} だが、その旧Recordの "
                    f"successor_blocker_id は {back} を指しており双方向に一致しない",
                    file=sys.stderr,
                )
                failures += 1

        print(f"{record_path}: baseline={baseline} status={record.get('status')}")
        result = subprocess.run(
            [sys.executable, str(validator), str(record_path), "--design", str(args.design),
             "--registry", str(args.registry), "--baseline", baseline,
             "--root", str(args.root)],
            check=False,
        )
        failures += int(result.returncode != 0)
    failures += _check_single_open_per_logical_task(records)

    print(f"blocked records checked: {len(paths)} errors: {failures}")
    return 1 if failures else 0


def _check_single_open_per_logical_task(records: dict[str, dict]) -> int:
    """同一論理TaskでOPEN Recordが複数あったら失敗させる。

    「論理Task」は`task_id`だけでは決まらない。C-1の改番で`task_id`が
    `TASK-MVP0A-003-LLM-FIRST` -> `TASK-LLM-001` と変わっており、
    設計書Hash変更では`task_id`が同じまま別Recordが増えるためである。

    そこで **後継関係で連結された成分** を1つの論理Taskとして扱う。
    `supersedes` と `resolution.successor_blocker_id` の双方を辺とみなし、
    同じ成分内にOPENが2件以上あれば失敗させる。

    ここを検査しないと「旧Recordを閉じ忘れたまま後継を発行する」が通り、
    どれが現在の正本Blockerなのか読む人が判断できなくなる。
    """
    by_id = {r.get("blocker_id"): r for r in records.values()}
    parent: dict[str, str] = {key: key for key in by_id}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        if left not in parent or right not in parent:
            return
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for record in records.values():
        blocker_id = record.get("blocker_id")
        predecessor = record.get("supersedes")
        if predecessor:
            union(predecessor, blocker_id)
        successor = (record.get("resolution") or {}).get("successor_blocker_id")
        if successor:
            union(blocker_id, successor)
        # 同じ task_id も同一論理Taskとして束ねる（改番していない系列のため）。
        for other in records.values():
            if other is not record and other.get("task_id") == record.get("task_id"):
                union(blocker_id, other.get("blocker_id"))

    groups: dict[str, list[str]] = {}
    for blocker_id, record in by_id.items():
        if record.get("status") == "OPEN":
            groups.setdefault(find(blocker_id), []).append(blocker_id)

    failures = 0
    for members in groups.values():
        if len(members) > 1:
            print(
                "同一論理TaskでOPEN Recordが複数ある: " + ", ".join(sorted(members)),
                file=sys.stderr,
            )
            failures += 1
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
