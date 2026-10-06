#!/usr/bin/env python3
"""v1.6設計書本文からRegistry YAMLを一次抽出する片方向ツール。

本ツールはv1.6→v1.7移行時に一度だけ実行する。以後の正本は
design-source/registries/*.yaml であり、本文からの再抽出は行わない。
再実行は移行の再現検証にのみ使用する。
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml

MANIFEST_ROW = re.compile(r'^\| `AT-[A-Z0-9\-]+` \| `')
GATE_ROW = re.compile(r'^\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$')

COLUMNS = [
    'test_id', 'case_id', 'scenario', 'expectation_descriptor_hash',
    'input_fixture_hash', 'expected_event_sequence', 'expected_subject_type',
    'expected_subject_id', 'expected_state', 'expected_error_code',
    'trace_scope', 'assertions', 'auto_reexecution_prohibited',
    'release_allowed', 'manual_queue_expected', 'fault_point',
    'evidence_status', 'evidence_manifest_hash',
]

IMPLEMENTED_PHASES = ['MVP0-A', 'MVP0-B', 'MVP1-A', 'MVP0-C', 'MVP1-D']

# §19.2 Phase Gate割当を機械可読へ展開したもの。
# 「MVP0-A以降」「全Phase」は実装対象5フェーズへ展開する。
PHASE_ASSIGNMENT = {
    'AT-PLAN-001': ['MVP0-A', 'MVP0-B', 'MVP0-C'],
    'AT-APPROVAL-001': IMPLEMENTED_PHASES,
    'AT-APPROVAL-002': IMPLEMENTED_PHASES,
    'AT-CLOCK-SKEW-001': IMPLEMENTED_PHASES,
    'AT-CRASH-001': ['MVP0-A', 'MVP1-A', 'MVP1-D', 'MVP1-E'],
    'AT-FAULT-IO-001': ['MVP0-A', 'MVP1-A', 'MVP1-D', 'MVP1-E'],
    'AT-FAULT-GUARD-001': ['MVP0-A', 'MVP1-A', 'MVP1-D', 'MVP1-E'],
    'AT-PATH-001': ['MVP0-A'],
    'AT-INPUT-PATH-001': ['MVP0-A'],
    'AT-WSL-BOUNDARY-001': ['MVP0-A'],
    'AT-SANDBOX-001': ['MVP0-B', 'MVP1-A'],
    # v1.7訂正: Mock Adapterも RuntimeEnvelopeSpec / Runtime Attestation を持つため
    # Config Driftは MVP0-A から検証可能。§3.13 Gate#4 の参照整合を解消する。
    'AT-CONFIG-001': ['MVP0-A', 'MVP0-B', 'MVP0-C'],
    'AT-AUTH-001': ['MVP0-C', 'MVP1-B', 'MVP1-C', 'MVP1-D', 'MVP1-E'],
    'AT-FALLBACK-001': ['MVP1-C'],
    'AT-REMOTE-PREP-001': ['MVP0-C'],
    'AT-SCHEMA-CONDITIONAL-001': IMPLEMENTED_PHASES,
    'AT-SCHEMA-COMPLETE-001': IMPLEMENTED_PHASES,
    'AT-EVENT-ORDER-001': IMPLEMENTED_PHASES,
    'AT-LEDGER-TAMPER-001': IMPLEMENTED_PHASES,
    'AT-POLICY-APPROVAL-001': ['MVP1-A', 'MVP1-D', 'MVP1-E', 'ENTERPRISE'],
    'AT-OUTBOX-001': ['MVP1-E'],
    'AT-BUDGET-001': ['MVP1-D'],
    'AT-BLIND-001': ['BLIND_REVIEWER'],
    'AT-FENCE-001': ['MVP0-A', 'MVP2-B'],
    'AT-TEST-MANIFEST-001': IMPLEMENTED_PHASES,
    'AT-MANIFEST-SUBJECT-001': IMPLEMENTED_PHASES,
    'AT-PLAN-DETERMINISM-001': IMPLEMENTED_PHASES,
    'AT-CONTROL-DATA-001': IMPLEMENTED_PHASES,
    'AT-POLICY-STALE-001': ['MVP0-A', 'MVP0-C', 'MVP1-A', 'MVP1-D', 'MVP1-E', 'ENTERPRISE'],
    'AT-EMERGENCY-RECOVERY-001': ['MVP0-A', 'MVP0-C', 'MVP1-A', 'MVP1-D', 'MVP1-E', 'ENTERPRISE'],
    'AT-EVENT-MAPPING-001': ['MVP0-C', 'MVP1-D', 'MVP1-E'],
    'AT-RUN-TERMINAL-001': IMPLEMENTED_PHASES,
    'AT-APPROVAL-UX-001': IMPLEMENTED_PHASES,
    'AT-PERF-001': IMPLEMENTED_PHASES,
    'AT-MIGRATION-001': IMPLEMENTED_PHASES,
    'AT-DRAIN-001': IMPLEMENTED_PHASES,
    'AT-GC-001': IMPLEMENTED_PHASES,
}

# Case単位のScope上書き。Test IDが複数Phaseの関心事を束ねている場合に使う。
# v1.7で新設。Gate参照整合の解消根拠を rationale へ必ず残す。
CASE_PHASE_OVERRIDE = {
    ('AT-EVENT-MAPPING-001', 'LEDGER_EVENT_MISSING'): {
        'phase_scope': IMPLEMENTED_PHASES,
        'rationale': 'Phase Store先行／Ledger Event欠落はLocal File Commitでも発生する'
                     '汎用不整合であり、MVP0-Aの§3.13 Gate#13が要求する。',
    },
    ('AT-EVENT-MAPPING-001', 'REMOTE_UNCERTAIN'): {
        'phase_scope': ['MVP0-C', 'MVP1-D', 'MVP1-E'],
        'rationale': 'Remote Invocation Registryが存在するPhaseに限定。',
    },
    ('AT-EVENT-MAPPING-001', 'BUDGET_UNKNOWN'): {
        'phase_scope': ['MVP1-D'],
        'rationale': 'Budget Reservation Storeが存在するPhaseに限定。',
    },
    ('AT-EVENT-MAPPING-001', 'OUTBOX_UNKNOWN'): {
        'phase_scope': ['MVP1-E'],
        'rationale': 'Transactional Outboxが存在するPhaseに限定。参照仕様。',
    },
}


def split_row(line: str) -> list[str]:
    cells = line.split(' | ')
    cells[0] = cells[0].lstrip('| ')
    cells[-1] = cells[-1].rstrip(' |')
    return [c.strip().strip('`') for c in cells]


def norm(value: str):
    if value == 'null':
        return None
    if value == 'true':
        return True
    if value == 'false':
        return False
    return value


def extract_tests(lines: list[str]) -> list[dict]:
    out = []
    for line in lines:
        if not MANIFEST_ROW.match(line):
            continue
        cells = split_row(line)
        if len(cells) != len(COLUMNS):
            raise SystemExit(f'column count mismatch ({len(cells)}): {line[:80]}')
        row = {k: norm(v) for k, v in zip(COLUMNS, cells)}
        seq = row['expected_event_sequence']
        row['expected_event_sequence'] = (
            [] if seq == 'NONE' else [s.strip() for s in seq.split('→')]
        )
        row['assertions'] = [a.strip() for a in row['assertions'].split(';')]
        key = (row['test_id'], row['case_id'])
        override = CASE_PHASE_OVERRIDE.get(key)
        if override:
            row['phase_scope'] = list(override['phase_scope'])
            row['phase_scope_rationale'] = override['rationale']
        else:
            row['phase_scope'] = list(PHASE_ASSIGNMENT[row['test_id']])
        # evidence_status/hashは実行時に決まる。Registryは常にUNVERIFIEDで固定する。
        row['evidence_status'] = 'UNVERIFIED'
        row['evidence_manifest_hash'] = None
        out.append(row)
    return out


def extract_gates(lines: list[str]) -> list[dict]:
    start = lines.index('## 3.13 受入Gate')
    end = lines.index('## 3.14 成果物')
    out = []
    for line in lines[start:end]:
        m = GATE_ROW.match(line)
        if not m or not m.group(1).isdigit():
            continue
        num = int(m.group(1))
        refs = sorted(set(re.findall(r'AT-[A-Z0-9\-]+', m.group(3))))
        # v1.7訂正: Gate 2は「Scope内Manifest全体」を条件とし、個別Test IDを
        # 参照しない。v1.6では機械可読な参照が空のまま放置されていたため、
        # Gateの根拠Caseを機械検証できなかった。モードとして明示する。
        out.append({
            'gate_id': f'MVP0A-GATE-{num:02d}',
            'phase': 'MVP0-A',
            'condition': m.group(2).strip(),
            'test_refs_mode': 'EXPLICIT' if refs else 'ALL_IN_SCOPE',
            'test_refs': refs,
            'test_refs_raw': m.group(3).strip().strip('`'),
        })
    return out


def extract_errors(lines: list[str]) -> list[dict]:
    start = lines.index('### 1.7.1 Error Code Registry')
    out = []
    for line in lines[start:start + 40]:
        m = re.match(r'^\| `([A-Z_]+)` \| (.+) \|$', line)
        if not m:
            continue
        codes = re.findall(r'`([A-Z0-9_]+)`', m.group(2))
        for code in codes:
            out.append({'error_code': code, 'classification': m.group(1)})
    return out


def extract_events(lines: list[str]) -> list[str]:
    start = lines.index('## 1.8 共通監査イベント')
    end = lines.index('## 1.9 共通メトリクス')
    return [
        m.group(1)
        for m in (re.match(r'^\* `([A-Z_]+)`$', line) for line in lines[start:end])
        if m
    ]


def extract_schemas(lines: list[str]) -> list[dict]:
    start = lines.index('## 15.9 Core Schema Catalog v1')
    end = lines.index('### Schema Delivery Gate')
    out = []
    for line in lines[start:end]:
        m = re.match(r'^### (\d+)\. ([A-Za-z]+)$', line)
        if m:
            out.append({
                'ordinal': int(m.group(1)),
                'schema_name': m.group(2),
                'schema_version': '1.0.0',
                'path': f'schemas/core/{m.group(2)}/1.0.0.schema.json',
            })
    return out


def dump(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='\n') as f:
        f.write('# 自動抽出結果。以後の編集は本ファイルを正本として直接行う。\n')
        yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False, width=4096)
    print(f'wrote {path}')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--design', required=True, type=Path)
    ap.add_argument('--out', required=True, type=Path)
    args = ap.parse_args()

    lines = args.design.read_text(encoding='utf-8').split('\n')
    tests = extract_tests(lines)
    gates = extract_gates(lines)

    dump(args.out / 'tests.yaml', {'test_cases': tests})
    dump(args.out / 'gates.yaml', {'gates': gates})
    dump(args.out / 'errors.yaml', {'error_codes': extract_errors(lines)})
    dump(args.out / 'events.yaml', {'event_types': extract_events(lines)})
    dump(args.out / 'schemas.yaml', {'core_schemas': extract_schemas(lines)})

    ids = {t['test_id'] for t in tests}
    print(f'\ntests: {len(tests)} cases / {len(ids)} ids, gates: {len(gates)}')
    for phase in IMPLEMENTED_PHASES:
        sel = [t for t in tests if phase in t['phase_scope']]
        print(f'  {phase}: {len(sel)} cases / {len({t["test_id"] for t in sel})} ids')

    print('\n--- MVP0-A Gateが参照するTestでMVP0-A scope外のもの ---')
    in_scope = {t['test_id'] for t in tests if 'MVP0-A' in t['phase_scope']}
    bad = False
    for g in gates:
        missing = [r for r in g['test_refs'] if r not in in_scope]
        if missing:
            bad = True
            print(f'  {g["gate_id"]}: {missing}')
    if not bad:
        print('  なし')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
