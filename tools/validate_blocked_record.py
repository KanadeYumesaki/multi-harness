#!/usr/bin/env python3
"""BLOCKED記録の有効性と設計／Registryの束縛を検査する。

## record_version

| Version | `observed.evidence_paths` | 監査Evidenceへの昇格 |
|---|---|---|
| `1.0` | Path文字列の配列 | しない。参照一覧に留まる |
| `1.1` | `{path, sha256, hash_provenance, hash_observed_at}` の配列 | 全EntryがMEASURED_AT_CREATIONかつHash一致のときだけ |

`1.0`はHashを持たないため、Path一覧を改変しなくても**参照先Fileの中身を差し替えられる**。
`1.1`はその差し替えを検出できるようにする（Owner Decision C-Q4）。旧Recordを読めなく
しないため`1.0`の受理は維持する。

## `--baseline`

`design_sha256`と`registry_snapshot_hash`はRecord作成時点の正本へ束縛される**不変Field**で
あり、設計書が改訂されても書き換えない。したがって「現在の設計書と一致するか」は
Record単体では判定できない。呼出側（`check_blocked_records.py`）が
Directory全体を見て`CURRENT`／`SUPERSEDED`を決め、本Toolへ渡す。

* `CURRENT`   … 現在の正本へ束縛されている前提で一致を要求する（既定）
* `SUPERSEDED`… 旧正本へ束縛された歴史的Record。一致を要求せず形式だけ検査する

`SUPERSEDED`のOPEN Recordを放置してよいわけではない。後継Recordの存在は
`check_blocked_records.py`が検査する（`BLOCKED-RECOVERY.md` 再開条件3）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

HASH = re.compile(r'^sha256:[0-9a-f]{64}$')
BLOCKER_ID = re.compile(r'^BLK-[0-9]{8}-[A-Z0-9-]+$')
TIMESTAMP = re.compile(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$')
SCOPES = {'MVP0-A', 'MVP0-B', 'MVP1-A', 'MVP0-C', 'MVP1-D'}
CATEGORIES = {'SPEC_CLARIFICATION', 'ENVIRONMENT', 'DEPENDENCY', 'SUT_FAILURE', 'HARNESS_FAILURE', 'SECURITY', 'DECISION_REQUIRED'}
RECORD_VERSIONS = {'1.0', '1.1'}
PROVENANCES = {'MEASURED_AT_CREATION', 'UNKNOWN'}
ALLOWED_KEYS = {
    'record_version', 'blocker_id', 'task_id', 'release_scope', 'category', 'status',
    'observed', 'next_action', 'owner', 'created_at', 'resolved_at',
    'design_sha256', 'registry_snapshot_hash', 'supersedes', 'resolution',
}
EVIDENCE_KEYS = {'path', 'sha256', 'hash_provenance', 'hash_observed_at'}


def sha256_file(path: Path) -> str:
    return 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()


def check_evidence(record: dict, root: Path, errors: list[str], notes: list[str]) -> bool:
    """Evidence参照を検査し、監査Evidenceへ昇格できるかを返す。

    昇格条件は「全EntryがMEASURED_AT_CREATIONで、記録Hashが現在のFileと一致すること」。
    1件でもUNKNOWNがあれば昇格しない。作成時点のHashが不明なEvidenceは、
    「当時のものと同一である」ことを機械的に示せないためである。

    ## 参照先Fileが無い場合の扱い

    `MEASURED_AT_CREATION` は「作成時にこのFileを実測した」という主張である。
    その主張の裏付けが消えているのは**整合性の失敗**であり、errorにする。

    ただしこれは **OPEN Recordについて**である。閉じたRecordはその時点の
    観測の写しであり、後から正本がリネームされるのは正常な進行である。
    ここをerrorにすると、Block Recordが一度参照したFileを二度と改名できなくなる。
    閉じたRecordの不在はnoteとし、監査Evidenceへは昇格させない。

    `UNKNOWN` と `record_version 1.0` は、そもそもHashによる裏付けを持たない。
    Fileの不在はその宣言の範囲内であり、errorにしない。Evidence一覧はAppend-onlyで
    あり、Pathの削除・置換で「何を根拠に塞がったか」を後から書き換えられない
    （Owner Decision C）。実際、C-1の改番で`tasks/TASK-MVP0A-003-LLM-FIRST.md`は
    リネームされたが、当時そのFileを見たという事実は変わらない。
    Fileが無いことはnoteとして出し、監査Evidenceへは昇格させない。
    """
    observed = record.get('observed') or {}
    entries = observed.get('evidence_paths', [])
    if not isinstance(entries, list):
        errors.append('observed.evidence_paths must be an array')
        return False

    if record.get('record_version') == '1.0':
        for entry in entries:
            if not isinstance(entry, str) or not entry.strip():
                errors.append('record_version 1.0: evidence_paths must be non-empty strings')
            elif not (root / entry).is_file():
                notes.append(f'note: referenced evidence file is gone: {entry}')
        # 1.0はHashを持たない。参照一覧であり監査Evidenceではない。
        return False

    promotable = True
    for index, entry in enumerate(entries):
        where = f'evidence_paths[{index}]'
        if not isinstance(entry, dict):
            errors.append(f'{where}: record_version 1.1 requires an object')
            promotable = False
            continue
        for key in sorted(set(entry) - EVIDENCE_KEYS):
            errors.append(f'{where}: unknown field {key}')
        for key in sorted(EVIDENCE_KEYS - set(entry)):
            errors.append(f'{where}: missing {key}')
        path = entry.get('path')
        provenance = entry.get('hash_provenance')
        digest = entry.get('sha256')
        observed_at = entry.get('hash_observed_at')
        if not isinstance(path, str) or not path.strip():
            errors.append(f'{where}: path invalid')
            promotable = False
            continue
        target = root / path
        if provenance not in PROVENANCES:
            errors.append(f'{where}: hash_provenance must be one of {sorted(PROVENANCES)}')
            promotable = False
            continue
        if provenance == 'UNKNOWN':
            # 「後から測った値」をMEASURED_AT_CREATIONと書けないようにする。
            if digest is not None or observed_at is not None:
                errors.append(f'{where}: UNKNOWN must carry sha256=null and hash_observed_at=null')
            if not target.is_file():
                notes.append(f'note: {where}: referenced evidence file is gone: {path}')
            promotable = False
            continue
        if not target.is_file():
            # OPEN なら、いま塞がっている事項の根拠が消えたということである。
            # 閉じたRecordなら、その時点の観測の写しであり、後から正本が
            # リネームされるのは正常な進行である。ここをerrorにすると、
            # Block Recordが一度参照したFileを二度と改名できなくなる。
            message = f'{where}: evidence file not found: {path}'
            if record.get('status') == 'OPEN':
                errors.append(message)
            else:
                notes.append(
                    f'note: {where}: referenced evidence file is gone: {path}'
                    f'（閉じたRecordの観測時点のPath）')
            promotable = False
            continue
        if not isinstance(digest, str) or not HASH.fullmatch(digest):
            errors.append(f'{where}: sha256 format invalid')
            promotable = False
            continue
        if not isinstance(observed_at, str) or not TIMESTAMP.fullmatch(observed_at):
            errors.append(f'{where}: hash_observed_at must be UTC ISO-8601')
            promotable = False
        if target.is_file() and sha256_file(target) != digest:
            # 中身が変わった。OPENと閉じたRecordで意味が違う。
            #
            # OPEN なら、いま塞がっている事項の根拠が動いたということである。
            # 判断のもとが変わったのだから、読み直さずに進めない。Errorにする。
            #
            # RESOLVED／CANCELLED なら、そのRecordは**その時点の観測の写し**である。
            # 後からRegistryやCoverage台帳が動くのは正常な進行であり、
            # 差し替えではない。ここをErrorにすると、Block Recordが一度参照した
            # Fileを二度と変更できなくなる。Registryが育たなくなる。
            #
            # どちらの場合も `promotable=False` にする。中身が変わった以上、
            # そのEvidenceを「当時のものと同一」として監査へ出せないことは同じである。
            message = f'{where}: evidence content changed since it was recorded: {path}'
            if record.get('status') == 'OPEN':
                errors.append(message)
            else:
                notes.append(f'note: {message}（閉じたRecordの観測時点との差分）')
            promotable = False
    return promotable and bool(entries)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('record', type=Path)
    parser.add_argument('--design', required=True, type=Path)
    parser.add_argument('--registry', required=True, type=Path)
    parser.add_argument('--baseline', default='CURRENT', choices=['CURRENT', 'SUPERSEDED'])
    parser.add_argument('--root', type=Path, default=Path('.'),
                        help='Evidence Pathを解決するRepository Root')
    args = parser.parse_args()
    errors: list[str] = []
    notes: list[str] = []
    try:
        record = json.loads(args.record.read_text(encoding='utf-8'))
        snapshot = json.loads(args.registry.read_text(encoding='utf-8'))
    except Exception as exc:
        print(f'input invalid: {exc}')
        return 4
    required = {'record_version', 'blocker_id', 'task_id', 'release_scope', 'category', 'status', 'observed', 'next_action', 'owner', 'created_at', 'design_sha256', 'registry_snapshot_hash'}
    for key in sorted(set(record) - ALLOWED_KEYS):
        errors.append(f'unknown field {key}')
    for key in sorted(required - set(record)):
        errors.append(f'missing {key}')
    if record.get('record_version') not in RECORD_VERSIONS:
        errors.append(f'record_version must be one of {sorted(RECORD_VERSIONS)}')
    if not BLOCKER_ID.fullmatch(str(record.get('blocker_id', ''))):
        errors.append('blocker_id format invalid')
    if not isinstance(record.get('task_id'), str) or not record.get('task_id').strip():
        errors.append('task_id invalid')
    if not isinstance(record.get('owner'), str) or not record.get('owner').strip():
        errors.append('owner invalid')
    if not isinstance(record.get('created_at'), str) or not record.get('created_at').endswith('Z'):
        errors.append('created_at must be UTC ISO-8601')
    if record.get('release_scope') not in SCOPES:
        errors.append('invalid release_scope')
    if record.get('category') not in CATEGORIES:
        errors.append('invalid category')
    if record.get('status') not in {'OPEN', 'RESOLVED', 'CANCELLED'}:
        errors.append('invalid status')
    supersedes = record.get('supersedes')
    if supersedes is not None and not BLOCKER_ID.fullmatch(str(supersedes)):
        errors.append('supersedes format invalid')
    actual_design = 'sha256:' + hashlib.sha256(args.design.read_bytes()).hexdigest()
    if args.baseline == 'CURRENT':
        if record.get('design_sha256') != actual_design:
            errors.append('design_sha256 mismatch')
        if record.get('registry_snapshot_hash') != snapshot.get('registry_snapshot_hash'):
            errors.append('registry_snapshot_hash mismatch')
    if not HASH.fullmatch(str(record.get('design_sha256', ''))) or not HASH.fullmatch(str(record.get('registry_snapshot_hash', ''))):
        errors.append('hash format invalid')
    observed = record.get('observed') or {}
    if not observed.get('summary') or not isinstance(observed.get('command'), list):
        errors.append('observed summary/command invalid')
    promotable = check_evidence(record, args.root, errors, notes)
    next_action = record.get('next_action') or {}
    if not next_action.get('action') or not next_action.get('acceptance'):
        errors.append('next_action/acceptance invalid')
    if not isinstance(next_action.get('acceptance'), list) or any(
        not isinstance(item, str) or not item.strip() for item in next_action.get('acceptance', [])
    ):
        errors.append('next_action.acceptance must be a non-empty string list')
    if record.get('status') == 'RESOLVED' and not record.get('resolved_at'):
        errors.append('resolved_at required for RESOLVED record')
    for note in notes:
        print(note)
    for error in errors:
        print(error)
    print(f'baseline={args.baseline} '
          f'record_version={record.get("record_version")} '
          f'audit_evidence={"YES" if promotable and not errors else "NO"}')
    print(f'blocked record errors: {len(errors)}')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
