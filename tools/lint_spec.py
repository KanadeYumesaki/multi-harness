#!/usr/bin/env python3
"""Spec Linter（設計書 v1.7 §16.1 Step 0）。

設計書本文とRegistryの参照整合・件数整合・Scope整合を機械検査する。
実装着手前およびAI生成の前後で必ず実行する。件数を本文へ手入力すると
本Linterが検出する。

終了Code: 0=合格 / 1=違反あり / 4=入力不正
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

# `schemas.yaml` の正規化は4 Tool共有の `tools/schema_catalog.py` が正本である
# （Step 3-b でTool内の複製を削除した）。Script実行時は sys.path[0] が tools/ に
# なるが、Testが importlib で読み込む経路では解決されないため明示的に足す。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from schema_catalog import (  # noqa: E402
    active_write_versions,
    logical_schema_count,
    normalize_core_schemas,
)

MANIFEST_ROW = re.compile(r'^\| `AT-[A-Z0-9\-]+` \| `')


class Lint:
    def __init__(self) -> None:
        self.violations: list[tuple[str, str]] = []

    def add(self, code: str, message: str) -> None:
        self.violations.append((code, message))

    def check(self, ok: bool, code: str, message: str) -> None:
        if not ok:
            self.add(code, message)

    @property
    def ok(self) -> bool:
        return not self.violations


def sha256_file(path: Path) -> str:
    return 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()


def load_state_namespaces(lines: list[str]) -> dict[str, set[str]]:
    """§19.1のSubject型／State名前空間表を読む。"""
    try:
        start = lines.index('### Subject型とState名前空間')
    except ValueError:
        return {}
    out: dict[str, set[str]] = {}
    for line in lines[start:start + 40]:
        m = re.match(r'^\| `([^|]+?)` \| `([^|]+?)` \|$', line)
        if not m:
            continue
        subjects = [s.strip().strip('`') for s in m.group(1).split('／')]
        states = {s.strip() for s in m.group(2).split(',')}
        for subject in subjects:
            out[subject] = states
    return out


def load_error_code_table(lines: list[str]) -> dict[str, str]:
    """§1.7.1 Error Code Registry表を読む。

    戻り値は `error_code -> classification`。表の1行は
    `| \\`VALIDATION_ERROR\\` | \\`CODE_A\\`, \\`CODE_B\\` |` の形式である。

    設計書§1.7.1と`errors.yaml`はどちらも「Error Codeの正本」を名乗っており、
    突き合わせる検査が無かった（Decision B §0.2）。本関数はその照合の入力を作る。
    """
    try:
        start = lines.index('### 1.7.1 Error Code Registry')
    except ValueError:
        return {}
    table: dict[str, str] = {}
    for line in lines[start:start + 40]:
        if line.startswith('## '):
            break
        m = re.match(r'^\| `([A-Z_]+)` \| (.+) \|$', line)
        if not m or m.group(1) == 'Classification':
            continue
        for code in re.findall(r'`([A-Z_]+)`', m.group(2)):
            table[code] = m.group(1)
    return table


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--design', required=True, type=Path)
    ap.add_argument('--registries', required=True, type=Path)
    ap.add_argument('--snapshot', type=Path)
    ap.add_argument('--spec-dir', type=Path)
    ap.add_argument('--emit-evidence', type=Path)
    ap.add_argument('--release-scope', default='MVP0-A')
    args = ap.parse_args()

    if not args.design.is_file():
        print('design not found', file=sys.stderr)
        return 4

    lint = Lint()
    text = args.design.read_text(encoding='utf-8')
    lines = text.split('\n')

    def load(name):
        return yaml.safe_load((args.registries / name).read_text(encoding='utf-8'))

    cases = load('tests.yaml')['test_cases']
    gates = load('gates.yaml')['gates']
    errors = load('errors.yaml')['error_codes']
    events = set(load('events.yaml')['event_types'])
    schemas = load('schemas.yaml')['core_schemas']
    states = load('states.yaml')['state_namespaces']

    # ---- ID一意性 ------------------------------------------------------
    case_keys = [(c['test_id'], c['case_id']) for c in cases]
    for key, n in Counter(case_keys).items():
        lint.check(n == 1, 'DUPLICATE_CASE_ID', f'{key[0]}/{key[1]} appears {n} times')
    for gid, n in Counter(g['gate_id'] for g in gates).items():
        lint.check(n == 1, 'DUPLICATE_GATE_ID', f'{gid} appears {n} times')
    for code, n in Counter(e['error_code'] for e in errors).items():
        lint.check(n == 1, 'DUPLICATE_ERROR_CODE', f'{code} appears {n} times')
    # 同一 schema_name の異なるVersionは許可する（Owner Decision D-06）。
    # 拒否するのは (schema_name, schema_version) の重複だけである。
    # 以前は schema_name 単独一意だったため、2.0.0 を登録した時点で
    # DUPLICATE_SCHEMA_NAME が立ち、複数Version化そのものができなかった。
    try:
        schema_entries = normalize_core_schemas(schemas)
    except ValueError as exc:
        schema_entries = []
        lint.add('DUPLICATE_SCHEMA_VERSION', str(exc))
    for key, n in Counter(
            (e['schema_name'], e['schema_version']) for e in schema_entries).items():
        lint.check(
            n == 1, 'DUPLICATE_SCHEMA_VERSION', f'{key[0]}@{key[1]} appears {n} times')
    # 論理Schemaごとに書込み可能Versionがちょうど1つあること（Owner Decision D-06 条件3）。
    # 「どの版へ書くのか」が0個でも2個でも、複数Version化した瞬間に書込み先が決まらない。
    try:
        active_versions = active_write_versions(schema_entries)
    except ValueError as exc:
        active_versions = {}
        lint.add('SCHEMA_ACTIVE_WRITE_INVALID', str(exc))
    # 登録Pathの実在。Fileが無い版をCatalogへ載せない（Fail-Closed）。
    for entry in schema_entries:
        path = args.registries.parent.parent / entry['path']
        lint.check(
            path.is_file(), 'SCHEMA_PATH_MISSING',
            f'{entry["schema_name"]}@{entry["schema_version"]} -> {entry["path"]}')

    # ---- 設計書§1.7.1 Error Code表 と errors.yaml の照合（Owner Decision B-1b 4点目） --
    # 正本を名乗るものが2つあるのに突き合わせが無い、という欠陥を塞ぐ。
    # Decision D（Schema本文とJSON Schemaの乖離）と同じ構造の欠陥である。
    design_error_table = load_error_code_table(lines)
    lint.check(bool(design_error_table), 'ERROR_CODE_TABLE_MISSING', '§1.7.1')
    registry_classification = {e['error_code']: e['classification'] for e in errors}
    for code in sorted(set(design_error_table) - set(registry_classification)):
        lint.add('ERROR_CODE_IN_DESIGN_NOT_IN_REGISTRY', code)
    for code in sorted(set(registry_classification) - set(design_error_table)):
        lint.add('ERROR_CODE_IN_REGISTRY_NOT_IN_DESIGN', code)
    for code in sorted(set(design_error_table) & set(registry_classification)):
        lint.check(
            design_error_table[code] == registry_classification[code],
            'ERROR_CODE_CLASSIFICATION_MISMATCH',
            f'{code}: design={design_error_table[code]} '
            f'registry={registry_classification[code]}')

    # ---- 本文とRegistryの件数一致 --------------------------------------
    doc_rows = [line for line in lines if MANIFEST_ROW.match(line)]
    lint.check(
        len(doc_rows) == len(cases), 'MANIFEST_COUNT_MISMATCH',
        f'design has {len(doc_rows)} manifest rows, registry has {len(cases)} cases')

    doc_keys = {
        (m.group(1), m.group(2))
        for m in (re.match(r'^\| `([^`]+)` \| `([^`]+)`', r) for r in doc_rows) if m
    }
    for key in sorted(doc_keys - set(case_keys)):
        lint.add('CASE_IN_DESIGN_NOT_IN_REGISTRY', f'{key[0]}/{key[1]}')
    for key in sorted(set(case_keys) - doc_keys):
        lint.add('CASE_IN_REGISTRY_NOT_IN_DESIGN', f'{key[0]}/{key[1]}')

    # ---- 本文表のExpectation Descriptor HashがRegistryと一致すること -------
    #
    # 件数とCase集合だけを見ていたため、Hash列だけが古いまま残っても通っていた。
    # 実際、DESCRIPTOR_FIELDSへFieldを足したときに全127行が黙ってずれた。
    # Hashは導出値なので、Registryが動けば表も動く。**片方だけ古い状態を許さない。**
    by_key = {(c['test_id'], c['case_id']): c for c in cases}
    for row in doc_rows:
        cells = [cell.strip() for cell in row.strip().strip('|').split('|')]
        if len(cells) < 4:
            continue
        key = (cells[0].strip('`'), cells[1].strip('`'))
        case = by_key.get(key)
        if case is None:
            continue
        want = f"`{case['expectation_descriptor_hash']}`"
        if cells[3] != want:
            lint.add(
                'MANIFEST_EXPECTATION_HASH_MISMATCH',
                f'{key[0]}/{key[1]}: design={cells[3]} registry={want}')

    # ---- Gate → Test 参照整合 ------------------------------------------
    known_tests = {c['test_id'] for c in cases}
    for g in gates:
        for ref in g['test_refs']:
            lint.check(ref in known_tests, 'GATE_REFERENCES_UNKNOWN_TEST',
                       f'{g["gate_id"]} -> {ref}')
        lint.check(
            g['test_refs_mode'] in ('EXPLICIT', 'ALL_IN_SCOPE'),
            'GATE_TEST_REFS_MODE_INVALID', f'{g["gate_id"]}: {g["test_refs_mode"]}')
        lint.check(
            g['test_refs_mode'] == 'ALL_IN_SCOPE' or bool(g['test_refs']),
            'GATE_WITHOUT_TEST_REFS', g['gate_id'])

    # ---- Gate → Scope整合（v1.7新設。v1.6のGate 4/13が該当した） --------
    scope_tests = {c['test_id'] for c in cases if args.release_scope in c['phase_scope']}
    for g in gates:
        if g['phase'] != args.release_scope:
            continue
        for ref in g['test_refs']:
            lint.check(
                ref in scope_tests, 'GATE_REFERENCES_OUT_OF_SCOPE_TEST',
                f'{g["gate_id"]} -> {ref} is not in {args.release_scope} scope')

    # ---- phase_scope の妥当性 ------------------------------------------
    known_phases = {
        'MVP0-A', 'MVP0-B', 'MVP0-C', 'MVP1-A', 'MVP1-B', 'MVP1-C', 'MVP1-D',
        'MVP1-E', 'MVP2-A', 'MVP2-B', 'BLIND_REVIEWER', 'ENTERPRISE',
    }
    for c in cases:
        scope = c.get('phase_scope')
        key = f'{c["test_id"]}/{c["case_id"]}'
        lint.check(bool(scope), 'PHASE_SCOPE_MISSING', key)
        for phase in scope or []:
            lint.check(phase in known_phases, 'PHASE_SCOPE_UNKNOWN', f'{key}: {phase}')

    # ---- Subject型 / State名前空間 -------------------------------------
    namespaces = load_state_namespaces(lines)
    lint.check(bool(namespaces), 'STATE_NAMESPACE_TABLE_MISSING', '§19.1')
    lint.check(
        namespaces == {name: set(values) for name, values in states.items()},
        'STATE_REGISTRY_DESIGN_MISMATCH',
        'design §19.1 and design-source/registries/states.yaml differ',
    )
    for c in cases:
        subject = c['expected_subject_type']
        key = f'{c["test_id"]}/{c["case_id"]}'
        if subject not in namespaces:
            lint.add('EXPECTED_STATE_SUBJECT_MISMATCH', f'{key}: unknown subject {subject}')
        elif c['expected_state'] not in namespaces[subject]:
            lint.add(
                'EXPECTED_STATE_SUBJECT_MISMATCH',
                f'{key}: state {c["expected_state"]} not in {subject} namespace')

    # ---- Error Code / Classification -----------------------------------
    registry_codes = {e['error_code'] for e in errors}
    for c in cases:
        code = c['expected_error_code']
        if code is not None:
            lint.check(code in registry_codes, 'UNKNOWN_ERROR_CODE',
                       f'{c["test_id"]}/{c["case_id"]}: {code}')

    # ---- Expected Event Sequence が正規Event ---------------------------
    for c in cases:
        for ev in c['expected_event_sequence']:
            lint.check(ev in events, 'UNKNOWN_EVENT_TYPE',
                       f'{c["test_id"]}/{c["case_id"]}: {ev}')

    # ---- Hash形式 -------------------------------------------------------
    for c in cases:
        h = c['expectation_descriptor_hash']
        lint.check(
            isinstance(h, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', h),
            'HASH_PATTERN_INVALID', f'{c["test_id"]}/{c["case_id"]}: {h}')

    # ---- 版数ドリフト（v1.6でAppendix Cがv1.5を指していた） --------------
    for pattern, code in [
        (r'integrated-design-v1\.[0-6]\.md', 'STALE_DESIGN_FILENAME'),
        (r'harness-core/1\.[0-6]\.0', 'STALE_PRODUCER_VERSION'),
        (r'runtime-go-v1\.6-kit', 'STALE_KIT_REFERENCE'),
    ]:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line):
                lint.add(code, f'L{i}: {line.strip()[:100]}')

    # ---- Code Fence の均衡 ---------------------------------------------
    lint.check(text.count('\n```') % 2 == 0, 'UNBALANCED_CODE_FENCE',
               f'{text.count(chr(10) + "```")} fences')

    # ---- Code Fence内のH1（shard分割ツールの誤爆源） --------------------
    in_fence = False
    for i, line in enumerate(lines, 1):
        if line.startswith('```'):
            in_fence = not in_fence
        elif in_fence and re.match(r'^#{1,2} ', line):
            lint.add('HEADING_INSIDE_CODE_FENCE', f'L{i}: {line.strip()[:80]}')

    # ---- 参照仕様Phaseの誤解禁 -----------------------------------------
    for i, line in enumerate(lines, 1):
        if re.search(r'feature_flag.*(MVP1-[BCE]|MVP2-[AB]|BLIND).*(true|ON|有効)', line, re.I):
            lint.add('REFERENCE_PHASE_FLAG_ENABLED', f'L{i}: {line.strip()[:100]}')

    # ---- Registry Snapshot の鮮度 --------------------------------------
    if args.snapshot:
        if not args.snapshot.is_file():
            lint.add('SNAPSHOT_MISSING', str(args.snapshot))
        else:
            snap = json.loads(args.snapshot.read_text(encoding='utf-8'))
            actual = sha256_file(args.design)
            lint.check(snap.get('design_sha256') == actual, 'SNAPSHOT_DESIGN_STALE',
                       f'snapshot={snap.get("design_sha256")} actual={actual}')
            for name, declared in (snap.get('source_registry_hashes') or {}).items():
                path = args.registries / name
                if not path.is_file():
                    lint.add('SNAPSHOT_REGISTRY_MISSING', name)
                elif sha256_file(path) != declared:
                    lint.add('SNAPSHOT_REGISTRY_STALE',
                             f'{name}: rebuild registry-snapshot.json')
            scope = (snap.get('scopes') or {}).get(args.release_scope)
            if scope:
                expect = len([c for c in cases if args.release_scope in c['phase_scope']])
                lint.check(scope['required_case_count'] == expect,
                           'SNAPSHOT_SCOPE_COUNT_STALE',
                           f'{args.release_scope}: {scope["required_case_count"]} != {expect}')

    # ---- 生成spec shardの完全性 -----------------------------------------
    if args.spec_dir:
        manifest_path = args.spec_dir / 'spec-manifest.json'
        if not manifest_path.is_file():
            lint.add('SPEC_MANIFEST_MISSING', str(manifest_path))
        else:
            try:
                manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
                lint.check(manifest.get('source_hash') == sha256_file(args.design),
                           'SPEC_SOURCE_HASH_MISMATCH', str(manifest.get('source_hash')))
                required_shards = {
                    '00-common.md', '01-canonical-hash.md', '02-input-read.md',
                    '03-token-profile.md', '10-mvp0a.md', '11-mvp0b.md',
                    '12-mvp1a.md', '13-mvp0c.md', '14-mvp1d.md',
                    '30-policy-freshness.md', '90-tests.md',
                    '99-reference/reference-phases.md',
                }
                declared = {item.get('path') for item in manifest.get('files', [])}
                for rel in sorted(required_shards - declared):
                    lint.add('SPEC_REQUIRED_SHARD_MISSING', rel)
                for item in manifest.get('files', []):
                    rel = item.get('path')
                    path = args.spec_dir / str(rel)
                    if not path.is_file():
                        lint.add('SPEC_SHARD_MISSING', str(rel))
                    elif sha256_file(path) != item.get('sha256'):
                        lint.add('SPEC_SHARD_HASH_MISMATCH', str(rel))
                for name, expected in manifest.get('registry_hashes', {}).items():
                    path = args.registries / name
                    if not path.is_file() or sha256_file(path) != expected:
                        lint.add('SPEC_REGISTRY_HASH_MISMATCH', name)
            except Exception as exc:
                lint.add('SPEC_MANIFEST_INVALID', str(exc))

    # ---- 結果 -----------------------------------------------------------
    grouped: dict[str, list[str]] = {}
    for code, msg in lint.violations:
        grouped.setdefault(code, []).append(msg)

    print(f'design   : {args.design.name}')
    print(f'registry : {len(cases)} cases / {len(set(known_tests))} test ids / '
          f'{len(gates)} gates / {len(errors)} error codes / '
          f'{len(events)} events / {logical_schema_count(schema_entries)} schemas '
          f'({len(schema_entries)} versions, active_write={len(active_versions)})')
    print(f'scope    : {args.release_scope} -> '
          f'{len([c for c in cases if args.release_scope in c["phase_scope"]])} cases')
    print(f'violations: {len(lint.violations)}')
    for code in sorted(grouped):
        print(f'  [{code}] x{len(grouped[code])}')
        for msg in grouped[code][:5]:
            print(f'      {msg}')
        if len(grouped[code]) > 5:
            print(f'      ... +{len(grouped[code]) - 5} more')

    if args.emit_evidence:
        payload = {
            'evidence_schema_version': '1.1',
            'release_scope': args.release_scope,
            'area': 'spec_lint',
            'status': 'PASS' if lint.ok else 'FAIL',
            'summary': {
                'violation_count': len(lint.violations),
                'violations_by_code': {k: len(v) for k, v in grouped.items()},
                'design_sha256': sha256_file(args.design),
            },
            'test_run_id': 'spec-lint',
            'recorded_at': dt.datetime.now(dt.timezone.utc).replace(
                microsecond=0).isoformat().replace('+00:00', 'Z'),
        }
        args.emit_evidence.parent.mkdir(parents=True, exist_ok=True)
        args.emit_evidence.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f'wrote {args.emit_evidence}')

    return 0 if lint.ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
