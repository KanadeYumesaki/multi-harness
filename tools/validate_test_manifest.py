#!/usr/bin/env python3
"""Test Manifest Validator（設計書 v1.7 §16.1 Step 1、§19.1）。

Registry上のTest Manifestが§19.1の契約を満たすかを検査する。
Runtime GO Verifierの前段であり、Evidenceの有無に関係なく実行できる。

`AT-TEST-MANIFEST-001`／`AT-MANIFEST-SUBJECT-001`が期待する拒否条件は、
本Validatorが実装する検査そのものである。

終了Code: 0=合格 / 1=違反あり / 4=入力不正
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

HASH_RE = re.compile(r'^sha256:[0-9a-f]{64}$')
EVIDENCE_STATUS = {'UNVERIFIED', 'PASS', 'FAIL', 'BLOCKED'}

REQUIRED_FIELDS = [
    'test_id', 'case_id', 'scenario', 'expectation_descriptor_hash',
    'input_fixture_hash', 'expected_event_sequence', 'expected_subject_type',
    'expected_subject_id', 'expected_state', 'expected_error_code',
    'trace_scope', 'assertions', 'auto_reexecution_prohibited',
    'release_allowed', 'manual_queue_expected', 'fault_point',
    'phase_scope', 'durability_tier', 'evidence_status', 'evidence_manifest_hash',
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--registries', required=True, type=Path)
    ap.add_argument('--emit-evidence', type=Path)
    ap.add_argument('--release-scope', default='MVP0-A')
    args = ap.parse_args()

    try:
        cases = yaml.safe_load(
            (args.registries / 'tests.yaml').read_text(encoding='utf-8'))['test_cases']
        errors_reg = yaml.safe_load(
            (args.registries / 'errors.yaml').read_text(encoding='utf-8'))['error_codes']
    except Exception as exc:
        print(f'registry load failed: {exc}', file=sys.stderr)
        return 4

    classification = {e['error_code']: e['classification'] for e in errors_reg}
    violations: list[tuple[str, str]] = []

    def bad(code: str, msg: str) -> None:
        violations.append((code, msg))

    keys = [(c.get('test_id'), c.get('case_id')) for c in cases]
    for key, n in Counter(keys).items():
        if n > 1:
            bad('DUPLICATE_CASE', f'{key[0]}/{key[1]} x{n}')

    for c in cases:
        key = f'{c.get("test_id")}/{c.get("case_id")}'

        for field in REQUIRED_FIELDS:
            if field not in c:
                bad('HASH_COLUMN_MISSING' if field.endswith('_hash') else 'FIELD_MISSING',
                    f'{key}.{field}')

        h = c.get('expectation_descriptor_hash')
        if not isinstance(h, str) or not HASH_RE.fullmatch(h):
            bad('HASH_PATTERN_INVALID', f'{key}.expectation_descriptor_hash={h!r}')

        fixture = c.get('input_fixture_hash')
        if fixture is not None and (
                not isinstance(fixture, str) or not HASH_RE.fullmatch(fixture)):
            bad('FIXTURE_HASH_FORMAT_INVALID', f'{key}.input_fixture_hash={fixture!r}')

        status = c.get('evidence_status')
        if status not in EVIDENCE_STATUS:
            bad('EVIDENCE_STATUS_INVALID', f'{key}: {status!r}')
        if status == 'PASS':
            # §19.1: evidence_status=PASS時は実Fixture HashとEvidence Hashが必須
            if fixture is None:
                bad('FIXTURE_HASH_REQUIRED_FOR_PASS', key)
            if not isinstance(c.get('evidence_manifest_hash'), str):
                bad('EVIDENCE_HASH_REQUIRED_FOR_PASS', key)

        state = c.get('expected_state')
        if not isinstance(state, str) or ',' in state or '/' in state:
            # 1行1決定結果。複数期待状態を1 Caseへ詰めない
            bad('AMBIGUOUS_EXPECTED_STATE', f'{key}: {state!r}')

        code = c.get('expected_error_code')
        if code is not None:
            if code not in classification:
                bad('UNKNOWN_ERROR_CODE', f'{key}: {code}')

        seq = c.get('expected_event_sequence')
        if not isinstance(seq, list):
            bad('EVENT_SEQUENCE_INVALID', f'{key}: {seq!r}')

        for flag in ('auto_reexecution_prohibited', 'release_allowed',
                     'manual_queue_expected'):
            if not isinstance(c.get(flag), bool):
                bad('FLAG_NOT_BOOLEAN', f'{key}.{flag}={c.get(flag)!r}')

        # 自動再実行禁止のCaseがReleaseを許可するのは矛盾
        if c.get('auto_reexecution_prohibited') and c.get('manual_queue_expected'):
            if c.get('release_allowed'):
                bad('RELEASE_ALLOWED_WITH_MANUAL_QUEUE', key)

        scope = c.get('phase_scope')
        if not isinstance(scope, list) or not scope:
            bad('PHASE_SCOPE_MISSING', key)

        durability = c.get('durability_tier')
        durability_required = str(c.get('test_id', '')).startswith(
            ('AT-CRASH-', 'AT-FAULT-IO-'))
        if durability_required and durability not in {'T1_PROCESS_KILL', 'T2_CACHE_DROP'}:
            bad('DURABILITY_TIER_REQUIRED', f'{key}: {durability!r}')
        if not durability_required and durability is not None:
            bad('DURABILITY_TIER_NOT_APPLICABLE', f'{key}: {durability!r}')

    grouped: dict[str, list[str]] = {}
    for code, msg in violations:
        grouped.setdefault(code, []).append(msg)

    in_scope = [c for c in cases if args.release_scope in (c.get('phase_scope') or [])]
    print(f'cases      : {len(cases)} total / {len(in_scope)} in {args.release_scope}')
    print(f'violations : {len(violations)}')
    for code in sorted(grouped):
        print(f'  [{code}] x{len(grouped[code])}')
        for msg in grouped[code][:5]:
            print(f'      {msg}')

    if args.emit_evidence:
        payload = {
            'evidence_schema_version': '1.1',
            'release_scope': args.release_scope,
            'area': 'manifest_validation',
            'status': 'PASS' if not violations else 'FAIL',
            'summary': {
                'total_cases': len(cases),
                'in_scope_cases': len(in_scope),
                'violation_count': len(violations),
                'violations_by_code': {k: len(v) for k, v in grouped.items()},
            },
            'test_run_id': 'manifest-validator',
            'recorded_at': dt.datetime.now(dt.timezone.utc).replace(
                microsecond=0).isoformat().replace('+00:00', 'Z'),
        }
        args.emit_evidence.parent.mkdir(parents=True, exist_ok=True)
        args.emit_evidence.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f'wrote {args.emit_evidence}')

    return 0 if not violations else 1


if __name__ == '__main__':
    raise SystemExit(main())
