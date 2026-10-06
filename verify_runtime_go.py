#!/usr/bin/env python3
"""Runtime GO Verifier v1.3（設計書 v1.12 §28）。

v1.0からの変更点は MIGRATION-v1.6-to-v1.7.md を参照。要約すると、
v1.0は「Manifestに書かれた文字列の形式」しか見ておらず、ダミー1ファイルで
RUNTIME_GOを取得できた。v1.1の対策に加え、v1.2は次を強制する。

  1. Release Scope対応。必要Case/Gate集合はRegistry Snapshotから導出する。
  2. Evidence領域の必須集合・件数検証。空配列で要件が消えない。
     v1.3: 必須集合は Release Scope 別。snapshot.scopes[<scope>]
     .required_evidence_areas から導出し、領域定義は snapshot.evidence_areas
     を引く。Scope未指定・未知Scopeで全領域へフォールバックしない。
  3. evidence_path のroot脱出検査を cases / gates / areas で統一。
  4. Evidence Fileの一意性検証。同一Evidenceの使い回しを拒否する。
  5. Evidence Fileの内容検証。Registryの期待値と突合する。
  6. Verifier自身のSource Hash、Manifest Hash、Registry Snapshot Hashを
     Report本体へ含め、verification_report_hash がそれらを覆う。
  7. 件数はRegistry由来。コードへハードコードしない。
  8. python_version >= 3.11 を実際に比較する（ADR-003）。
  9. test_manifest_hash をRegistry導出値と突合する。

本ファイルは stdlib のみに依存する。GO判定器へ第三者依存を持ち込まない。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

VERIFIER_VERSION = '1.3'
# 自己試験の最小件数。`tests/test_verify_runtime_go.py` の実件数と一致させる。
# 下限を実件数より低いまま置くと、一部だけ走らせたEvidenceを受理してしまう。
# ずれは同Fileの test_043 が落として知らせる。
SELF_TEST_MIN_TESTS = 65
MANIFEST_VERSION = '1.2'
# Case/Gate/領域Evidenceの現行版。1.2 と 2.0 は read_only であり、Release判定の
# 入力として受理しない（設計書 v1.15 §15.11.2、Owner Decision DEC-EVT-PRED-B）。
# 受理しないことと消すことは別で、旧版のDevelopment Evidenceは履歴として残す。
EVIDENCE_SCHEMA_VERSION = '3.0'

# Evidence 2.0 の語彙（設計書 v1.15 §26.2.1）。
EVIDENCE_KINDS = ('UNIT', 'ORCHESTRATION')
OBS_OBSERVED = 'OBSERVED'
OBS_OBSERVED_EMPTY = 'OBSERVED_EMPTY'
OBS_NOT_APPLICABLE = 'NOT_APPLICABLE'
EVENT_OBSERVATIONS = (OBS_OBSERVED, OBS_OBSERVED_EMPTY, OBS_NOT_APPLICABLE)
POLICY_REQUIRED_EMPTY = 'REQUIRED_EMPTY'
POLICY_NOT_APPLICABLE = 'NOT_APPLICABLE'
EVENT_OBSERVATION_POLICIES = (POLICY_REQUIRED_EMPTY, POLICY_NOT_APPLICABLE)
# Policy と 層／観測有無 の対応。ずれた組合せを1つも許さない（§26.2.1）。
POLICY_SHAPE = {
    POLICY_REQUIRED_EMPTY: ('ORCHESTRATION', OBS_OBSERVED_EMPTY),
    POLICY_NOT_APPLICABLE: ('UNIT', OBS_NOT_APPLICABLE),
}
HASH_PROFILE = 'json-sorted-compact-utf8/1'

SHA256_RE = re.compile(r'^sha256:[0-9a-f]{64}$')
GIT_SHA_RE = re.compile(r'^[0-9a-f]{40}$')
PASS = 'PASS'
MINIMUM_PYTHON = (3, 11)

EXIT_OK = 0
EXIT_MISSING = 2
EXIT_FAIL = 3
EXIT_INPUT_INVALID = 4


class Findings:
    """errors=積極的な不一致 / missing=未提供。両方が空のときだけGO。"""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.missing: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def miss(self, msg: str) -> None:
        self.missing.append(msg)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.missing


def canonical(obj: Any) -> bytes:
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(',', ':')
    ).encode('utf-8')


def domain_hash(prefix: str, obj: Any) -> str:
    return 'sha256:' + hashlib.sha256(prefix.encode('utf-8') + canonical(obj)).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return 'sha256:' + h.hexdigest()


def load_json(path: Path) -> Any:
    with path.open('r', encoding='utf-8') as f:
        return json.load(f)


def is_hash(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA256_RE.fullmatch(value))


def resolve_evidence(
    root: Path, rel: Any, label: str, f: Findings
) -> Path | None:
    """evidence_root配下に必ず収まる形でPathを解決する。

    v1.0はこの検査を test_cases にしか適用していなかったため、gates と
    required_evidence_areas は絶対Path指定でroot外のFileを参照できた。
    """
    if not rel:
        f.miss(f'{label}.evidence_path')
        return None
    if not isinstance(rel, str):
        f.error(f'{label}.evidence_path is not a string')
        return None
    if Path(rel).is_absolute() or rel.startswith('\\') or ':' in rel[:3]:
        f.error(f'{label}: evidence_path must be relative to evidence-root: {rel}')
        return None
    root_resolved = root.resolve()
    full = (root_resolved / rel).resolve()
    try:
        full.relative_to(root_resolved)
    except ValueError:
        f.error(f'{label}: evidence path escapes evidence-root: {rel}')
        return None
    if not full.is_file():
        f.miss(f'{label}: evidence file missing: {rel}')
        return None
    return full


def check_evidence_file(
    root: Path, entry: dict, label: str, declared_hash: Any, f: Findings
) -> dict | None:
    """Path解決 → Bytes Hash照合 → JSONとしてパース、までを行う。

    v1.0はHashを取るだけで中身を一切見なかったため、内容が `{}` の
    ファイルでもPASS証跡として成立した。
    """
    if not is_hash(declared_hash):
        f.miss(f'{label}.evidence_manifest_hash')
    full = resolve_evidence(root, entry.get('evidence_path'), label, f)
    if full is None:
        return None
    actual = sha256_file(full)
    if is_hash(declared_hash) and actual != declared_hash:
        f.error(f'{label}: evidence hash mismatch (actual={actual})')
        return None
    try:
        doc = load_json(full)
    except Exception as exc:
        f.error(f'{label}: evidence file is not valid JSON: {exc}')
        return None
    if not isinstance(doc, dict):
        f.error(f'{label}: evidence file must be a JSON object')
        return None
    if doc.get('evidence_schema_version') != EVIDENCE_SCHEMA_VERSION:
        f.error(f'{label}: evidence_schema_version must be {EVIDENCE_SCHEMA_VERSION}')
    return doc


def check_bound_file(root: Path, rel: Any, declared_hash: Any, label: str,
                     f: Findings) -> Path | None:
    """FixtureやRaw resultをEvidenceと同じroot内の実Bytesへ束縛する。"""
    if not is_hash(declared_hash):
        f.miss(f'{label}.hash')
    path = resolve_evidence(root, rel, label, f)
    if path is not None and is_hash(declared_hash):
        actual = sha256_file(path)
        if actual != declared_hash:
            f.error(f'{label}: hash mismatch (actual={actual})')
    return path


def valid_timestamp(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', value))


def verify_common_evidence(doc: dict, manifest: dict, label: str,
                           f: Findings) -> None:
    """全Evidenceを同一Source/Environment/Schema/Migrationへ束縛する。"""
    checks = [
        ('implementation_commit_sha', manifest.get('implementation_commit_sha')),
        ('schema_set_hash', manifest.get('schema_set_hash')),
        ('migration_head', manifest.get('migration_head')),
    ]
    if doc.get('area') != 'environment':
        checks.append(('runtime_environment_hash',
                       (manifest.get('runtime_environment') or {}).get(
                           'environment_manifest_hash')))
    for field, expected in checks:
        if doc.get(field) != expected:
            f.error(f'{label}: evidence {field} is not bound to manifest')
    if not isinstance(doc.get('producer'), str) or not doc['producer'].strip():
        f.miss(f'{label}.producer')
    if not doc.get('test_run_id'):
        f.miss(f'{label}.test_run_id')
    for field in ('started_at', 'recorded_at'):
        if not valid_timestamp(doc.get(field)):
            f.miss(f'{label}.{field}')


def check_no_reuse(seen: dict[str, str], key: str, h: Any, label: str, f: Findings) -> None:
    """同一Evidence Fileを複数の判定単位へ流用させない。"""
    if not is_hash(h):
        return
    prev = seen.get(h)
    if prev is not None and prev != key:
        f.error(f'{label}: evidence reused from {prev} (same evidence_manifest_hash)')
    seen.setdefault(h, key)


def parse_python_version(value: Any) -> tuple[int, ...] | None:
    if not isinstance(value, str):
        return None
    m = re.match(r'^(\d+)\.(\d+)', value.strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def verify_environment(m: dict, root: Path, f: Findings) -> None:
    env = m.get('runtime_environment') or {}
    if not isinstance(env, dict):
        f.error('runtime_environment must be an object')
        return
    if env.get('is_wsl2') is not True:
        f.miss('runtime_environment.is_wsl2=true')
    if env.get('workspace_on_linux_native_fs') is not True:
        f.miss('runtime_environment.workspace_on_linux_native_fs=true')

    parsed = parse_python_version(env.get('python_version'))
    if parsed is None:
        f.miss('runtime_environment.python_version')
    elif parsed < MINIMUM_PYTHON:
        # v1.0は真偽値としてしか見ておらず "2.7.0" が合格していた。
        f.error(
            f'runtime_environment.python_version {env["python_version"]} '
            f'< required {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]} (ADR-003)'
        )

    if not is_hash(env.get('environment_manifest_hash')):
        f.miss('runtime_environment.environment_manifest_hash')
    resolved = resolve_evidence(
        root, env.get('environment_manifest_path'),
        'runtime_environment.environment_manifest', f)
    if resolved is not None and is_hash(env.get('environment_manifest_hash')):
        if sha256_file(resolved) != env['environment_manifest_hash']:
            f.error('runtime_environment.environment_manifest hash mismatch')
            return
        try:
            doc = load_json(resolved)
        except Exception as exc:
            f.error(f'runtime environment evidence is not valid JSON: {exc}')
            return
        if not isinstance(doc, dict):
            f.error('runtime environment evidence must be an object')
            return
        expected = {
            'evidence_schema_version': EVIDENCE_SCHEMA_VERSION,
            'area': 'environment',
            'status': PASS,
            'release_scope': m.get('release_scope'),
            'is_wsl2': True,
            'workspace_on_linux_native_fs': True,
            'python_version': env.get('python_version'),
            'implementation_commit_sha': m.get('implementation_commit_sha'),
            'schema_set_hash': m.get('schema_set_hash'),
            'migration_head': m.get('migration_head'),
        }
        for field, want in expected.items():
            if doc.get(field) != want:
                f.error(f'runtime environment evidence {field}={doc.get(field)!r} != {want!r}')
        workspace = doc.get('workspace_root')
        if not isinstance(workspace, str) or not workspace.startswith('/'):
            f.miss('runtime environment evidence workspace_root')
        elif workspace == '/mnt' or workspace.startswith('/mnt/'):
            f.error('runtime environment evidence workspace_root is on /mnt/*')
        for field in ('mount_point', 'filesystem_type', 'mount_id'):
            if doc.get(field) in (None, ''):
                f.miss(f'runtime environment evidence {field}')
        for field in ('mountinfo_hash', 'python_lock_hash'):
            if not is_hash(doc.get(field)):
                f.miss(f'runtime environment evidence {field}')
        if not doc.get('test_run_id'):
            f.miss('runtime environment evidence test_run_id')
        if not isinstance(doc.get('producer'), str) or not doc['producer'].strip():
            f.miss('runtime environment evidence producer')
        for field in ('started_at', 'recorded_at'):
            if not valid_timestamp(doc.get(field)):
                f.miss(f'runtime environment evidence {field}')


def verify_cases(
    m: dict, snap: dict, scope: str, root: Path, f: Findings,
    seen: dict[str, str],
) -> None:
    required = {tuple(pair) for pair in snap['scopes'][scope]['required_cases']}
    expectations = snap['expectations']
    cases = m.get('test_cases')
    if not isinstance(cases, list):
        f.error('test_cases must be an array')
        return
    if not all(isinstance(item, dict) for item in cases):
        f.error('every test_cases item must be an object')
        return

    present = [(c.get('test_id'), c.get('case_id')) for c in cases]
    if len(set(present)) != len(present):
        f.error('duplicate test_id/case_id in test_cases')

    extra = sorted(set(present) - required)
    absent = sorted(required - set(present))
    for t, c in extra:
        f.error(f'test case out of release scope {scope}: {t}/{c}')
    for t, c in absent:
        f.miss(f'required test case not in manifest: {t}/{c}')

    declared = m.get('expected_case_count')
    if declared != len(required):
        f.error(f'expected_case_count must be {len(required)} for {scope}, got {declared}')
    declared_ids = m.get('expected_test_id_count')
    if declared_ids != snap['scopes'][scope]['required_test_id_count']:
        f.error(
            f'expected_test_id_count must be '
            f'{snap["scopes"][scope]["required_test_id_count"]} for {scope}, '
            f'got {declared_ids}'
        )

    for c in cases:
        key = f'{c.get("test_id")}/{c.get("case_id")}'
        if (c.get('test_id'), c.get('case_id')) not in required:
            continue
        if c.get('status') != PASS:
            f.miss(f'case not PASS: {key}')
        if not is_hash(c.get('input_fixture_hash')):
            f.miss(f'{key}.input_fixture_hash')

        check_no_reuse(seen, key, c.get('evidence_manifest_hash'), key, f)
        doc = check_evidence_file(root, c, key, c.get('evidence_manifest_hash'), f)
        if doc is None:
            continue
        verify_common_evidence(doc, m, key, f)

        exp = expectations.get(key)
        if exp is None:
            f.error(f'{key}: no expectation in registry')
            continue
        checks = [
            ('release_scope', doc.get('release_scope'), scope),
            ('test_id', doc.get('test_id'), c.get('test_id')),
            ('case_id', doc.get('case_id'), c.get('case_id')),
            ('status', doc.get('status'), c.get('status')),
            ('input_fixture_hash', doc.get('input_fixture_hash'),
             c.get('input_fixture_hash')),
            ('expectation_descriptor_hash', doc.get('expectation_descriptor_hash'),
             exp['expectation_descriptor_hash']),
            ('observed_subject_type', doc.get('observed_subject_type'),
             exp['expected_subject_type']),
            ('observed_state', doc.get('observed_state'), exp['expected_state']),
            ('observed_error_code', doc.get('observed_error_code'),
             exp['expected_error_code']),
        ]
        for field, actual, want in checks:
            if actual != want:
                f.error(f'{key}: evidence {field}={actual!r} != expected {want!r}')

        # Event列は素の等値では見ない。`null` と `[]` の意味が違うためである。
        check_event_observation(doc, exp, key, f)

        check_bound_file(root, doc.get('input_fixture_path'),
                         c.get('input_fixture_hash'), f'{key}.input_fixture', f)
        check_bound_file(root, doc.get('raw_result_path'),
                         doc.get('raw_result_hash'), f'{key}.raw_result', f)
        if not is_hash(doc.get('runner_source_hash')):
            f.miss(f'{key}.runner_source_hash')
        command = doc.get('command')
        if not isinstance(command, list) or not command or not all(
                isinstance(arg, str) for arg in command):
            f.miss(f'{key}.command(list[str])')
        if doc.get('exit_code') != 0:
            f.error(f'{key}: exit_code must be 0, got {doc.get("exit_code")!r}')
        if doc.get('durability_tier') != exp.get('durability_tier'):
            f.error(
                f'{key}: durability_tier={doc.get("durability_tier")!r} '
                f'!= expected {exp.get("durability_tier")!r}'
            )

        assertions = doc.get('assertions')
        if not isinstance(assertions, list) or not assertions:
            f.miss(f'{key}.assertions')
        else:
            for a in assertions:
                if not isinstance(a, dict) or a.get('result') is not True:
                    f.error(f'{key}: assertion not satisfied: {a}')



def check_event_observation(doc: Any, exp: Any, key: str, f: 'Findings') -> None:
    """層・Event観測有無・Policy の整合を見る（設計書 v1.16 §26.2.1）。

    **免除の根拠は Release 束縛の Snapshot だけである。** ローカルの Registry は
    読まない。読めば、Registry を書き換えた木で走らせるだけで免除を作れる。

    * 期待Event列が空なのに Snapshot へ Policy が無い Case は拒否する。
      `REQUIRED_EMPTY` と `NOT_APPLICABLE` のどちらかへ倒さない。
    * Evidence が写した Policy が Snapshot と食い違えば拒否する。
    * Policy は層と観測有無を一意に決める。ずれた組合せを1つも許さない。
    * `NOT_APPLICABLE` は `null`、`OBSERVED_EMPTY` は `[]` を要求する。
      **空配列を非該当の代用にしない。** 見て0件と見ていないは意味が正反対である。
    """
    kind = doc.get('evidence_kind')
    observation = doc.get('event_observation')
    sequence = doc.get('observed_event_sequence')
    want = list(exp['expected_event_sequence'] or [])
    snapshot_policy = exp.get('event_observation_policy')
    declared_policy = doc.get('event_observation_policy')

    if snapshot_policy is not None and snapshot_policy not in EVENT_OBSERVATION_POLICIES:
        f.error(
            f'{key}: snapshot event_observation_policy={snapshot_policy!r} is not '
            f'one of {list(EVENT_OBSERVATION_POLICIES)}')
        return
    if declared_policy is not None and declared_policy not in EVENT_OBSERVATION_POLICIES:
        f.error(
            f'{key}: evidence event_observation_policy={declared_policy!r} is not '
            f'one of {list(EVENT_OBSERVATION_POLICIES)}')
        return
    if declared_policy != snapshot_policy:
        # Evidence 側だけで免除を名乗らせない。
        f.error(
            f'{key}: evidence event_observation_policy={declared_policy!r} != '
            f'snapshot {snapshot_policy!r}')
        return
    if snapshot_policy is None and not want:
        # 空の期待列と Policy 欠落。どちらの意味か決まらないので止める。
        f.error(
            f'{key}: expected_event_sequence is empty but no event_observation_policy '
            f'is bound in the release snapshot')
        return
    if snapshot_policy is not None and want:
        f.error(
            f'{key}: event_observation_policy={snapshot_policy!r} contradicts a '
            f'non-empty expected event sequence {want!r}')
        return

    if kind not in EVIDENCE_KINDS:
        f.error(f'{key}: evidence_kind={kind!r} must be one of {list(EVIDENCE_KINDS)}')
        return
    if observation not in EVENT_OBSERVATIONS:
        f.error(
            f'{key}: event_observation={observation!r} must be one of '
            f'{list(EVENT_OBSERVATIONS)}')
        return

    if kind == 'ORCHESTRATION' and observation == OBS_NOT_APPLICABLE:
        # 作用を主張する層が Ledger 観測の免除を名乗っている。
        f.error(f'{key}: ORCHESTRATION evidence must not claim {OBS_NOT_APPLICABLE}')
        return

    if snapshot_policy is not None:
        want_kind, want_observation = POLICY_SHAPE[snapshot_policy]
        if kind != want_kind:
            f.error(
                f'{key}: policy {snapshot_policy} requires evidence_kind={want_kind}, '
                f'got {kind!r}')
            return
        if observation != want_observation:
            f.error(
                f'{key}: policy {snapshot_policy} requires '
                f'event_observation={want_observation}, got {observation!r}')
            return

    if observation == OBS_NOT_APPLICABLE:
        if sequence is not None:
            f.error(
                f'{key}: {OBS_NOT_APPLICABLE} requires observed_event_sequence=null, '
                f'got {sequence!r}')
        if want:
            # 期待列が非空なら作用を主張している。免除と両立しない。
            f.error(
                f'{key}: {OBS_NOT_APPLICABLE} contradicts a non-empty expected '
                f'event sequence {want!r}')
        return

    if observation == OBS_OBSERVED_EMPTY:
        if sequence != []:
            f.error(
                f'{key}: {OBS_OBSERVED_EMPTY} requires observed_event_sequence=[], '
                f'got {sequence!r}')
        if want:
            f.error(
                f'{key}: {OBS_OBSERVED_EMPTY} contradicts expected event sequence '
                f'{want!r}')
        return

    # OBSERVED
    if not isinstance(sequence, list) or not sequence:
        f.error(
            f'{key}: {OBS_OBSERVED} requires a non-empty observed_event_sequence, '
            f'got {sequence!r}')
        return
    if sequence != want:
        f.error(
            f'{key}: evidence observed_event_sequence={sequence!r} != expected {want!r}')

def verify_gates(m: dict, snap: dict, scope: str, root: Path, f: Findings,
                 seen: dict[str, str]) -> None:
    required = set(snap['scopes'][scope]['required_gate_ids'])
    gates = m.get('gates')
    if not isinstance(gates, list):
        f.error('gates must be an array')
        return
    if not all(isinstance(item, dict) for item in gates):
        f.error('every gates item must be an object')
        return

    present = [g.get('gate_id') for g in gates]
    if len(set(present)) != len(present):
        f.error('duplicate gate_id')
    for gid in sorted(set(present) - required):
        f.error(f'gate out of release scope {scope}: {gid}')
    for gid in sorted(required - set(present)):
        f.miss(f'required gate not in manifest: {gid}')

    if m.get('expected_gate_count') != len(required):
        f.error(
            f'expected_gate_count must be {len(required)} for {scope}, '
            f'got {m.get("expected_gate_count")}'
        )

    passing_cases = {
        (c.get('test_id'), c.get('case_id'))
        for c in (m.get('test_cases') or [])
        if c.get('status') == PASS
    }
    for g in gates:
        gid = str(g.get('gate_id'))
        if gid not in required:
            continue
        if g.get('status') != PASS:
            f.miss(f'gate not PASS: {gid}')
        check_no_reuse(seen, gid, g.get('evidence_manifest_hash'), gid, f)
        doc = check_evidence_file(root, g, gid, g.get('evidence_manifest_hash'), f)
        if doc is None:
            continue
        verify_common_evidence(doc, m, gid, f)
        if doc.get('gate_id') != gid:
            f.error(f'{gid}: evidence gate_id={doc.get("gate_id")!r}')
        if doc.get('release_scope') != scope:
            f.error(f'{gid}: evidence release_scope={doc.get("release_scope")!r}')
        if doc.get('status') != g.get('status'):
            f.error(f'{gid}: evidence status={doc.get("status")!r}')
        if not isinstance(doc.get('summary'), dict) or not doc['summary']:
            f.miss(f'{gid}.summary')

        # Gateは自分の根拠Caseを名指しする義務を負う。
        spec = snap['gates'][gid]
        refs = doc.get('case_refs')
        if not isinstance(refs, list) or not refs:
            f.miss(f'{gid}.case_refs')
            continue
        declared: set[tuple] = set()
        for ref in refs:
            if not isinstance(ref, list) or len(ref) != 2:
                f.error(f'{gid}: malformed case_ref {ref!r}')
                continue
            pair = (ref[0], ref[1])
            declared.add(pair)
            if pair not in passing_cases:
                f.error(f'{gid}: case_ref {ref[0]}/{ref[1]} is not a PASS case')

        if spec.get('test_refs_mode') == 'ALL_IN_SCOPE':
            # 「Scope内Manifest全体」を条件とするGateは、Scope内全Caseを
            # 根拠として列挙しなければならない。部分列挙を認めない。
            required_pairs = {tuple(p) for p in snap['scopes'][scope]['required_cases']}
            for miss in sorted(required_pairs - declared):
                f.miss(f'{gid}: case_ref missing for ALL_IN_SCOPE gate: {miss[0]}/{miss[1]}')
        else:
            allowed = set(spec.get('test_refs') or [])
            for t, c in sorted(declared):
                if t not in allowed:
                    f.error(
                        f'{gid}: case_ref {t}/{c} is not covered by gate test_refs '
                        f'{sorted(allowed)}'
                    )


def verify_areas(m: dict, snap: dict, scope: str, root: Path, f: Findings,
                 seen: dict[str, str]) -> None:
    """v1.0最大の穴。required_evidence_areas を空配列にすると全要件が消えていた。

    v1.3から必須集合は **Scope別**である。`snap['evidence_areas']` は領域の
    定義Catalog（Path・human_measured・再利用束縛）であって要求ではない。
    要求は `snap['scopes'][scope]['required_evidence_areas']` だけが持つ。

    Catalogを要求として使うと、Scope別化した意味が消えて全領域必須へ戻る。
    逆に要求が空になると v1.0 の穴が再現するので、空はFail-Closedで拒否する。
    """
    spec = {a['area']: a for a in snap['evidence_areas']}
    required = set(snap['scopes'][scope]['required_evidence_areas'])
    required_case_keys = {f'{t}/{c}' for t, c in snap['scopes'][scope]['required_cases']}
    if not required:
        f.error(f'required evidence area set is empty for {scope}')
        return
    undefined = sorted(required - set(spec))
    if undefined:
        # 定義の無い領域を要求している。Registry生成が壊れている。
        f.error(f'required evidence areas are not defined in catalog: {undefined}')
        return
    areas = m.get('required_evidence_areas')
    if not isinstance(areas, list):
        f.error('required_evidence_areas must be an array')
        return
    if not all(isinstance(item, dict) for item in areas):
        f.error('every required_evidence_areas item must be an object')
        return

    present = [a.get('area') for a in areas]
    if len(set(present)) != len(present):
        f.error('duplicate evidence area')
    for name in sorted(required - set(present)):
        f.miss(f'required evidence area not in manifest: {name}')
    for name in sorted(set(present) - required):
        if name in spec:
            # 定義はあるがこのScopeでは要求していない。黙って無視すると
            # 「MVP0-Aへapproval_uxを出せば通る」という抜け道になる。
            f.error(f'evidence area out of release scope {scope}: {name}')
        else:
            f.error(f'unknown evidence area: {name}')

    subtrees = m.get('source_subtree_hashes') or {}
    for a in areas:
        name = str(a.get('area'))
        if name not in required:
            continue
        if a.get('status') != PASS:
            f.miss(f'evidence area not PASS: {name}')
        check_no_reuse(seen, name, a.get('evidence_manifest_hash'), name, f)
        doc = check_evidence_file(root, a, name, a.get('evidence_manifest_hash'), f)
        if doc is None:
            continue
        verify_common_evidence(doc, m, name, f)
        if doc.get('area') != name:
            f.error(f'{name}: evidence area={doc.get("area")!r}')
        if doc.get('status') != a.get('status'):
            f.error(f'{name}: evidence status={doc.get("status")!r}')
        if not isinstance(doc.get('summary'), dict) or not doc['summary']:
            f.miss(f'{name}.summary')
        else:
            summary = doc['summary']
            if name == 'source_static_analysis':
                if summary.get('implementation_commit_sha') != m.get(
                        'implementation_commit_sha'):
                    f.error('source_static_analysis: commit is not bound')
                if summary.get('source_tree_clean') is not True:
                    f.error('source_static_analysis: source_tree_clean must be true')
                if not is_hash(summary.get('source_tree_hash')):
                    f.miss('source_static_analysis.summary.source_tree_hash')
            elif name == 'core_schema_suite':
                if summary.get('schema_set_hash') != m.get('schema_set_hash'):
                    f.error('core_schema_suite: schema_set_hash is not bound')
                if summary.get('validated_schema_count') != len(snap['core_schemas']):
                    f.error('core_schema_suite: validated_schema_count mismatch')
            elif name == 'sqlite_migration':
                if summary.get('migration_head') != m.get('migration_head'):
                    f.error('sqlite_migration: migration_head is not bound')
                if summary.get('fresh_install_passed') is not True:
                    f.error('sqlite_migration: fresh_install_passed must be true')
            elif name == 'unit_case_suite':
                # 領域が名指しした Case 集合が、Snapshot の Unit Case 集合と
                # **完全一致**すること。欠落は「検証していない Case を数えた」、
                # 混入は「別の層の Case を Unit として数えた」であり、どちらも
                # 領域の意味を壊す。
                declared = summary.get('case_ids')
                if not isinstance(declared, list) or not all(
                        isinstance(item, str) for item in declared):
                    f.miss('unit_case_suite.summary.case_ids')
                else:
                    expected_units = {
                        key for key in required_case_keys
                        if snap['expectations'][key].get(
                            'event_observation_policy') == POLICY_NOT_APPLICABLE
                    }
                    got = set(declared)
                    if len(got) != len(declared):
                        f.error('unit_case_suite: duplicate case_ids')
                    for key in sorted(expected_units - got):
                        f.miss(f'unit_case_suite: unit case not covered: {key}')
                    for key in sorted(got - expected_units):
                        # Snapshot に無い Case、あるいは Unit ではない Case。
                        # Policy 未確定の Case もここで落ちる。
                        f.error(f'unit_case_suite: case is not a unit case: {key}')
                    if summary.get('unit_case_count') != len(got):
                        f.error(
                            f'unit_case_suite: unit_case_count='
                            f'{summary.get("unit_case_count")!r} != {len(got)}')
                if summary.get('human_measured') is not False:
                    f.error('unit_case_suite: human_measured must be false')
                if summary.get('failing_case_ids'):
                    f.error(
                        f'unit_case_suite: failing cases {summary["failing_case_ids"]}')
                if summary.get('registry_snapshot_hash') != snap[
                        'registry_snapshot_hash']:
                    f.error('unit_case_suite: registry_snapshot_hash is not bound')
                if summary.get('design_sha256') != snap['design_sha256']:
                    f.error('unit_case_suite: design_sha256 is not bound')
                hashes = summary.get('case_evidence_hashes')
                if not isinstance(hashes, list) or len(hashes) != len(
                        summary.get('case_ids') or []):
                    f.miss('unit_case_suite.summary.case_evidence_hashes')
                else:
                    bound = {
                        c.get('evidence_manifest_hash') for c in (m.get('test_cases') or [])
                        if isinstance(c, dict)
                    }
                    # Case Evidence の Hash は Manifest 側の束縛と同じでなければ、
                    # 領域は別物の上に立っている。
                    for h in sorted(set(hashes) - bound):
                        f.error(f'unit_case_suite: evidence hash not in manifest: {h}')
            elif name == 'verifier_self_test':
                if summary.get('verifier_source_sha256') != sha256_file(
                        Path(__file__).resolve()):
                    f.error('verifier_self_test: verifier source hash mismatch')
                if not isinstance(summary.get('tests_run'), int) or \
                        summary['tests_run'] < SELF_TEST_MIN_TESTS:
                    f.error(
                        f'verifier_self_test: at least {SELF_TEST_MIN_TESTS} '
                        f'tests are required')
                if summary.get('failures') != 0 or summary.get('errors') != 0:
                    f.error('verifier_self_test: failures/errors must be zero')

        reuse = a.get('reused_from')
        if reuse is None:
            if doc.get('release_scope') != scope:
                f.error(f'{name}: evidence release_scope={doc.get("release_scope")!r}')
            continue

        # §26.6 再認定階層。人手計測Evidenceの再利用は subtree hash 一致に束縛する。
        if not spec[name].get('human_measured'):
            f.error(f'{name}: reused_from is only permitted for human-measured areas')
            continue
        bound = spec[name].get('reuse_bound_subtree')
        if not reuse.get('release_id'):
            f.miss(f'{name}.reused_from.release_id')
        declared = reuse.get('bound_subtree_hash')
        current = subtrees.get(bound)
        if not is_hash(declared):
            f.miss(f'{name}.reused_from.bound_subtree_hash')
        elif not is_hash(current):
            f.miss(f'source_subtree_hashes[{bound}]')
        elif declared != current:
            f.error(
                f'{name}: reused evidence is invalidated. '
                f'{bound} changed ({declared} -> {current})'
            )


def verify(
    design: Path, manifest_path: Path, evidence_root: Path,
    registry_path: Path, scope: str,
) -> tuple[dict, int]:
    f = Findings()

    try:
        m = load_json(manifest_path)
    except Exception as exc:
        return {'decision': 'INPUT_INVALID', 'reason': f'manifest: {exc}'}, EXIT_INPUT_INVALID
    try:
        snap = load_json(registry_path)
    except Exception as exc:
        return {'decision': 'INPUT_INVALID', 'reason': f'registry: {exc}'}, EXIT_INPUT_INVALID
    if not design.is_file():
        return {'decision': 'INPUT_INVALID', 'reason': 'design file not found'}, EXIT_INPUT_INVALID
    if not isinstance(m, dict) or not isinstance(snap, dict):
        return {'decision': 'INPUT_INVALID', 'reason': 'not a JSON object'}, EXIT_INPUT_INVALID

    # Registry Snapshotの自己完全性。改変されていれば以降の判定は無意味。
    # generated_atはHash対象外（生成時刻を含めると再生成の度に値が変わり、
    # 鮮度比較ができなくなる）。
    claimed = snap.get('registry_snapshot_hash')
    recomputed = domain_hash(
        'FDE-HARNESS/registry-snapshot/1/',
        {k: v for k, v in snap.items()
         if k not in ('registry_snapshot_hash', 'generated_at')})
    if claimed != recomputed:
        return (
            {'decision': 'INPUT_INVALID',
             'reason': f'registry snapshot hash mismatch (recomputed={recomputed})'},
            EXIT_INPUT_INVALID,
        )

    if scope not in snap.get('release_enabled_phases', []):
        return (
            {'decision': 'INPUT_INVALID',
             'reason': f'release scope {scope} is not release-enabled; '
                       f'phases without registered gates are blocked. '
                       f'enabled={snap.get("release_enabled_phases")}'},
            EXIT_INPUT_INVALID,
        )
    required_snapshot_fields = {
        'design_version', 'design_sha256', 'scopes', 'expectations', 'gates',
        'evidence_areas', 'core_schemas',
    }
    missing_snapshot_fields = sorted(required_snapshot_fields - set(snap))
    if missing_snapshot_fields or not isinstance(snap.get('scopes'), dict) or \
            scope not in snap.get('scopes', {}):
        return (
            {'decision': 'INPUT_INVALID',
             'reason': f'registry snapshot structure invalid; '
                       f'missing={missing_snapshot_fields}'},
            EXIT_INPUT_INVALID,
        )
    # Scope別のRequired領域が無いSnapshotで判定しない。ここを黙って
    # Catalog全件へフォールバックさせると、Scope別化が意味を失う。
    scope_areas = snap['scopes'][scope].get('required_evidence_areas')
    if not isinstance(scope_areas, list) or not scope_areas:
        return (
            {'decision': 'INPUT_INVALID',
             'reason': f'registry snapshot has no required_evidence_areas '
                       f'for release scope {scope}'},
            EXIT_INPUT_INVALID,
        )

    design_hash = sha256_file(design)
    manifest_hash = sha256_file(manifest_path)
    verifier_hash = sha256_file(Path(__file__).resolve())

    # RegistryとManifestの双方を実際の設計書へ束縛する。
    if snap.get('design_sha256') != design_hash:
        f.error(f'registry.design_sha256 does not match design file ({design_hash})')
    if m.get('design_sha256') != design_hash:
        f.error(f'manifest.design_sha256 does not match design file ({design_hash})')
    if m.get('design_version') != snap.get('design_version'):
        f.error(f'design_version must be {snap.get("design_version")}')
    if m.get('manifest_version') != MANIFEST_VERSION:
        f.error(f'manifest_version must be {MANIFEST_VERSION}')
    if m.get('release_scope') != scope:
        f.error(f'manifest.release_scope must be {scope}, got {m.get("release_scope")!r}')
    if m.get('registry_snapshot_hash') != snap.get('registry_snapshot_hash'):
        f.error('manifest.registry_snapshot_hash does not match registry snapshot')

    commit = m.get('implementation_commit_sha')
    if not isinstance(commit, str) or not GIT_SHA_RE.fullmatch(commit):
        f.miss('implementation_commit_sha')
    if m.get('source_tree_clean') is not True:
        f.miss('source_tree_clean=true')
    if not m.get('implementation_repository'):
        f.miss('implementation_repository')

    verify_environment(m, evidence_root, f)

    if not is_hash(m.get('schema_set_hash')):
        f.miss('schema_set_hash')
    if not m.get('migration_head'):
        f.miss('migration_head')

    expected_tm = snap['scopes'][scope]['test_manifest_hash']
    if not is_hash(m.get('test_manifest_hash')):
        f.miss('test_manifest_hash')
    elif m['test_manifest_hash'] != expected_tm:
        f.error(
            f'test_manifest_hash does not match registry-derived value for {scope} '
            f'(expected {expected_tm})'
        )

    seen_evidence: dict[str, str] = {}
    verify_cases(m, snap, scope, evidence_root, f, seen_evidence)
    verify_gates(m, snap, scope, evidence_root, f, seen_evidence)
    verify_areas(m, snap, scope, evidence_root, f, seen_evidence)

    for field in ('skipped_count', 'xfail_count'):
        if m.get(field) != 0:
            f.error(f'{field} must be 0, got {m.get(field)!r}')

    # TemplateのBLOCKED値はEvidence不足時には許容するが、それ以外が
    # 完全なManifestで自己宣言が矛盾していればGOにしない。
    if not f.errors and not f.missing and m.get('release_decision') != 'RUNTIME_GO':
        f.error('release_decision must be RUNTIME_GO when all evidence passes')

    decision = 'RUNTIME_GO' if f.ok else (
        'RUNTIME_NO_GO' if f.errors else 'BLOCKED_EVIDENCE_MISSING')

    report = {
        'verifier_version': VERIFIER_VERSION,
        'verifier_source_sha256': verifier_hash,
        'hash_profile': HASH_PROFILE,
        'release_scope': scope,
        'design_version': snap.get('design_version'),
        'design_sha256': design_hash,
        'manifest_sha256': manifest_hash,
        'registry_snapshot_hash': snap.get('registry_snapshot_hash'),
        'test_manifest_hash': expected_tm,
        'implementation_commit_sha': commit if isinstance(commit, str) else None,
        'runtime_environment_hash': (m.get('runtime_environment') or {}).get(
            'environment_manifest_hash'),
        'schema_set_hash': m.get('schema_set_hash'),
        'migration_head': m.get('migration_head'),
        'required_case_count': snap['scopes'][scope]['required_case_count'],
        'required_test_id_count': snap['scopes'][scope]['required_test_id_count'],
        'required_gate_count': snap['scopes'][scope]['required_gate_count'],
        'required_evidence_area_count': len(
            snap['scopes'][scope]['required_evidence_areas']),
        'decision': decision,
        'error_count': len(f.errors),
        'missing_count': len(f.missing),
        'errors': f.errors,
        'missing': f.missing,
    }
    # v1.0のreport hashはdecisionと件数しか覆わず、全く別のManifest 2件が
    # 同一hashを出していた。v1.2はManifest/Registry/Verifier/Commitを覆う。
    report['verification_report_hash'] = domain_hash(
        'FDE-HARNESS/runtime-go-report/1/', report)

    if f.errors:
        return report, EXIT_FAIL
    if f.missing:
        return report, EXIT_MISSING
    return report, EXIT_OK


def build_release_manifest(report: dict, evidence_manifest: dict) -> dict:
    """GO判定内容に束縛したRelease Manifestを作る。"""
    reused = sorted(
        area['area'] for area in evidence_manifest.get('required_evidence_areas', [])
        if area.get('reused_from') is not None
    )
    # Case Evidenceは別Treeにあるため、ここではManifestに束縛された
    # Gate Evidence集合Hashを保存し、詳細TierはReportで追跡する。
    gate_report_hash = domain_hash(
        'FDE-HARNESS/gate-report/1/', evidence_manifest.get('gates', []))
    body = {
        'release_manifest_version': '1.0',
        'decision': 'RUNTIME_GO',
        'release_scope': report['release_scope'],
        'design_sha256': report['design_sha256'],
        'registry_snapshot_hash': report['registry_snapshot_hash'],
        'verifier_source_sha256': report['verifier_source_sha256'],
        'evidence_manifest_sha256': report['manifest_sha256'],
        'verification_report_hash': report['verification_report_hash'],
        'implementation_commit_sha': report['implementation_commit_sha'],
        'runtime_environment_hash': report['runtime_environment_hash'],
        'schema_set_hash': report['schema_set_hash'],
        'migration_head': report['migration_head'],
        'test_manifest_hash': report['test_manifest_hash'],
        'gate_report_hash': gate_report_hash,
        'passed_gates': report['required_gate_count'],
        'passed_cases': report['required_case_count'],
        'passed_areas': report['required_evidence_area_count'],
        'skipped_count': 0,
        'xfail_count': 0,
        'reused_evidence': reused,
        'durability_claim': 'T1/T2 only; T3 NOT VERIFIED',
    }
    body['release_manifest_hash'] = domain_hash(
        'FDE-HARNESS/release-manifest/1/', body)
    return body


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description='Verify v1.8 Runtime GO evidence')
    p.add_argument('--design', required=True, type=Path)
    p.add_argument('--manifest', required=True, type=Path)
    p.add_argument('--evidence-root', required=True, type=Path)
    p.add_argument('--registry', required=True, type=Path,
                   help='registry-snapshot.json（件数と期待値の正本）')
    p.add_argument('--release-scope', required=True,
                   help='判定対象Phase。例: MVP0-A')
    p.add_argument('--emit-report', type=Path)
    p.add_argument('--emit-release-manifest', type=Path)
    p.add_argument('--quiet', action='store_true')
    args = p.parse_args(argv)

    report, code = verify(
        args.design, args.manifest, args.evidence_root,
        args.registry, args.release_scope,
    )
    if args.emit_report:
        args.emit_report.parent.mkdir(parents=True, exist_ok=True)
        args.emit_report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if args.emit_release_manifest and code == EXIT_OK:
        manifest = load_json(args.manifest)
        release_manifest = build_release_manifest(report, manifest)
        args.emit_release_manifest.parent.mkdir(parents=True, exist_ok=True)
        args.emit_release_manifest.write_text(
            json.dumps(release_manifest, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8')
    if not args.quiet:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
