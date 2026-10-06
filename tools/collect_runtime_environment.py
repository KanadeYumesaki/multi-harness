#!/usr/bin/env python3
"""WSL2/Linux native filesystemの実測Runtime Environment Evidenceを生成する。"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import sys
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def unescape_mount(value: str) -> str:
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), value)


def find_mount(workspace: Path, mountinfo: str) -> tuple[str, str, int]:
    candidates = []
    for line in mountinfo.splitlines():
        left, sep, right = line.partition(' - ')
        if not sep:
            continue
        fields, tail = left.split(), right.split()
        if len(fields) < 5 or not tail:
            continue
        mount_point = Path(unescape_mount(fields[4])).resolve()
        try:
            workspace.relative_to(mount_point)
        except ValueError:
            continue
        candidates.append((len(mount_point.parts), str(mount_point), tail[0], int(fields[0])))
    if not candidates:
        raise ValueError(f'no mount found for {workspace}')
    _, mount_point, fs_type, mount_id = max(candidates)
    return mount_point, fs_type, mount_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', required=True, type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--python-lock', required=True, type=Path)
    parser.add_argument('--out-root', required=True, type=Path)
    args = parser.parse_args()

    workspace = args.workspace.resolve(strict=True)
    if not workspace.is_dir():
        print('workspace must be a directory', file=sys.stderr)
        return 4
    if workspace == Path('/mnt') or str(workspace).startswith('/mnt/'):
        print('WORKSPACE_ON_FOREIGN_FS_DENIED: /mnt/*', file=sys.stderr)
        return 3
    proc_version = Path('/proc/version').read_text(encoding='utf-8')
    is_wsl2 = 'microsoft' in proc_version.lower() and 'wsl2' in (
        proc_version + platform.release()).lower()
    if not is_wsl2:
        print('runtime is not WSL2', file=sys.stderr)
        return 3
    mountinfo_path = Path('/proc/self/mountinfo')
    mountinfo_bytes = mountinfo_path.read_bytes()
    mountinfo = mountinfo_bytes.decode('utf-8')
    mount_point, fs_type, mount_id = find_mount(workspace, mountinfo)
    denied_types = {'9p', 'drvfs', 'cifs', 'nfs', 'nfs4', 'fuseblk', 'overlay'}
    if fs_type.lower() in denied_types:
        print(f'WORKSPACE_ON_FOREIGN_FS_DENIED: {fs_type}', file=sys.stderr)
        return 3

    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    if not isinstance(manifest, dict):
        print('manifest must be an object', file=sys.stderr)
        return 4
    if not args.python_lock.is_file():
        print('python lock file missing', file=sys.stderr)
        return 4
    now = dt.datetime.now(dt.timezone.utc).replace(
        microsecond=0).isoformat().replace('+00:00', 'Z')
    evidence_dir = args.out_root / 'environment'
    evidence_dir.mkdir(parents=True, exist_ok=True)
    saved_mountinfo = evidence_dir / 'mountinfo.txt'
    saved_mountinfo.write_bytes(mountinfo_bytes)
    payload = {
        'evidence_schema_version': '1.2',
        'release_scope': manifest.get('release_scope'),
        'area': 'environment', 'status': 'PASS',
        'summary': {'collector': 'live-/proc', 'uname': platform.uname()._asdict()},
        'producer': 'tools/collect_runtime_environment.py',
        'test_run_id': f'environment-{os.getpid()}',
        'started_at': now, 'recorded_at': now,
        'implementation_commit_sha': manifest.get('implementation_commit_sha'),
        'schema_set_hash': manifest.get('schema_set_hash'),
        'migration_head': manifest.get('migration_head'),
        'is_wsl2': True, 'workspace_on_linux_native_fs': True,
        'python_version': platform.python_version(),
        'workspace_root': str(workspace), 'mount_point': mount_point,
        'filesystem_type': fs_type, 'mount_id': mount_id,
        'mountinfo_path': 'environment/mountinfo.txt',
        'mountinfo_hash': sha256_bytes(mountinfo_bytes),
        'python_lock_hash': sha256_file(args.python_lock),
    }
    out = evidence_dir / 'runtime-environment.json'
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8') + b'\n'
    out.write_bytes(data)
    print(json.dumps({'path': str(out), 'sha256': sha256_bytes(data)}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
