#!/usr/bin/env python3
"""Verifierを意図的に更新したときの期待Hash更新。"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', required=True, type=Path)
    parser.add_argument('--expected', required=True, type=Path)
    args = parser.parse_args()
    digest = hashlib.sha256(args.target.read_bytes()).hexdigest()
    args.expected.write_text(
        f'sha256:{digest}  {args.target.name}\n', encoding='utf-8')
    print(f'updated integrity reference: sha256:{digest}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
