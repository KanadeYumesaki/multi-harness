#!/usr/bin/env python3
"""Registry YAML（正本）から registry-snapshot.json（生成物）を作る。

Verifierは第三者ライブラリを持たない stdlib-only を維持する必要があるため、
YAMLを直接読ませずに本ツールがJSONへ落とす。生成物は source_hash と
generated_at を持ち、設計書§0.2の生成物規則へ従う。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import yaml

# `schemas.yaml` の正規化とCatalog Hashは4 Tool共有の `tools/schema_catalog.py` が
# 正本である（Step 3-b でTool内の複製を削除した）。Script実行時は sys.path[0] が
# tools/ になるが、Testが importlib で読み込む経路では解決されないため明示的に足す。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from design_identity import design_version  # noqa: E402
from schema_catalog import (  # noqa: E402
    active_write_versions,
    compute_schema_catalog_hash,
    domain_hash,
    logical_schema_count,
    normalize_core_schemas,
    schema_catalog_entries,
    sha256_file,
)

# 1.3: core_schema_versions / schema_catalog_hash / totals.core_schema_version_count を
#      追加した（Owner Decision D-06。Schemaの複数Version登録に対応する）。
# 1.4: required_evidence_areas を evidence_areas（定義Catalog）と
#      scopes[<phase>].required_evidence_areas（Scope別要求）へ分離した
#      （Owner Decision DEC-U-EVIDENCE-SCOPE）。
SNAPSHOT_VERSION = '1.4'
HASH_PROFILE = 'json-sorted-compact-utf8/1'

# 実装対象Phase。参照仕様Phaseはここに載せない＝Release Scopeとして指定できない。
IMPLEMENTED_PHASES = ['MVP0-A', 'MVP0-B', 'MVP1-A', 'MVP0-C', 'MVP1-D']
REFERENCE_PHASES = [
    'MVP1-B', 'MVP1-C', 'MVP1-E', 'MVP2-A', 'MVP2-B',
    'BLIND_REVIEWER', 'ENTERPRISE',
]

# Evidence領域の正本は design-source/registries/evidence-areas.yaml である。
# v1.11以前はここに「Scopeによらず必須」の定数を置いていた。§26.1が
# 「Release Scopeは判定の入力である」と定め、Case・Gate・test_manifest_hash は
# Scope別に導出していたのに、Evidence領域だけが定数のまま取り残されていた。
# 結果、MVP0-A に「対応Caseが1件も無いのに充足を要求される領域」が残った。
# Owner Decision DEC-U-EVIDENCE-SCOPE でRegistry導出へ移した。


def normalize_evidence_areas(raw: list[dict]) -> list[dict]:
    """Registryの領域定義を検証してSnapshot用の形へ整える。

    ここはFail-Closedで書く。領域定義が壊れたまま通すと、Verifierが
    「必須集合が空」の状態でGOを出せてしまう（v1.0最大の穴と同じ形）。
    """
    if not raw:
        raise SystemExit('evidence-areas.yaml: evidence_areas が空である')
    areas = []
    seen = set()
    for entry in raw:
        name = entry.get('area')
        if not name:
            raise SystemExit('evidence-areas.yaml: area 名が無い項目がある')
        if name in seen:
            raise SystemExit(f'evidence-areas.yaml: area {name} が重複している')
        seen.add(name)
        if not entry.get('evidence_path'):
            raise SystemExit(f'evidence-areas.yaml: {name} に evidence_path が無い')
        human = entry.get('human_measured')
        if not isinstance(human, bool):
            raise SystemExit(f'evidence-areas.yaml: {name} の human_measured が真偽値でない')
        scopes = entry.get('phase_scope')
        if not isinstance(scopes, list) or not scopes:
            raise SystemExit(f'evidence-areas.yaml: {name} の phase_scope が空である')
        unknown = sorted(set(scopes) - set(IMPLEMENTED_PHASES))
        if unknown:
            raise SystemExit(
                f'evidence-areas.yaml: {name} の phase_scope に実装対象外Phase {unknown} がある'
            )
        area = {
            'area': name,
            'evidence_path': entry['evidence_path'],
            'human_measured': human,
            'phase_scope': [p for p in IMPLEMENTED_PHASES if p in scopes],
        }
        bound = entry.get('reuse_bound_subtree')
        if human and not bound:
            # 再利用束縛の無い人手計測領域は、Subtreeが変わっても再利用できてしまう。
            raise SystemExit(
                f'evidence-areas.yaml: {name} は human_measured だが reuse_bound_subtree が無い'
            )
        if bound:
            area['reuse_bound_subtree'] = bound
        areas.append(area)
    return sorted(areas, key=lambda a: a['area'])


def required_areas_for(areas: list[dict], phase: str) -> list[str]:
    """Scopeに必須の領域名。空になる場合は停止する。"""
    required = sorted(a['area'] for a in areas if phase in a['phase_scope'])
    if not required:
        raise SystemExit(f'evidence-areas.yaml: {phase} の必須Evidence領域が0件になる')
    return required


def scoped_test_manifest_hash(cases: list[dict], scope: str) -> str:
    """Scope内Caseの期待値だけを対象にした決定的Hash。

    Manifestの test_manifest_hash はこの値と一致しなければならない。
    これによりTest Manifestの丸ごと差し替えを検出する。
    """
    projection = sorted(
        (
            {
                'test_id': c['test_id'],
                'case_id': c['case_id'],
                'expectation_descriptor_hash': c['expectation_descriptor_hash'],
                'expected_subject_type': c['expected_subject_type'],
                'expected_state': c['expected_state'],
                'expected_error_code': c['expected_error_code'],
                'expected_event_sequence': c['expected_event_sequence'],
                'auto_reexecution_prohibited': c['auto_reexecution_prohibited'],
                'release_allowed': c['release_allowed'],
                'manual_queue_expected': c['manual_queue_expected'],
                'fault_point': c['fault_point'],
                'durability_tier': c['durability_tier'],
            }
            for c in cases
            if scope in c['phase_scope']
        ),
        key=lambda c: (c['test_id'], c['case_id']),
    )
    return domain_hash('FDE-HARNESS/test-manifest/1/',
                       {'release_scope': scope, 'cases': projection})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--registries', required=True, type=Path)
    ap.add_argument('--design', required=True, type=Path)
    ap.add_argument('--out', required=True, type=Path)
    args = ap.parse_args()

    def load(name):
        return yaml.safe_load((args.registries / name).read_text(encoding='utf-8'))

    cases = load('tests.yaml')['test_cases']
    gates = load('gates.yaml')['gates']
    errors = load('errors.yaml')['error_codes']
    events = load('events.yaml')['event_types']
    schemas = load('schemas.yaml')['core_schemas']
    states = load('states.yaml')['state_namespaces']
    evidence_areas = normalize_evidence_areas(
        load('evidence-areas.yaml')['evidence_areas'])

    # 論理Schema数とVersion数を分離する（Owner Decision D-06 条件3）。
    # 登録Pathが存在しない場合は schema_catalog_entries がFail-Closedで停止する。
    schema_entries = normalize_core_schemas(schemas)
    active_versions = active_write_versions(schema_entries)
    schema_catalog = schema_catalog_entries(schema_entries, args.registries.parent.parent)

    scopes = {}
    for phase in IMPLEMENTED_PHASES:
        sel = [c for c in cases if phase in c['phase_scope']]
        scopes[phase] = {
            'required_case_count': len(sel),
            'required_test_id_count': len({c['test_id'] for c in sel}),
            'required_cases': sorted(
                [[c['test_id'], c['case_id']] for c in sel]
            ),
            'required_gate_ids': sorted(
                g['gate_id'] for g in gates if g['phase'] == phase
            ),
            'required_gate_count': sum(1 for g in gates if g['phase'] == phase),
            'test_manifest_hash': scoped_test_manifest_hash(cases, phase),
            # Scope別のRequired Evidence領域。Case・Gateと同じ扱いにする。
            'required_evidence_areas': required_areas_for(evidence_areas, phase),
            'required_evidence_area_count': len(
                required_areas_for(evidence_areas, phase)),
        }

    snapshot = {
        'snapshot_version': SNAPSHOT_VERSION,
        'hash_profile': HASH_PROFILE,
        'design_version': design_version(args.design),
        'design_sha256': sha256_file(args.design),
        'generated_at': dt.datetime.now(dt.timezone.utc).replace(
            microsecond=0).isoformat().replace('+00:00', 'Z'),
        'source_registry_hashes': {
            name: sha256_file(args.registries / name)
            for name in sorted(p.name for p in args.registries.glob('*.yaml'))
        },
        'implemented_phases': IMPLEMENTED_PHASES,
        # Gateが1件も定義されていないPhaseにGOを付与しない。
        'release_enabled_phases': [
            phase for phase in IMPLEMENTED_PHASES
            if any(g['phase'] == phase for g in gates)
        ],
        'reference_phases': REFERENCE_PHASES,
        # 領域の定義Catalog。どのScopeで必須かは scopes[<phase>] 側が持つ。
        # キー名を required_evidence_areas のままにすると、Scope別化した後も
        # 「全部必須」と読める。定義と要求を名前で分ける。
        'evidence_areas': evidence_areas,
        'scopes': scopes,
        'expectations': {
            f'{c["test_id"]}/{c["case_id"]}': {
                'expectation_descriptor_hash': c['expectation_descriptor_hash'],
                'expected_subject_type': c['expected_subject_type'],
                'expected_state': c['expected_state'],
                'expected_error_code': c['expected_error_code'],
                'expected_event_sequence': c['expected_event_sequence'],
                # Event観測Policy（設計書§19.1.1）。Release Verifier はこの値だけを
                # 免除の根拠にする。Registry を直接読ませない。
                #
                # **欠落は欠落のまま載せる。** Policy を持たない Case は
                # `None` になる。値へ倒すと、決まっていない Case が
                # 「Ledger を見なくてよい」と宣言されたことになる。
                'event_observation_policy': c.get('event_observation_policy'),
                'phase_scope': c['phase_scope'],
                'fault_point': c['fault_point'],
                'durability_tier': c['durability_tier'],
            }
            for c in cases
        },
        'gates': {
            g['gate_id']: {
                'phase': g['phase'],
                'condition': g['condition'],
                'test_refs_mode': g['test_refs_mode'],
                'test_refs': g['test_refs'],
            }
            for g in gates
        },
        'error_codes': {e['error_code']: e['classification'] for e in errors},
        'event_types': events,
        # 論理Schema名の一覧。Versionを持たないためVerifierの既存判定と互換である。
        'core_schemas': [s['schema_name'] for s in schemas],
        # 版ごとのPath・Content Hash・書込み可否。Verifierが版を識別できるようにする。
        # Step 3-a では「複数Version形式を使っているときだけ」出す条件付きにしていたが、
        # これは registry-snapshot.json を変更禁止Pathにしていた当時の都合である。
        # Step 3-b で schemas.yaml を複数Version形式へ移したため無条件に出す。
        'core_schema_versions': schema_catalog,
        'schema_catalog_hash': compute_schema_catalog_hash(schema_catalog),
        'core_schema_active_write_versions': active_versions,
        'state_namespaces': states,
        'totals': {
            'case_count': len(cases),
            'test_id_count': len({c['test_id'] for c in cases}),
            'gate_count': len(gates),
            'error_code_count': len(errors),
            'event_type_count': len(events),
            # 論理Schema数（22）とVersion数は別物である。混ぜると
            # 「2.0.0を足したらSchemaが増えた」と読めてしまう。
            'core_schema_count': logical_schema_count(schema_entries),
            'core_schema_version_count': len(schema_entries),
            'state_namespace_count': len(states),
        },
    }
    # §1.11の規約と同じ理由で、生成時刻はHash対象へ含めない。含めると
    # 同一Registryから生成したsnapshot同士が比較できず、鮮度検査が常に
    # 「陳腐化」を報告してしまう。generated_atは来歴Metadataであって内容ではない。
    snapshot['registry_snapshot_hash'] = domain_hash(
        'FDE-HARNESS/registry-snapshot/1/',
        {k: v for k, v in snapshot.items() if k != 'generated_at'})

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'wrote {args.out}')
    print(f'  registry_snapshot_hash = {snapshot["registry_snapshot_hash"]}')
    print(f'  schema_catalog_hash    = {snapshot["schema_catalog_hash"]}')
    print(f'  core schemas           = {logical_schema_count(schema_entries)} logical / '
          f'{len(schema_entries)} versions')
    for phase, s in scopes.items():
        print(f'  {phase}: {s["required_case_count"]} cases / '
              f'{s["required_test_id_count"]} ids / {s["required_gate_count"]} gates')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
