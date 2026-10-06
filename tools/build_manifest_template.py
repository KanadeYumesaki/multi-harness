#!/usr/bin/env python3
"""Registry SnapshotからRelease Scope別のManifest Templateを生成する。

v1.6はTemplateが全86 Case固定の手作りJSONだったため、Scope概念を持てず、
Registryとの同期も人手だった。v1.7はScopeを指定して生成する。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--registry', required=True, type=Path)
    ap.add_argument('--release-scope', required=True)
    ap.add_argument('--out', required=True, type=Path)
    args = ap.parse_args()

    snap = json.loads(args.registry.read_text(encoding='utf-8'))
    if args.release_scope not in snap['release_enabled_phases']:
        raise SystemExit(
            f'{args.release_scope} is not release-enabled. '
            f'choose from {snap["release_enabled_phases"]}; phases without gates are blocked'
        )
    scope = snap['scopes'][args.release_scope]

    # 領域定義Catalog。どのScopeで必須かは scope 側が持つ（Snapshot 1.4）。
    area_spec = {a['area']: a for a in snap['evidence_areas']}
    unknown = sorted(set(scope['required_evidence_areas']) - set(area_spec))
    if unknown:
        raise SystemExit(f'registry snapshot: undefined evidence areas {unknown}')

    manifest = {
        '_comment': 'Template。実行結果から試験Harnessが更新する。人手でHashを転記しない。',
        'manifest_version': '1.2',
        'design_version': snap['design_version'],
        'design_sha256': snap['design_sha256'],
        'release_scope': args.release_scope,
        'registry_snapshot_hash': snap['registry_snapshot_hash'],
        'implementation_repository': None,
        'implementation_commit_sha': None,
        'source_tree_clean': None,
        # Scopeが要求する領域の束縛Subtreeだけを置く。要求しない領域の
        # Subtree枠を残すと、MVP0-Aのテンプレに承認UIの欄が現れる。
        'source_subtree_hashes': {
            area_spec[name]['reuse_bound_subtree']: None
            for name in scope['required_evidence_areas']
            if area_spec[name].get('reuse_bound_subtree')
        },
        'runtime_environment': {
            'environment_manifest_path': 'environment/runtime-environment.json',
            'environment_manifest_hash': None,
            'is_wsl2': None,
            'workspace_on_linux_native_fs': None,
            'python_version': None,
        },
        'schema_set_hash': None,
        'migration_head': None,
        'test_manifest_hash': scope['test_manifest_hash'],
        'expected_gate_count': scope['required_gate_count'],
        'expected_test_id_count': scope['required_test_id_count'],
        'expected_case_count': scope['required_case_count'],
        'test_cases': [
            {
                'test_id': test_id,
                'case_id': case_id,
                'expectation_descriptor_hash':
                    snap['expectations'][f'{test_id}/{case_id}'][
                        'expectation_descriptor_hash'],
                'input_fixture_hash': None,
                'status': 'UNVERIFIED',
                'evidence_manifest_hash': None,
                'evidence_path': f'tests/cases/{test_id}/{case_id}/evidence.json',
            }
            for test_id, case_id in scope['required_cases']
        ],
        'gates': [
            {
                'gate_id': gid,
                'condition': snap['gates'][gid]['condition'],
                'test_refs_mode': snap['gates'][gid]['test_refs_mode'],
                'test_refs': snap['gates'][gid]['test_refs'],
                'status': 'UNVERIFIED',
                'evidence_manifest_hash': None,
                'evidence_path': f'gates/{gid}.json',
            }
            for gid in scope['required_gate_ids']
        ],
        'required_evidence_areas': [
            {
                'area': name,
                'status': 'UNVERIFIED',
                'evidence_manifest_hash': None,
                'evidence_path': area_spec[name]['evidence_path'],
            }
            for name in scope['required_evidence_areas']
        ],
        'skipped_count': 0,
        'xfail_count': 0,
        'release_decision': 'BLOCKED_EVIDENCE_MISSING',
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'wrote {args.out}')
    print(f'  scope={args.release_scope} cases={scope["required_case_count"]} '
          f'gates={scope["required_gate_count"]} '
          f'areas={len(scope["required_evidence_areas"])}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
