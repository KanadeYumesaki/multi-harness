#!/usr/bin/env python3
"""AT-VERIFIER-*：Runtime GO Verifier自身の否定系試験。

v1.6キットではGO判定器そのものが無検証だった。判定器の終了Codeが
リリース可否の唯一の根拠である以上、判定器は「正しくFAILさせられること」を
機械的に証明できなければならない。

実行:
    python tests/test_verify_runtime_go.py            # 通常
    python tests/test_verify_runtime_go.py --emit-evidence <dir> --manifest <manifest>
        → verifier_self_test 領域のEvidence JSONを生成する
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))

import verify_runtime_go as V  # noqa: E402

DESIGN = KIT / 'design-v1.25-runtime-go.md'
if not DESIGN.is_file():
    raise RuntimeError(f'ASCII design file missing: {DESIGN}')
REGISTRY = KIT / 'registry-snapshot.json'
SCOPE = 'MVP0-A'
GIT_SHA = 'b7c1f4a9e2d6035a8c1f0e4b7d2a95c3e60f81d4'
SUBTREE = 'src/harness/presentation/cli'
SUBTREE_HASH = 'sha256:' + 'ab' * 32
SCHEMA_HASH = 'sha256:' + 'cd' * 32
RUNNER_HASH = 'sha256:' + 'ef' * 32
MIGRATION_HEAD = '0007_effect_receipt'
NOW = '2026-08-05T00:00:00Z'


def sha256_bytes(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def write_json(path: Path, payload: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8') + b'\n'
    path.write_bytes(data)
    return sha256_bytes(data)


def reseal(snap: dict) -> dict:
    """改変したSnapshotの自己Hashを計算し直す。

    Verifierは自己Hashを再計算して改変を検出する。Variantを作るときに
    ここを更新しないと、検査したかった内容ではなく自己Hash不一致が返り、
    **試験が別の理由で通ってしまう。**
    """
    snap['registry_snapshot_hash'] = V.domain_hash(
        'FDE-HARNESS/registry-snapshot/1/',
        {k: v for k, v in snap.items()
         if k not in ('registry_snapshot_hash', 'generated_at')})
    return snap


def snapshot_with_every_policy_declared(scope: str = SCOPE) -> dict:
    """Policy 未確定のCaseへ Policy を与えたSnapshot Variant。

    **実Registryの主張ではない。** 実Registryでは Masking の数件が Owner Decision
    待ちで未確定のまま残っており、その状態では Release GO に到達できない。それが
    正しい振る舞いである（`test_052` が実Snapshotで確かめる）。

    ここで Variant を作るのは、Verifier の**判定Logic**を完全なEvidence一式に対して
    見るためである。未確定Caseを含んだままだと、どの試験も「未確定で落ちた」に
    なってしまい、検査したかった内容が見えない。
    """
    snap = json.loads(REGISTRY.read_text(encoding='utf-8'))
    for test_id, case_id in snap['scopes'][scope]['required_cases']:
        exp = snap['expectations'][f'{test_id}/{case_id}']
        if exp.get('event_observation_policy') is None and not exp['expected_event_sequence']:
            exp['event_observation_policy'] = V.POLICY_REQUIRED_EMPTY
    return reseal(snap)


def snapshot_requiring(*areas: str, scope: str = SCOPE) -> dict:
    """指定Scopeの必要Evidence領域を差し替えたSnapshot Variant。"""
    snap = snapshot_with_every_policy_declared(scope)
    snap['scopes'][scope]['required_evidence_areas'] = sorted(areas)
    snap['scopes'][scope]['required_evidence_area_count'] = len(set(areas))
    return reseal(snap)


class EvidenceTree:
    """Scope内で完全に合格するEvidence Tree一式を生成する。

    各試験はこれを複製し、1箇所だけ壊して「確実にFAILする」ことを確認する。
    """

    def __init__(self, root: Path, snapshot: dict | None = None) -> None:
        self.root = root
        self.snap = (snapshot if snapshot is not None
                     else snapshot_with_every_policy_declared())
        self.scope = self.snap['scopes'][SCOPE]
        self.manifest_path = root / 'runtime-go-manifest.json'
        self.evidence_root = root / 'runtime-evidence'
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        # Verifier には**この木を組んだSnapshotそのもの**を読ませる。
        # 実Registryを読ませると、Variantで組んだManifestと食い違い、
        # 検査したかった内容ではなくHash不一致が返る。
        self.registry_path = root / 'registry-snapshot.json'
        self.registry_path.write_text(
            json.dumps(self.snap, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8')
        self.manifest = self._build()

    def _build(self) -> dict:
        environment_rel = 'environment/runtime-environment.json'
        environment_hash = write_json(self.evidence_root / environment_rel, {
            'evidence_schema_version': V.EVIDENCE_SCHEMA_VERSION,
            'release_scope': SCOPE, 'area': 'environment', 'status': 'PASS',
            'summary': {'checked': True}, 'producer': 'verifier-contract-test/1',
            'test_run_id': 'run-environment', 'started_at': NOW, 'recorded_at': NOW,
            'implementation_commit_sha': GIT_SHA, 'schema_set_hash': SCHEMA_HASH,
            'migration_head': MIGRATION_HEAD, 'is_wsl2': True,
            'workspace_on_linux_native_fs': True, 'python_version': '3.12.4',
            'workspace_root': '/home/test/fde-harness', 'mount_point': '/',
            'filesystem_type': 'ext4', 'mount_id': 42,
            'mountinfo_hash': sha256_bytes(b'mountinfo'),
            'python_lock_hash': sha256_bytes(b'lock'),
        })

        common = {
            'implementation_commit_sha': GIT_SHA,
            'runtime_environment_hash': environment_hash,
            'schema_set_hash': SCHEMA_HASH,
            'migration_head': MIGRATION_HEAD,
            'producer': 'verifier-contract-test/1',
            'started_at': NOW,
            'recorded_at': NOW,
        }
        cases = []
        for test_id, case_id in self.scope['required_cases']:
            exp = self.snap['expectations'][f'{test_id}/{case_id}']
            rel = f'tests/cases/{test_id}/{case_id}/evidence.json'
            fixture_rel = f'fixtures/{test_id}/{case_id}.json'
            fixture_path = self.evidence_root / fixture_rel
            fixture_path.parent.mkdir(parents=True, exist_ok=True)
            fixture_path.write_bytes(f'fixture:{test_id}:{case_id}'.encode())
            fixture = V.sha256_file(fixture_path)
            raw_rel = f'raw/{test_id}/{case_id}.json'
            raw_hash = write_json(self.evidence_root / raw_rel, {
                'test_id': test_id, 'case_id': case_id, 'exit_code': 0,
                'observed_state': exp['expected_state'],
            })
            h = write_json(self.evidence_root / rel, {
                **common,
                'evidence_schema_version': V.EVIDENCE_SCHEMA_VERSION,
                'release_scope': SCOPE,
                'test_id': test_id,
                'case_id': case_id,
                'status': 'PASS',
                'expectation_descriptor_hash': exp['expectation_descriptor_hash'],
                'input_fixture_hash': fixture,
                'input_fixture_path': fixture_rel,
                'raw_result_path': raw_rel,
                'raw_result_hash': raw_hash,
                'runner_source_hash': RUNNER_HASH,
                'command': ['python', '-m', 'pytest', f'{test_id}/{case_id}'],
                'exit_code': 0,
                'observed_subject_type': exp['expected_subject_type'],
                'observed_state': exp['expected_state'],
                'observed_error_code': exp['expected_error_code'],
                # 健全な Release Evidence の基準形。Snapshot の Policy が
                # 層と観測有無を決める。Fixture 側で推論しない。
                'event_observation_policy': exp.get('event_observation_policy'),
                'evidence_kind': (
                    V.POLICY_SHAPE[exp['event_observation_policy']][0]
                    if exp.get('event_observation_policy') else 'ORCHESTRATION'),
                'event_observation': (
                    V.POLICY_SHAPE[exp['event_observation_policy']][1]
                    if exp.get('event_observation_policy')
                    else V.OBS_OBSERVED),
                'observed_event_sequence': (
                    None
                    if exp.get('event_observation_policy') == V.POLICY_NOT_APPLICABLE
                    else exp['expected_event_sequence']),
                'assertions': [{'expression': 'actual_state == expected_state',
                                'result': True}],
                'durability_tier': exp.get('durability_tier'),
                'test_run_id': f'run-{test_id}-{case_id}',
            })
            cases.append({'test_id': test_id, 'case_id': case_id, 'status': 'PASS',
                          'input_fixture_hash': fixture,
                          'evidence_manifest_hash': h, 'evidence_path': rel})

        gates = []
        for gid in self.scope['required_gate_ids']:
            spec = self.snap['gates'][gid]
            refs = spec['test_refs']
            if spec['test_refs_mode'] == 'ALL_IN_SCOPE':
                case_refs = [list(p) for p in self.scope['required_cases']]
            else:
                case_refs = [[t, c] for t, c in self.scope['required_cases'] if t in refs]
            rel = f'gates/{gid}.json'
            h = write_json(self.evidence_root / rel, {
                **common,
                'evidence_schema_version': V.EVIDENCE_SCHEMA_VERSION,
                'release_scope': SCOPE, 'gate_id': gid, 'status': 'PASS',
                'test_refs': refs, 'case_refs': case_refs,
                'summary': {'case_count': len(case_refs)},
                'test_run_id': f'run-{gid}',
            })
            gates.append({'gate_id': gid, 'status': 'PASS',
                          'evidence_manifest_hash': h, 'evidence_path': rel})

        areas = []
        area_hashes = {}
        # 領域定義はCatalog、必須集合はScope側。Catalogを回すと
        # MVP0-Aへ approval_ux のEvidenceを作ることになる。
        catalog = {a['area']: a for a in self.snap['evidence_areas']}
        for area_name in self.scope['required_evidence_areas']:
            spec = catalog[area_name]
            rel = spec['evidence_path']
            if spec['area'] == 'environment':
                h = environment_hash
            else:
                summary = {'checked': True}
                if spec['area'] == 'source_static_analysis':
                    summary = {'implementation_commit_sha': GIT_SHA,
                               'source_tree_clean': True,
                               'source_tree_hash': sha256_bytes(b'source-tree')}
                elif spec['area'] == 'core_schema_suite':
                    summary = {'schema_set_hash': SCHEMA_HASH,
                               'validated_schema_count': len(self.snap['core_schemas'])}
                elif spec['area'] == 'sqlite_migration':
                    summary = {'migration_head': MIGRATION_HEAD,
                               'fresh_install_passed': True}
                elif spec['area'] == 'unit_case_suite':
                    # Unit Case は Snapshot の Policy から引く。Fixture 側で
                    # Case ID を並べない。
                    unit_ids = sorted(
                        f"{c['test_id']}/{c['case_id']}" for c in cases
                        if self.snap['expectations'][
                            f"{c['test_id']}/{c['case_id']}"
                        ].get('event_observation_policy') == V.POLICY_NOT_APPLICABLE)
                    by_key = {f"{c['test_id']}/{c['case_id']}": c for c in cases}
                    summary = {
                        'case_ids': unit_ids,
                        'case_evidence_hashes': sorted(
                            by_key[k]['evidence_manifest_hash'] for k in unit_ids),
                        'unit_case_count': len(unit_ids),
                        'failing_case_ids': [],
                        'design_sha256': self.snap['design_sha256'],
                        'registry_snapshot_hash': self.snap['registry_snapshot_hash'],
                        'human_measured': False,
                    }
                elif spec['area'] == 'verifier_self_test':
                    summary = {
                        'verifier_source_sha256': V.sha256_file(KIT / 'verify_runtime_go.py'),
                        'tests_run': V.SELF_TEST_MIN_TESTS,
                        'failures': 0, 'errors': 0,
                    }
                h = write_json(self.evidence_root / rel, {
                    **common,
                    'evidence_schema_version': V.EVIDENCE_SCHEMA_VERSION,
                    'release_scope': SCOPE, 'area': spec['area'], 'status': 'PASS',
                    'summary': summary,
                    'test_run_id': f'run-{spec["area"]}',
                })
            area_hashes[spec['area']] = (rel, h)
            areas.append({'area': spec['area'], 'status': 'PASS',
                          'evidence_manifest_hash': h, 'evidence_path': rel})

        # Environment Manifestは environment 領域のEvidenceと同一Fileである。
        env_path, env_hash = area_hashes['environment']

        return {
            'manifest_version': V.MANIFEST_VERSION,
            'design_version': self.snap['design_version'],
            'design_sha256': self.snap['design_sha256'],
            'release_scope': SCOPE,
            'registry_snapshot_hash': self.snap['registry_snapshot_hash'],
            'implementation_repository': 'https://example.invalid/fde-harness.git',
            'implementation_commit_sha': GIT_SHA,
            'source_tree_clean': True,
            'source_subtree_hashes': {SUBTREE: SUBTREE_HASH},
            'runtime_environment': {
                'environment_manifest_path': env_path,
                'environment_manifest_hash': env_hash,
                'is_wsl2': True,
                'workspace_on_linux_native_fs': True,
                'python_version': '3.12.4',
            },
            'schema_set_hash': SCHEMA_HASH,
            'migration_head': MIGRATION_HEAD,
            'test_manifest_hash': self.scope['test_manifest_hash'],
            'expected_gate_count': self.scope['required_gate_count'],
            'expected_test_id_count': self.scope['required_test_id_count'],
            'expected_case_count': self.scope['required_case_count'],
            'test_cases': cases,
            'gates': gates,
            'required_evidence_areas': areas,
            'skipped_count': 0,
            'xfail_count': 0,
            'release_decision': 'RUNTIME_GO',
        }

    def run(self, manifest: dict | None = None, registry: Path | None = None,
            scope: str = SCOPE) -> tuple[dict, int]:
        self.manifest_path.write_text(
            json.dumps(manifest or self.manifest, ensure_ascii=False, indent=2),
            encoding='utf-8')
        return V.verify(DESIGN, self.manifest_path, self.evidence_root,
                        registry or self.registry_path, scope)

    def mutate(self) -> dict:
        return copy.deepcopy(self.manifest)


class VerifierContract(unittest.TestCase):
    tree: EvidenceTree
    tmp: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix='at-verifier-')
        cls.tree = EvidenceTree(Path(cls.tmp))
        # 人手計測領域を含むVariant。§26.6の再利用契約を検査するために要る。
        cls.human_tmp = tempfile.mkdtemp(prefix='at-verifier-human-')
        base = json.loads(REGISTRY.read_text(encoding='utf-8'))
        cls.human_tree = EvidenceTree(
            Path(cls.human_tmp),
            snapshot=snapshot_requiring(
                *base['scopes'][SCOPE]['required_evidence_areas'], 'approval_ux'),
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)
        shutil.rmtree(cls.human_tmp, ignore_errors=True)

    def assert_go(self, report, code, msg=''):
        self.assertEqual(code, V.EXIT_OK,
                         f'{msg} expected GO, got {report.get("errors")} '
                         f'{report.get("missing")}')
        self.assertEqual(report['decision'], 'RUNTIME_GO')

    def assert_no_go(self, report, code, expect_code=None, contains=None):
        self.assertNotEqual(code, V.EXIT_OK, 'verifier wrongly returned RUNTIME_GO')
        self.assertNotEqual(report.get('decision'), 'RUNTIME_GO')
        if expect_code is not None:
            self.assertEqual(code, expect_code, report)
        if contains:
            blob = json.dumps(report, ensure_ascii=False)
            self.assertIn(contains, blob, report)

    # --- AT-VERIFIER-001: 完全なEvidenceでのみGOになる ---
    def test_001_happy_path(self):
        self.assert_go(*self.tree.run())

    # --- AT-VERIFIER-002: v1.0最大の穴。空Areaで要件が消えない ---
    def test_002_empty_evidence_areas(self):
        m = self.tree.mutate()
        m['required_evidence_areas'] = []
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_MISSING,
                          contains='required evidence area not in manifest')

    # --- AT-VERIFIER-003: 同一Evidenceの使い回しを拒否 ---
    def test_003_evidence_reuse_across_cases(self):
        m = self.tree.mutate()
        m['test_cases'][1]['evidence_path'] = m['test_cases'][0]['evidence_path']
        m['test_cases'][1]['evidence_manifest_hash'] = \
            m['test_cases'][0]['evidence_manifest_hash']
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='evidence reused from')

    # --- AT-VERIFIER-004/005: escape検査が cases/gates/areas で統一されている ---
    def test_004_gate_evidence_absolute_path_escape(self):
        outside = Path(self.tmp) / 'outside.json'
        h = write_json(outside, {'evidence_schema_version': V.EVIDENCE_SCHEMA_VERSION})
        m = self.tree.mutate()
        m['gates'][0]['evidence_path'] = outside.resolve().as_posix()
        m['gates'][0]['evidence_manifest_hash'] = h
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='must be relative to evidence-root')

    def test_005_area_evidence_traversal_escape(self):
        m = self.tree.mutate()
        m['required_evidence_areas'][0]['evidence_path'] = '../outside.json'
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='escapes evidence-root')

    # --- AT-VERIFIER-006/007: Scope集合の厳密一致 ---
    def test_006_out_of_scope_case_rejected(self):
        m = self.tree.mutate()
        m['test_cases'].append({
            'test_id': 'AT-OUTBOX-001', 'case_id': 'REMOTE_FOUND', 'status': 'PASS',
            'input_fixture_hash': sha256_bytes(b'x'),
            'evidence_manifest_hash': sha256_bytes(b'y'), 'evidence_path': 'x.json'})
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='out of release scope')

    def test_007_missing_required_case_detected(self):
        m = self.tree.mutate()
        dropped = m['test_cases'].pop()
        m['expected_case_count'] -= 1
        report, code = self.tree.run(m)
        self.assert_no_go(report, code)
        self.assertIn(dropped['test_id'], json.dumps(report, ensure_ascii=False))

    # --- AT-VERIFIER-008: ADR-003 Python下限 ---
    def test_008_python_version_below_minimum(self):
        m = self.tree.mutate()
        m['runtime_environment']['python_version'] = '2.7.0'
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='ADR-003')

    # --- AT-VERIFIER-009/010/011: Evidence中身の検証 ---
    def test_009_empty_evidence_content_rejected(self):
        m = self.tree.mutate()
        target = self.tree.evidence_root / m['test_cases'][0]['evidence_path']
        data = b'{}\n'
        target.write_bytes(data)
        m['test_cases'][0]['evidence_manifest_hash'] = sha256_bytes(data)
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='evidence_schema_version')
        self.tree = EvidenceTree(Path(self.tmp))  # 木を復元

    def test_010_observed_state_mismatch(self):
        m = self.tree.mutate()
        entry = m['test_cases'][0]
        target = self.tree.evidence_root / entry['evidence_path']
        doc = json.loads(target.read_text(encoding='utf-8'))
        doc['observed_state'] = 'SUCCEEDED_BUT_ACTUALLY_NOT'
        entry['evidence_manifest_hash'] = write_json(target, doc)
        try:
            self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                              contains='observed_state')
        finally:
            self.tree = EvidenceTree(Path(self.tmp))

    def test_011_failed_assertion_rejected(self):
        m = self.tree.mutate()
        entry = m['test_cases'][0]
        target = self.tree.evidence_root / entry['evidence_path']
        doc = json.loads(target.read_text(encoding='utf-8'))
        doc['assertions'] = [{'expression': 'x == y', 'result': False}]
        entry['evidence_manifest_hash'] = write_json(target, doc)
        try:
            self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                              contains='assertion not satisfied')
        finally:
            self.tree = EvidenceTree(Path(self.tmp))

    # --- AT-VERIFIER-012/013/014: 正本への束縛 ---
    def test_012_test_manifest_hash_swap_detected(self):
        m = self.tree.mutate()
        m['test_manifest_hash'] = sha256_bytes(b'other-manifest')
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='test_manifest_hash does not match')

    def test_013_registry_tamper_detected(self):
        snap = json.loads(REGISTRY.read_text(encoding='utf-8'))
        snap['scopes'][SCOPE]['required_case_count'] = 1
        tampered = Path(self.tmp) / 'tampered-registry.json'
        tampered.write_text(json.dumps(snap, ensure_ascii=False), encoding='utf-8')
        report, code = self.tree.run(registry=tampered)
        self.assert_no_go(report, code, expect_code=V.EXIT_INPUT_INVALID,
                          contains='registry snapshot hash mismatch')

    def test_014_design_hash_mismatch_detected(self):
        m = self.tree.mutate()
        m['design_sha256'] = sha256_bytes(b'not the design')
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='design_sha256 does not match')

    # --- AT-VERIFIER-015: report hashが実際に判定内容を束縛する ---
    def test_015_report_hash_binds_manifest(self):
        base, _ = self.tree.run()
        m = self.tree.mutate()
        m['implementation_commit_sha'] = 'c' * 40
        other, _ = self.tree.run(m)
        self.assertNotEqual(
            base['verification_report_hash'], other['verification_report_hash'],
            'v1.0では別内容のManifestが同一report hashを出していた')
        self.tree.run()  # manifest fileを元へ戻す

    # --- AT-VERIFIER-016: GateがPASS Caseだけを根拠にする ---
    def test_016_gate_case_ref_must_be_passing(self):
        m = self.tree.mutate()
        target = self.tree.evidence_root / m['gates'][0]['evidence_path']
        doc = json.loads(target.read_text(encoding='utf-8'))
        doc['case_refs'] = [['AT-NOT-A-REAL-TEST', 'NOPE']]
        m['gates'][0]['evidence_manifest_hash'] = write_json(target, doc)
        try:
            self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                              contains='is not a PASS case')
        finally:
            self.tree = EvidenceTree(Path(self.tmp))

    # --- AT-VERIFIER-017: 参照仕様PhaseはScopeに指定できない ---
    def test_017_reference_phase_scope_rejected(self):
        report, code = self.tree.run(scope='MVP1-E')
        self.assert_no_go(report, code, expect_code=V.EXIT_INPUT_INVALID,
                          contains='not release-enabled')

    # --- AT-VERIFIER-018: 宣言件数の突合 ---
    def test_018_declared_count_mismatch(self):
        m = self.tree.mutate()
        m['expected_gate_count'] = 1
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='expected_gate_count must be')

    # --- AT-VERIFIER-019/020/021: §26.6 再認定階層 ---
    # v1.12で approval_ux は MVP0-A の必須から外れた（§23.7）。人手計測Evidenceの
    # 再利用契約そのものは検査し続ける必要があるため、approval_ux を要求する
    # Snapshot Variant の上で同じ主張を固定する。試験は消していない。
    def test_019_reused_human_evidence_invalidated_by_subtree_change(self):
        m = self.human_tree.mutate()
        area = next(a for a in m['required_evidence_areas'] if a['area'] == 'approval_ux')
        area['reused_from'] = {'release_id': 'r-2026-08-01',
                               'bound_subtree_hash': 'sha256:' + 'cd' * 32}
        self.assert_no_go(*self.human_tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='reused evidence is invalidated')

    def test_020_reused_human_evidence_accepted_when_subtree_unchanged(self):
        m = self.human_tree.mutate()
        area = next(a for a in m['required_evidence_areas'] if a['area'] == 'approval_ux')
        area['reused_from'] = {'release_id': 'r-2026-08-01',
                               'bound_subtree_hash': SUBTREE_HASH}
        self.assert_go(*self.human_tree.run(m),
                       'subtree未変更なら人手計測Evidenceは再利用可')

    def test_021_reuse_denied_for_machine_measured_area(self):
        m = self.tree.mutate()
        area = next(a for a in m['required_evidence_areas'] if a['area'] == 'spec_lint')
        area['reused_from'] = {'release_id': 'r-2026-08-01',
                               'bound_subtree_hash': SUBTREE_HASH}
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='only permitted for human-measured')

    # --- AT-VERIFIER-002: Scope別Required Evidence領域（v1.12 §23.7） ---
    # ここで守るのは「要求の減らし方」である。Scope別化は必須集合を
    # 小さくする変更なので、境界を固定しないと素通しになる。

    def test_032_mvp0a_passes_without_approval_ux(self):
        """MVP0-Aは approval_ux 無しでGOになる。

        これが本改訂の目的そのものである。承認UIはMVP0-Bで実装するため、
        MVP0-Aでは人手計測Evidenceを作れない。
        """
        areas = {a['area'] for a in self.tree.manifest['required_evidence_areas']}
        self.assertNotIn('approval_ux', areas)
        self.assert_go(*self.tree.run(), 'MVP0-Aはapproval_ux無しで成立する')

    def test_033_registry_derives_scope_areas_and_keeps_the_area_defined(self):
        """必須集合はRegistry導出。approval_uxはCatalogに残りMVP0-Aから外れる。

        領域を**消す**と「その検証はもう要らない」という意味になる。
        要らないのではなく、MVP0-Aの時点では対象が存在しないだけである。
        """
        snap = json.loads(REGISTRY.read_text(encoding='utf-8'))
        catalog = {a['area'] for a in snap['evidence_areas']}
        self.assertIn('approval_ux', catalog, 'approval_uxはCatalogから消さない')

        mvp0a = set(snap['scopes']['MVP0-A']['required_evidence_areas'])
        mvp0b = set(snap['scopes']['MVP0-B']['required_evidence_areas'])
        self.assertNotIn('approval_ux', mvp0a)
        self.assertIn('approval_ux', mvp0b)
        # MVP0-A は MVP0-B の真部分集合であり、差分は approval_ux だけである。
        self.assertEqual(mvp0b - mvp0a, {'approval_ux'})
        self.assertEqual(mvp0a - mvp0b, set())
        # 件数はコードに書かず導出値と突き合わせる（不変条件#18）。
        for scope_name, spec in snap['scopes'].items():
            self.assertEqual(
                spec['required_evidence_area_count'],
                len(spec['required_evidence_areas']),
                f'{scope_name}: 件数と集合が食い違う')
            self.assertTrue(set(spec['required_evidence_areas']) <= catalog,
                            f'{scope_name}: Catalogに無い領域を要求している')

    def test_034_report_area_count_is_scope_derived_not_catalog_size(self):
        """Reportの領域件数がScope由来であること。Catalog件数を出さない。"""
        report, _ = self.tree.run()
        snap = json.loads(REGISTRY.read_text(encoding='utf-8'))
        self.assertEqual(report['required_evidence_area_count'],
                         len(snap['scopes'][SCOPE]['required_evidence_areas']))
        self.assertNotEqual(report['required_evidence_area_count'],
                            len(snap['evidence_areas']),
                            'Catalog全件を出しているならScope別化が効いていない')

    def test_035_scope_requiring_approval_ux_rejects_its_absence(self):
        """approval_uxを要求するScopeで、それが無ければ拒否する。

        MVP0-Bはこの構成である。「MVP0-Aで要求しない」を
        「誰も要求しない」へ広げていないことを固定する。
        """
        m = self.human_tree.mutate()
        m['required_evidence_areas'] = [
            a for a in m['required_evidence_areas'] if a['area'] != 'approval_ux'
        ]
        self.assert_no_go(*self.human_tree.run(m), expect_code=V.EXIT_MISSING,
                          contains='required evidence area not in manifest: approval_ux')

    def test_036_out_of_scope_area_is_rejected(self):
        """Scopeが要求しない領域をManifestへ足せない。

        足せるならScope別化は「出しても出さなくてもよい」になり、
        MVP0-Bでapproval_uxを省く抜け道と対称の穴になる。
        """
        m = self.tree.mutate()
        m['required_evidence_areas'].append({
            'area': 'approval_ux', 'status': 'PASS',
            'evidence_manifest_hash': 'sha256:' + '11' * 32,
            'evidence_path': 'approval/approval-ux-report.json',
        })
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='evidence area out of release scope MVP0-A')

    def test_037_empty_scope_area_set_is_input_invalid(self):
        """Scopeの必要領域が空のSnapshotで判定しない。

        v1.0最大の穴は「required_evidence_areasを空配列にすると全要件が消える」
        だった。Scope別化で同じ穴を作らない。
        """
        registry = Path(self.tmp) / 'empty-areas-snapshot.json'
        registry.write_text(
            json.dumps(snapshot_requiring(), ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8')
        self.assert_no_go(*self.tree.run(registry=registry),
                          expect_code=V.EXIT_INPUT_INVALID,
                          contains='no required_evidence_areas')

    def test_038_undefined_area_in_scope_is_rejected(self):
        """Catalogに定義の無い領域をScopeが要求していたら止める。"""
        registry = Path(self.tmp) / 'undefined-area-snapshot.json'
        registry.write_text(
            json.dumps(snapshot_requiring('spec_lint', 'no_such_area'),
                       ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8')
        self.assert_no_go(*self.tree.run(registry=registry), expect_code=V.EXIT_FAIL,
                          contains='not defined in catalog')

    def test_039_unknown_scope_is_rejected_without_fallback(self):
        """未知Scopeは拒否する。全領域へフォールバックしない。"""
        for bogus in ('MVP9-Z', '', 'mvp0-a'):
            report, code = self.tree.run(scope=bogus)
            self.assert_no_go(report, code, expect_code=V.EXIT_INPUT_INVALID)
            self.assertNotIn('required_evidence_area_count', report,
                             f'{bogus}: 拒否したのに領域件数を出している')

    def test_040_no_bypass_case_does_not_satisfy_approval_ux(self):
        """NO_BYPASSがPASSでも approval_ux 領域の代わりにならない。

        NO_BYPASS は SOURCE_SCAN であってUX計測ではない。
        Caseが残ることと領域が充足されることは別である。
        """
        cases = {(c['test_id'], c['case_id']) for c in self.tree.manifest['test_cases']}
        self.assertIn(('AT-APPROVAL-UX-001', 'NO_BYPASS'), cases,
                      'NO_BYPASSはMVP0-Aへ残す')
        # NO_BYPASS がPASSしているTreeでも、approval_uxを要求するScopeは通らない。
        m = self.human_tree.mutate()
        self.assertIn(('AT-APPROVAL-UX-001', 'NO_BYPASS'),
                      {(c['test_id'], c['case_id']) for c in m['test_cases']})
        m['required_evidence_areas'] = [
            a for a in m['required_evidence_areas'] if a['area'] != 'approval_ux'
        ]
        self.assert_no_go(*self.human_tree.run(m), expect_code=V.EXIT_MISSING,
                          contains='approval_ux')

    def test_041_human_measured_only_governs_reuse_not_requirement(self):
        """`human_measured` は再利用可否だけを決め、必須性には関与しない。

        2つを結び付けると、人手計測を機械計測へ変えるだけで必須性が変わる。
        機械計測領域への `reused_from` は引き続き拒否される（test_021）。
        """
        snap = json.loads(REGISTRY.read_text(encoding='utf-8'))
        catalog = {a['area']: a for a in snap['evidence_areas']}
        human = {name for name, a in catalog.items() if a['human_measured']}
        self.assertEqual(human, {'approval_ux'})
        # human_measured であっても、Scopeが要求しなければ必須ではない。
        self.assertNotIn('approval_ux',
                         snap['scopes']['MVP0-A']['required_evidence_areas'])
        # 人手計測領域には再利用束縛Subtreeが必ずある。
        for name in human:
            self.assertTrue(catalog[name].get('reuse_bound_subtree'),
                            f'{name}: 人手計測なのに再利用束縛が無い')

    def test_042_reused_from_without_prior_release_is_not_accepted(self):
        """過去Releaseが無ければ `reused_from` は成立しない。

        `release_id` が空の再利用宣言を通すと、「最初のRelease」が
        人手計測領域を再利用で埋められてしまう。
        """
        m = self.human_tree.mutate()
        area = next(a for a in m['required_evidence_areas'] if a['area'] == 'approval_ux')
        area['reused_from'] = {'release_id': '', 'bound_subtree_hash': SUBTREE_HASH}
        self.assert_no_go(*self.human_tree.run(m), expect_code=V.EXIT_MISSING,
                          contains='approval_ux.reused_from.release_id')

    # --- AT-VERIFIER-044〜051: Evidence 2.0 の層とEvent観測（v1.15 §26.2.1）---

    def _swap_case_evidence(self, m, index, **fields):
        """1 Case の Evidence を差し替えた変種を置く。

        共有Fixtureの実Fileは書き換えない。書き換えると同じClassの他の試験へ
        漏れる。別Pathへ変種を書き、Manifestの束縛をそちらへ向ける。
        """
        entry = m['test_cases'][index]
        source = self.tree.evidence_root / entry['evidence_path']
        doc = json.loads(source.read_text(encoding='utf-8'))
        doc.update(fields)
        rel = f"variant/{entry['test_id']}/{entry['case_id']}.json"
        entry['evidence_manifest_hash'] = write_json(
            self.tree.evidence_root / rel, doc)
        entry['evidence_path'] = rel
        return entry

    def _case_index_by_policy(self, m, policy):
        """Snapshot の Policy で見本Caseを選ぶ。Case IDを手で書かない。"""
        expectations = self.tree.snap['expectations']
        for i, entry in enumerate(m['test_cases']):
            key = f"{entry['test_id']}/{entry['case_id']}"
            if expectations[key].get('event_observation_policy') == policy:
                return i
        self.fail(f'Policy {policy!r} の Case が Scope 内に無い')

    def _case_index(self, m, *, empty_expectation):
        """期待Event列が空／非空のCaseをSnapshotから選ぶ。Case IDを手で書かない。"""
        expectations = self.tree.snap['expectations']
        for i, entry in enumerate(m['test_cases']):
            key = f"{entry['test_id']}/{entry['case_id']}"
            want = expectations[key]['expected_event_sequence']
            if bool(want) is not empty_expectation:
                return i
        self.fail(f'期待Event列が{"空" if empty_expectation else "非空"}のCaseが無い')

    def test_044_correct_policy_shape_is_accepted(self):
        """Snapshot の Policy どおりに組んだ Evidence が通ること。

        `NOT_APPLICABLE` の Case は Unit 層・`null` で受理される。
        Ledger を観測していないことを、観測したことに書き換えていない。
        """
        m = self.tree.mutate()
        i = self._case_index_by_policy(m, V.POLICY_NOT_APPLICABLE)
        entry = m['test_cases'][i]
        doc = json.loads(
            (self.tree.evidence_root / entry['evidence_path']).read_text(encoding='utf-8'))
        self.assertEqual(doc['evidence_kind'], 'UNIT')
        self.assertEqual(doc['event_observation'], V.OBS_NOT_APPLICABLE)
        self.assertIsNone(doc['observed_event_sequence'])
        self.assertEqual(doc['event_observation_policy'], V.POLICY_NOT_APPLICABLE)
        self.assert_go(*self.tree.run(m), msg='正しいPolicy形')

    def test_045_orchestration_may_not_claim_not_applicable(self):
        """作用を主張する層がLedger観測の免除を名乗れないこと。"""
        m = self.tree.mutate()
        i = self._case_index_by_policy(m, V.POLICY_NOT_APPLICABLE)
        self._swap_case_evidence(
            m, i, evidence_kind='ORCHESTRATION',
            event_observation=V.OBS_NOT_APPLICABLE,
            observed_event_sequence=None)
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='must not claim NOT_APPLICABLE')

    def test_046_observed_empty_may_not_use_null(self):
        """「見て0件」を `null` で表せないこと。未観測と区別が付かなくなる。"""
        m = self.tree.mutate()
        i = self._case_index_by_policy(m, V.POLICY_REQUIRED_EMPTY)
        self._swap_case_evidence(
            m, i, evidence_kind='ORCHESTRATION',
            event_observation=V.OBS_OBSERVED_EMPTY,
            observed_event_sequence=None)
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='requires observed_event_sequence=[]')

    def test_047_not_applicable_may_not_use_an_empty_list(self):
        """非該当を空配列で表せないこと。`[]` は「見て0件」の意味である。"""
        m = self.tree.mutate()
        i = self._case_index_by_policy(m, V.POLICY_NOT_APPLICABLE)
        self._swap_case_evidence(
            m, i, evidence_kind='UNIT',
            event_observation=V.OBS_NOT_APPLICABLE,
            observed_event_sequence=[])
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='requires observed_event_sequence=null')

    def test_048_observed_requires_a_non_empty_sequence(self):
        """`OBSERVED` が空列を持てないこと。空なら `OBSERVED_EMPTY` である。"""
        m = self.tree.mutate()
        # Policy を持たない（＝期待列が非空の）Case を選ぶ。Policy 付きだと
        # 層／観測の整合検査が先に当たり、列の検査まで届かない。
        i = self._case_index(m, empty_expectation=False)
        self._swap_case_evidence(
            m, i, evidence_kind='ORCHESTRATION',
            event_observation=V.OBS_OBSERVED,
            observed_event_sequence=[])
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='requires a non-empty observed_event_sequence')

    def test_049_missing_evidence_kind_is_rejected(self):
        """層を名乗らないEvidenceを受理しないこと。"""
        m = self.tree.mutate()
        i = self._case_index_by_policy(m, V.POLICY_NOT_APPLICABLE)
        entry = self._swap_case_evidence(m, i)
        source = self.tree.evidence_root / entry['evidence_path']
        doc = json.loads(source.read_text(encoding='utf-8'))
        doc.pop('evidence_kind')
        entry['evidence_manifest_hash'] = write_json(source, doc)
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='evidence_kind=None')

    def test_050_unknown_event_observation_is_rejected(self):
        """語彙の外の値を受理しないこと。"""
        m = self.tree.mutate()
        i = self._case_index_by_policy(m, V.POLICY_NOT_APPLICABLE)
        self._swap_case_evidence(
            m, i, evidence_kind='UNIT', event_observation='SKIPPED',
            observed_event_sequence=None)
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains="event_observation='SKIPPED'")

    def test_051_not_applicable_contradicting_the_registry_is_rejected(self):
        """期待Event列が非空のCaseが非該当を名乗れないこと。

        Registryが作用を要求しているのに、Evidence側の申告だけで観測を
        免除できてはならない。
        """
        m = self.tree.mutate()
        i = self._case_index(m, empty_expectation=False)
        self._swap_case_evidence(
            m, i, evidence_kind='UNIT',
            event_observation=V.OBS_NOT_APPLICABLE,
            observed_event_sequence=None,
            event_observation_policy=V.POLICY_NOT_APPLICABLE)
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='!= snapshot None')

    # --- AT-VERIFIER-052〜058: Policy を Release の信頼根へ束縛する ---

    def test_052_required_empty_altered_to_not_applicable_is_rejected(self):
        """`REQUIRED_EMPTY` の Case を `NOT_APPLICABLE` へ書き換えて拒否されること。

        Evidence 側の申告だけで Ledger 観測の免除を作らせない。
        """
        m = self.tree.mutate()
        i = self._case_index_by_policy(m, V.POLICY_REQUIRED_EMPTY)
        self._swap_case_evidence(
            m, i, evidence_kind='UNIT',
            event_observation=V.OBS_NOT_APPLICABLE,
            observed_event_sequence=None,
            event_observation_policy=V.POLICY_NOT_APPLICABLE)
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='!= snapshot')

    def test_053_not_applicable_altered_to_observed_empty_is_rejected(self):
        """`NOT_APPLICABLE` の Case を `OBSERVED_EMPTY` へ書き換えて拒否されること。

        見ていないものを「見て0件だった」に格上げさせない。
        """
        m = self.tree.mutate()
        i = self._case_index_by_policy(m, V.POLICY_NOT_APPLICABLE)
        self._swap_case_evidence(
            m, i, evidence_kind='ORCHESTRATION',
            event_observation=V.OBS_OBSERVED_EMPTY,
            observed_event_sequence=[])
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='requires evidence_kind=UNIT')

    def test_054_snapshot_policy_tampering_breaks_the_snapshot_hash(self):
        """Snapshot の Policy を書き換えたら自己Hashが合わなくなること。

        Snapshot は Release の信頼根である。ここを書き換えて免除を作れるなら
        束縛の意味が無い。
        """
        snap = copy.deepcopy(self.tree.snap)
        key = next(k for k, v in snap['expectations'].items()
                   if v.get('event_observation_policy') == V.POLICY_REQUIRED_EMPTY)
        snap['expectations'][key]['event_observation_policy'] = V.POLICY_NOT_APPLICABLE
        # **reseal しない。** 書き換えたのに自己Hashを直さない状態を見る。
        path = Path(self.tmp) / 'tampered-registry.json'
        path.write_text(json.dumps(snap, ensure_ascii=False, indent=2) + '\n',
                        encoding='utf-8')
        # Snapshot 自体の自己Hash不一致は入力の破綻であり、Case毎の検査へ進む前に
        # 止まる。Release判定の入力が壊れているのに判定を続けない。
        self.assert_no_go(*self.tree.run(registry=path),
                          expect_code=V.EXIT_INPUT_INVALID,
                          contains='registry snapshot hash mismatch')

    def test_055_evidence_policy_tampering_breaks_the_manifest_hash(self):
        """Evidence の Policy を書き換えたら Manifest の束縛Hashが合わなくなること。"""
        m = self.tree.mutate()
        i = self._case_index_by_policy(m, V.POLICY_NOT_APPLICABLE)
        entry = m['test_cases'][i]
        path = self.tree.evidence_root / entry['evidence_path']
        original = path.read_bytes()
        doc = json.loads(original.decode('utf-8'))
        doc['event_observation_policy'] = V.POLICY_REQUIRED_EMPTY
        # Manifest の evidence_manifest_hash は更新しない。
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n',
                        encoding='utf-8')
        try:
            self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                              contains='hash mismatch')
        finally:
            path.write_bytes(original)

    def test_056_removing_the_policy_from_evidence_is_detected(self):
        """Evidence から Policy を消して拒否されること。

        消して通るなら、Policy を書かない Evidence が免除を受けられる。
        """
        m = self.tree.mutate()
        i = self._case_index_by_policy(m, V.POLICY_NOT_APPLICABLE)
        entry = self._swap_case_evidence(m, i)
        path = self.tree.evidence_root / entry['evidence_path']
        doc = json.loads(path.read_text(encoding='utf-8'))
        doc.pop('event_observation_policy')
        entry['evidence_manifest_hash'] = write_json(path, doc)
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='!= snapshot')

    def test_057_unclassified_case_cannot_reach_release_go(self):
        """Policy 未宣言の Case を含む Scope が Release GO に到達しないこと。

        実 Registry には未宣言の Case が残っていない（Owner が決めた）。
        **その在庫に依存しない。** 1件だけ宣言を外した Snapshot を組み、
        宣言が無いと GO にならないことを確かめる。

        在庫を前提にすると、在庫が変わるだけで規則の検査が消える。
        検査したいのは「宣言が無ければ通さない」という規則である。
        """
        stripped = copy.deepcopy(snapshot_with_every_policy_declared())
        target = next(
            key for key in (f'{t}/{c}' for t, c in stripped['scopes'][SCOPE]['required_cases'])
            if stripped['expectations'][key].get('event_observation_policy') is not None
            and not stripped['expectations'][key]['expected_event_sequence']
        )
        stripped['expectations'][target]['event_observation_policy'] = None
        reseal(stripped)

        tmp = tempfile.mkdtemp(prefix='at-verifier-unclassified-')
        try:
            tree = EvidenceTree(Path(tmp), snapshot=stripped)
            report, code = tree.run()
            self.assert_no_go(report, code)
            blob = json.dumps(report, ensure_ascii=False)
            self.assertIn(target, blob)
            self.assertIn('no event_observation_policy', blob)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_058_only_the_active_evidence_version_is_accepted(self):
        """現行 Release 判定へ 1.2 の Evidence を出せないこと。"""
        m = self.tree.mutate()
        i = self._case_index_by_policy(m, V.POLICY_NOT_APPLICABLE)
        self._swap_case_evidence(m, i, evidence_schema_version='1.2')
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='evidence_schema_version must be')

    # --- AT-VERIFIER-059〜065: Unit Evidence Area（設計書 v1.16 §26.2）---

    def _swap_area_evidence(self, m, area, **fields):
        """1 領域の Evidence を差し替えた変種を置く。共有Fixtureは壊さない。"""
        entry = next(a for a in m['required_evidence_areas'] if a['area'] == area)
        source = self.tree.evidence_root / entry['evidence_path']
        doc = json.loads(source.read_text(encoding='utf-8'))
        doc['summary'] = {**doc['summary'], **fields}
        rel = f'variant-area/{area}.json'
        entry['evidence_manifest_hash'] = write_json(self.tree.evidence_root / rel, doc)
        entry['evidence_path'] = rel
        return doc

    def test_059_unit_area_is_accepted_when_it_covers_every_unit_case(self):
        """Unit 領域が Unit Case を過不足なく名指ししていれば通ること。"""
        m = self.tree.mutate()
        entry = next(a for a in m['required_evidence_areas']
                     if a['area'] == 'unit_case_suite')
        doc = json.loads(
            (self.tree.evidence_root / entry['evidence_path']).read_text(encoding='utf-8'))
        expected = sorted(
            key for key in (f'{t}/{c}' for t, c in self.tree.scope['required_cases'])
            if self.tree.snap['expectations'][key].get(
                'event_observation_policy') == V.POLICY_NOT_APPLICABLE)
        self.assertEqual(doc['summary']['case_ids'], expected)
        self.assertEqual(doc['summary']['unit_case_count'], len(expected))
        self.assertIs(doc['summary']['human_measured'], False)
        self.assert_go(*self.tree.run(m), msg='Unit 領域の正常系')

    def test_060_unit_area_missing_a_case_is_detected(self):
        """名指しから Unit Case が1件抜けたら落ちること。

        抜けた Case は検証されていないのに、領域は充足したことになる。
        """
        m = self.tree.mutate()
        entry = next(a for a in m['required_evidence_areas']
                     if a['area'] == 'unit_case_suite')
        doc = json.loads(
            (self.tree.evidence_root / entry['evidence_path']).read_text(encoding='utf-8'))
        remaining = doc['summary']['case_ids'][1:]
        self._swap_area_evidence(
            m, 'unit_case_suite', case_ids=remaining, unit_case_count=len(remaining),
            case_evidence_hashes=doc['summary']['case_evidence_hashes'][1:])
        self.assert_no_go(*self.tree.run(m), contains='unit case not covered')

    def test_061_unit_area_with_an_unknown_case_is_detected(self):
        """Snapshot に無い Case を混ぜたら落ちること。"""
        m = self.tree.mutate()
        entry = next(a for a in m['required_evidence_areas']
                     if a['area'] == 'unit_case_suite')
        doc = json.loads(
            (self.tree.evidence_root / entry['evidence_path']).read_text(encoding='utf-8'))
        mixed = [*doc['summary']['case_ids'], 'AT-NOT-A-CASE-001/NOPE']
        self._swap_area_evidence(
            m, 'unit_case_suite', case_ids=mixed, unit_case_count=len(mixed))
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='is not a unit case')

    def test_062_unit_area_may_not_claim_an_orchestration_case(self):
        """Orchestration Case を Unit として数えたら落ちること。

        Ledger を観測して作用を確かめた Case と、State と Error だけで閉じた
        Case を同じ重みで数えない。
        """
        m = self.tree.mutate()
        entry = next(a for a in m['required_evidence_areas']
                     if a['area'] == 'unit_case_suite')
        doc = json.loads(
            (self.tree.evidence_root / entry['evidence_path']).read_text(encoding='utf-8'))
        orchestration = next(
            key for key in (f'{t}/{c}' for t, c in self.tree.scope['required_cases'])
            if self.tree.snap['expectations'][key].get(
                'event_observation_policy') != V.POLICY_NOT_APPLICABLE)
        mixed = sorted([*doc['summary']['case_ids'], orchestration])
        self._swap_area_evidence(
            m, 'unit_case_suite', case_ids=mixed, unit_case_count=len(mixed))
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains=f'is not a unit case: {orchestration}')

    def test_063_unit_area_hash_must_be_bound_to_the_manifest(self):
        """名指しした Evidence Hash が Manifest の束縛と違えば落ちること。

        違う Hash を書けば、領域は別物の Evidence の上に立つ。
        """
        m = self.tree.mutate()
        entry = next(a for a in m['required_evidence_areas']
                     if a['area'] == 'unit_case_suite')
        doc = json.loads(
            (self.tree.evidence_root / entry['evidence_path']).read_text(encoding='utf-8'))
        # 件数は合わせる。件数不一致で先に落ちると、束縛の検査まで届かない。
        bogus = [sha256_bytes(f'not-the-evidence-{i}'.encode())
                 for i in range(len(doc['summary']['case_evidence_hashes']))]
        self._swap_area_evidence(m, 'unit_case_suite', case_evidence_hashes=bogus)
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='evidence hash not in manifest')

    def test_064_unit_area_may_not_be_human_measured(self):
        """Unit 領域は機械計測である。人手計測を名乗らせない。"""
        m = self.tree.mutate()
        self._swap_area_evidence(m, 'unit_case_suite', human_measured=True)
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='human_measured must be false')

    def test_065_unit_area_reports_failing_cases(self):
        """根拠 Case が落ちているのに領域だけ PASS にできないこと。"""
        m = self.tree.mutate()
        entry = next(a for a in m['required_evidence_areas']
                     if a['area'] == 'unit_case_suite')
        doc = json.loads(
            (self.tree.evidence_root / entry['evidence_path']).read_text(encoding='utf-8'))
        self._swap_area_evidence(
            m, 'unit_case_suite', failing_case_ids=[doc['summary']['case_ids'][0]])
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='failing cases')

    def test_043_self_test_floor_matches_actual_test_count(self):
        """Verifierの自己試験下限が、実際の試験件数と一致すること。

        下限が実件数より低いと、一部だけ走らせたEvidenceが受理される。
        高いと、正しく全件走らせたEvidenceが拒否される。
        件数を手で書かず、ここで突き合わせる（不変条件#18）。
        """
        actual = len([name for name in dir(type(self)) if name.startswith('test_')])
        self.assertEqual(
            V.SELF_TEST_MIN_TESTS, actual,
            f'自己試験は{actual}件だが下限は{V.SELF_TEST_MIN_TESTS}件になっている。'
            'verify_runtime_go.py の SELF_TEST_MIN_TESTS を合わせること')

    # --- AT-VERIFIER-022: v1.0で成立した偽装が再現しない ---
    def test_022_v16_forgery_no_longer_passes(self):
        """ダミー1ファイルを全Case/Gateへ向け、Areaを空にする v1.0偽装の再現。"""
        dummy = self.tree.evidence_root / 'dummy.json'
        h = write_json(dummy, {'note': 'this is not evidence of anything'})
        m = self.tree.mutate()
        for c in m['test_cases']:
            c['evidence_path'] = 'dummy.json'
            c['evidence_manifest_hash'] = h
            c['input_fixture_hash'] = 'sha256:' + '0' * 64
        for g in m['gates']:
            g['evidence_path'] = 'dummy.json'
            g['evidence_manifest_hash'] = h
        m['required_evidence_areas'] = []
        m['runtime_environment']['python_version'] = '2.7.0'
        try:
            self.assert_no_go(*self.tree.run(m))
        finally:
            dummy.unlink(missing_ok=True)

    # --- AT-VERIFIER-023: Case/Gate/Area間でもEvidence流用禁止 ---
    def test_023_cross_category_evidence_reuse_rejected(self):
        m = self.tree.mutate()
        case, gate = m['test_cases'][0], m['gates'][0]
        area = next(a for a in m['required_evidence_areas'] if a['area'] == 'spec_lint')
        path = self.tree.evidence_root / case['evidence_path']
        doc = json.loads(path.read_text(encoding='utf-8'))
        gate_doc = json.loads(
            (self.tree.evidence_root / gate['evidence_path']).read_text(encoding='utf-8'))
        doc.update({'gate_id': gate['gate_id'], 'case_refs': gate_doc['case_refs'],
                    'area': area['area'], 'summary': {'checked': True}})
        h = write_json(path, doc)
        for entry in (case, gate, area):
            entry['evidence_path'] = case['evidence_path']
            entry['evidence_manifest_hash'] = h
        try:
            self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                              contains='evidence reused from')
        finally:
            self.tree = EvidenceTree(Path(self.tmp))

    # --- AT-VERIFIER-024: Area必須Fieldを省略できない ---
    def test_024_area_contract_fields_required(self):
        m = self.tree.mutate()
        area = next(a for a in m['required_evidence_areas'] if a['area'] == 'spec_lint')
        path = self.tree.evidence_root / area['evidence_path']
        doc = json.loads(path.read_text(encoding='utf-8'))
        for field in ('summary', 'test_run_id', 'recorded_at'):
            doc.pop(field, None)
        area['evidence_manifest_hash'] = write_json(path, doc)
        try:
            self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_MISSING,
                              contains='spec_lint.summary')
        finally:
            self.tree = EvidenceTree(Path(self.tmp))

    # --- AT-VERIFIER-025: 全証跡PASS時のDecision矛盾を拒否 ---
    def test_025_release_decision_contradiction_rejected(self):
        m = self.tree.mutate()
        m['release_decision'] = 'RUNTIME_NO_GO'
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='release_decision must be RUNTIME_GO')

    # --- AT-VERIFIER-026: Crash/I/O Evidenceの耐久性Tier必須 ---
    def test_026_durability_tier_required(self):
        m = self.tree.mutate()
        entry = next(c for c in m['test_cases']
                     if self.tree.snap['expectations'][
                         f'{c["test_id"]}/{c["case_id"]}'].get('durability_tier'))
        path = self.tree.evidence_root / entry['evidence_path']
        doc = json.loads(path.read_text(encoding='utf-8'))
        doc.pop('durability_tier')
        entry['evidence_manifest_hash'] = write_json(path, doc)
        try:
            self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                              contains='durability_tier')
        finally:
            self.tree = EvidenceTree(Path(self.tmp))

    # --- AT-VERIFIER-027/028: FixtureとRaw resultの実Bytes束縛 ---
    def test_027_fixture_file_required(self):
        m = self.tree.mutate()
        entry = m['test_cases'][0]
        doc = json.loads((self.tree.evidence_root / entry['evidence_path']).read_text())
        (self.tree.evidence_root / doc['input_fixture_path']).unlink()
        try:
            self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_MISSING,
                              contains='input_fixture')
        finally:
            self.tree = EvidenceTree(Path(self.tmp))

    def test_028_raw_result_hash_mismatch_rejected(self):
        m = self.tree.mutate()
        entry = m['test_cases'][0]
        doc = json.loads((self.tree.evidence_root / entry['evidence_path']).read_text())
        (self.tree.evidence_root / doc['raw_result_path']).write_bytes(b'tampered')
        try:
            self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                              contains='raw_result: hash mismatch')
        finally:
            self.tree = EvidenceTree(Path(self.tmp))

    # --- AT-VERIFIER-029: Environment Evidenceの/mnt自己申告を拒否 ---
    def test_029_environment_content_checked(self):
        m = self.tree.mutate()
        env = m['runtime_environment']
        path = self.tree.evidence_root / env['environment_manifest_path']
        doc = json.loads(path.read_text(encoding='utf-8'))
        doc['workspace_root'] = '/mnt/c/forbidden'
        new_hash = write_json(path, doc)
        env['environment_manifest_hash'] = new_hash
        area = next(a for a in m['required_evidence_areas'] if a['area'] == 'environment')
        area['evidence_manifest_hash'] = new_hash
        self.assert_no_go(*self.tree.run(m), expect_code=V.EXIT_FAIL,
                          contains='workspace_root is on /mnt')
        self.tree = EvidenceTree(Path(self.tmp))

    # --- AT-VERIFIER-030: Gate 0件の後続PhaseはRelease Scopeにできない ---
    def test_030_phase_without_gates_is_not_release_enabled(self):
        report, code = self.tree.run(scope='MVP0-B')
        self.assert_no_go(report, code, expect_code=V.EXIT_INPUT_INVALID,
                          contains='without registered gates')

    # --- AT-VERIFIER-031: Release Manifestの自己HashとReport束縛 ---
    def test_031_release_manifest_binds_report(self):
        report, code = self.tree.run()
        self.assert_go(report, code)
        release = V.build_release_manifest(report, self.tree.manifest)
        claimed = release.pop('release_manifest_hash')
        self.assertEqual(
            claimed, V.domain_hash('FDE-HARNESS/release-manifest/1/', release))
        self.assertEqual(release['verification_report_hash'],
                         report['verification_report_hash'])


def emit_evidence(out_dir: Path, manifest_path: Path) -> int:
    """verifier_self_test 領域のEvidence JSONを生成する。"""
    suite = unittest.TestLoader().loadTestsFromTestCase(VerifierContract)
    buf = unittest.TextTestRunner(stream=open(__import__('os').devnull, 'w'),
                                  verbosity=0).run(suite)
    status = 'PASS' if buf.wasSuccessful() else 'FAIL'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    recorded_at = dt.datetime.now(dt.timezone.utc).replace(
        microsecond=0).isoformat().replace('+00:00', 'Z')
    payload = {
        'evidence_schema_version': V.EVIDENCE_SCHEMA_VERSION,
        'release_scope': manifest['release_scope'],
        'area': 'verifier_self_test',
        'status': status,
        'implementation_commit_sha': manifest.get('implementation_commit_sha'),
        'runtime_environment_hash': manifest.get('runtime_environment', {}).get(
            'environment_manifest_hash'),
        'schema_set_hash': manifest.get('schema_set_hash'),
        'migration_head': manifest.get('migration_head'),
        'producer': 'tests/test_verify_runtime_go.py',
        'started_at': recorded_at,
        'summary': {
            'verifier_source_sha256': V.sha256_file(KIT / 'verify_runtime_go.py'),
            'tests_run': buf.testsRun,
            'failures': len(buf.failures),
            'errors': len(buf.errors),
        },
        'test_run_id': 'verifier-self-test',
        'recorded_at': recorded_at,
    }
    path = out_dir / 'tooling/verifier-self-test-report.json'
    write_json(path, payload)
    print(f'wrote {path} status={status} tests={buf.testsRun}')
    return 0 if buf.wasSuccessful() else 1


if __name__ == '__main__':
    if '--emit-evidence' in sys.argv:
        i = sys.argv.index('--emit-evidence')
        if '--manifest' not in sys.argv:
            raise SystemExit('--manifest is required with --emit-evidence')
        j = sys.argv.index('--manifest')
        raise SystemExit(emit_evidence(Path(sys.argv[i + 1]), Path(sys.argv[j + 1])))
    unittest.main(verbosity=2)
