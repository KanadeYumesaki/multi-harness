#!/usr/bin/env python3
"""実装Sourceの必須禁止パターンをASTと文字列で検査する。"""
from __future__ import annotations

import argparse
import ast
from pathlib import Path

BANNED_FLAGS = {'--yes', '--auto-approve', '--force', '--skip-approval'}


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f'{dotted(node.value)}.{node.attr}'
    return ''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    findings: list[str] = []
    if not args.root.exists():
        print(f'source root not present yet: {args.root} (Spec Foundation mode)')
        return 0
    for path in sorted(args.root.rglob('*.py')):
        source = path.read_text(encoding='utf-8')
        for flag in BANNED_FLAGS:
            if flag in source:
                findings.append(f'{path}: approval bypass flag {flag}')
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = dotted(node.func)
            if any(kw.arg == 'shell' and isinstance(kw.value, ast.Constant)
                   and kw.value.value is True for kw in node.keywords):
                findings.append(f'{path}:{node.lineno}: shell=True')
            if name == 'sqlite3.connect' and path.name != 'connection_factory.py':
                findings.append(f'{path}:{node.lineno}: sqlite3.connect outside factory')
            if name.startswith('subprocess.') and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    findings.append(f'{path}:{node.lineno}: process command must be list[str]')
    for finding in findings:
        print(finding)
    print(f'forbidden patterns: {len(findings)}')
    return 1 if findings else 0


if __name__ == '__main__':
    raise SystemExit(main())
