#!/usr/bin/env python3
"""AIタスクがBLOCKEDになったときの再開用記録を生成する。

`record_version` は `1.1` で出す。`observed.evidence_paths` は
`{path, sha256, hash_provenance, hash_observed_at}` のObject配列であり、
`--evidence` で渡したFileのSHA-256を**生成時に実測**して
`hash_provenance=MEASURED_AT_CREATION` として記録する（Owner Decision C-Q4）。

Path一覧のAppend-onlyだけでは、参照先Fileの中身の差し替えを防げない。
Hashが無い限り「そのEvidenceが当時のものと同一である」ことを機械的に示せない。

設計書Hash／Registry Snapshotが変わったために再発行する場合は `--supersedes`
へ旧`blocker_id`を渡す（`BLOCKED-RECOVERY.md` 再開条件3）。旧Recordは書き換えない。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
from pathlib import Path


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(
        microsecond=0).isoformat().replace('+00:00', 'Z')


def sha256_file(path: Path) -> str:
    return 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()


def evidence_reference(path: str, stamp: str, root: Path) -> dict:
    """作成時点で実測したHashを持つEvidence参照。

    Fileが無ければ生成しない。存在しないPathをEvidenceとして列挙させると、
    「根拠がある」という主張だけが残る。
    """
    target = root / path
    if not target.is_file():
        raise SystemExit(f'evidence file not found: {path}')
    return {
        'path': path,
        'sha256': sha256_file(target),
        'hash_provenance': 'MEASURED_AT_CREATION',
        'hash_observed_at': stamp,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--release-scope', required=True)
    parser.add_argument('--category', required=True)
    parser.add_argument('--summary', required=True)
    parser.add_argument('--command', nargs='+', required=True)
    parser.add_argument('--exit-code', required=True, type=int)
    parser.add_argument('--next-action', required=True)
    parser.add_argument('--acceptance', nargs='+', required=True)
    parser.add_argument('--owner', required=True)
    parser.add_argument('--design', required=True, type=Path)
    parser.add_argument('--registry', required=True, type=Path)
    parser.add_argument('--evidence', nargs='*', default=[],
                        help='Evidence Path。生成時にSHA-256を実測して束縛する')
    parser.add_argument('--supersedes', default=None,
                        help='再発行時の旧blocker_id（BLOCKED-RECOVERY.md 再開条件3）')
    parser.add_argument('--required-decision', default=None)
    parser.add_argument('--id-suffix', default=None,
                        help='blocker_idの末尾へ足す識別子。同日同Taskの再発行で衝突を避ける')
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--out-dir', default='blocked/records', type=Path)
    args = parser.parse_args()
    stamp = now()
    suffix = re.sub(r'[^A-Z0-9-]+', '-', args.task_id.upper()).strip('-')[:32]
    if not suffix:
        parser.error('--task-id must contain at least one ASCII letter, digit, or hyphen')
    if args.id_suffix:
        extra = re.sub(r'[^A-Z0-9-]+', '-', args.id_suffix.upper()).strip('-')
        if not extra:
            parser.error('--id-suffix must contain at least one ASCII letter, digit, or hyphen')
        suffix = f'{suffix}-{extra}'
    blocker_id = f'BLK-{stamp[:10].replace("-", "")}-{suffix}'
    record = {
        'record_version': '1.1', 'blocker_id': blocker_id,
        'task_id': args.task_id, 'release_scope': args.release_scope,
        'category': args.category, 'status': 'OPEN',
        'observed': {'summary': args.summary, 'command': args.command,
                     'exit_code': args.exit_code,
                     'evidence_paths': [
                         evidence_reference(path, stamp, args.root)
                         for path in args.evidence
                     ]},
        'next_action': {'action': args.next_action, 'acceptance': args.acceptance,
                        'required_decision': args.required_decision},
        'owner': args.owner, 'created_at': stamp, 'resolved_at': None,
        'design_sha256': sha256_file(args.design),
        'registry_snapshot_hash': json.loads(args.registry.read_text(
            encoding='utf-8'))['registry_snapshot_hash'],
        'supersedes': args.supersedes,
        'resolution': None,
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    path = args.out_dir / f'{blocker_id}.json'
    if path.exists():
        raise SystemExit(f'{path} already exists; pass --id-suffix to issue a distinct record')
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
