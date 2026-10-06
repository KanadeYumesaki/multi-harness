#!/usr/bin/env python3
"""Verifier Sourceをリポジトリ外の期待Hashと照合する。

expectedファイルは同じUIDで更新できるため、ブランチ保護、Required Check、レビュー者分離と併用する。このツールは外部管理者の代替ではない。"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

HASH_LINE = re.compile(r'^sha256:[0-9a-f]{64}\s+(.+?)\s*$')


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return 'sha256:' + digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', required=True, type=Path)
    parser.add_argument('--expected', required=True, type=Path)
    parser.add_argument(
        '--trusted-hash',
        default=None,
        help='外部CI保護変数から渡すsha256:<64hex>（任意だがRelease運用では推奨）',
    )
    args = parser.parse_args()
    if args.target.is_symlink() or not args.target.is_file():
        print(f'verifier target missing or symlink: {args.target}', file=sys.stderr)
        return 4
    if args.expected.is_symlink() or not args.expected.is_file():
        print(f'integrity reference missing or symlink: {args.expected}', file=sys.stderr)
        return 4
    match = HASH_LINE.fullmatch(args.expected.read_text(encoding='utf-8').strip())
    if not match:
        print('integrity reference must be: sha256:<64hex>  <relative-file>', file=sys.stderr)
        return 4
    expected_hash = match.group(0).split()[0]
    actual = sha256(args.target)
    if args.trusted_hash is not None and args.trusted_hash != expected_hash:
        print('EXTERNAL_VERIFIER_TRUST_ANCHOR_MISMATCH', file=sys.stderr)
        return 3
    if actual != expected_hash:
        print(f'VERIFIER_SOURCE_TAMPERED expected={expected_hash} actual={actual}',
              file=sys.stderr)
        return 3
    expected_name = Path(match.group(1)).name
    if expected_name != args.target.name:
        print(f'integrity reference target mismatch: {match.group(1)}', file=sys.stderr)
        return 4
    print(f'verifier integrity ok: {actual}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
