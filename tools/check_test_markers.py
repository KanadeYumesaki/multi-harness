#!/usr/bin/env python3
"""pytest.skip/skipif/xfailと同名markerをASTで拒否する。"""
from __future__ import annotations

import argparse
import ast
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    findings = []
    for path in sorted(args.root.rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or node.attr not in {'skip', 'skipif', 'xfail'}:
                continue
            owner = node.value
            if isinstance(owner, ast.Name) and owner.id in {'pytest', 'mark'}:
                findings.append(f'{path}:{node.lineno}: {owner.id}.{node.attr}')
            elif isinstance(owner, ast.Attribute) and owner.attr == 'mark':
                findings.append(f'{path}:{node.lineno}: pytest.mark.{node.attr}')
    for finding in findings:
        print(finding)
    print(f'forbidden skip/xfail markers: {len(findings)}')
    return 1 if findings else 0


if __name__ == '__main__':
    raise SystemExit(main())
