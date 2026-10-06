#!/usr/bin/env python3
"""Case Evidence Emitter。実行結果だけからEvidenceを生成する。

## PASSを手入力できない

`status` は引数に無い。**実行結果と Registry 期待値の突合から導出する**。
「PASSと書く」経路が存在しなければ、書き間違いも偽装もできない。

`emit_case_evidence()` の入力は `CaseRunResult` だけであり、そこには
観測値しか入らない。期待値は `design-source/registries/tests.yaml` から読む。

## 副作用観測の無いCaseはPASSを出せない

`side_effects=None` は「測っていない」を意味する。測っていないものを
PASSにしない（不変条件#16）。Domain試験だけで通したCaseがそのまま
Release Evidenceになる経路を塞ぐ。

**「0だった」と「測っていない」は別である。** 前者は
`SideEffectObservation(...)` の全項目0で表現し、後者は `None` で表現する。
同じ値に潰さない。

## Dirty Tree を拒む

`git status --short` に出力があるなら、そのEvidenceがどのソースに対する
観測なのか確定しない。Runbook §5 と同じ規則である。

## 出力先はRepository外に限る

Evidenceを観測対象のツリー内へ書くと、書いた時点でCleanでなくなる。
Path Traversal（`..`）も拒否する。Case IDから組んだ相対Pathが
EVIDENCE_DIRの外へ出る経路を作らない。

## 同じEvidenceを二度書かない

既に存在するPathへは書かない。上書きを許すと、失敗したEvidenceを
成功で塗り替えられる。再実行したいなら新しい `RELEASE_ID` を使う。

## 終了Code

| Code | 意味 |
|---|---|
| `0` | Evidence を生成した |
| `3` | 生成条件を満たさない（Dirty Tree／出力先不正／観測欠落／再利用） |
| `4` | 入出力が不正 |
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

__all__ = [
    "CaseRunResult",
    "EvidenceEmissionError",
    "SideEffectObservation",
    "emit_case_evidence",
]

#: 書込み先の版。正本は design-source/registries/evidence-schemas.yaml の
#: active_write_version である。2.0 は read_only として残る（B-1）。
EVIDENCE_SCHEMA_VERSION: Final[str] = "3.0"
#: Hash Profile は 1.2 と共通のままにする。ここを変えると既存 1.2 Evidence の
#: `evidence_hash` が測り直しで一致しなくなり、過去の記録が壊れたように見える。
#: 変わったのは Body の中身であって Hash の採り方ではない。
_HASH_PREFIX: Final[str] = "FDE-HARNESS/case-evidence/1/"

#: Registry の `event_observation_policy`。正本は tests.yaml である（§19.1.1）。
_POLICY_NOT_APPLICABLE: Final[str] = "NOT_APPLICABLE"
_POLICY_REQUIRED_EMPTY: Final[str] = "REQUIRED_EMPTY"

_KIND_UNIT: Final[str] = "UNIT"
_KIND_ORCHESTRATION: Final[str] = "ORCHESTRATION"

_OBS_OBSERVED: Final[str] = "OBSERVED"
_OBS_OBSERVED_EMPTY: Final[str] = "OBSERVED_EMPTY"
_OBS_NOT_APPLICABLE: Final[str] = "NOT_APPLICABLE"
_CASE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^AT-[A-Z0-9-]+/[A-Z0-9_]+$")


class EvidenceEmissionError(RuntimeError):
    """Evidenceを生成できない。**部分的な生成物を残さない。**"""


@dataclass(frozen=True, slots=True)
class SideEffectObservation:
    """実行中に観測した副作用の回数。

    存在すること自体が「測った」証拠である。測っていない場合は
    このObjectを作らず `None` を渡す。
    """

    network_calls: int
    process_launches: int
    workspace_commits: int
    external_effects: int
    ledger_effect_attempts: int

    def as_dict(self) -> dict[str, int]:
        return {
            "network_calls": self.network_calls,
            "process_launches": self.process_launches,
            "workspace_commits": self.workspace_commits,
            "external_effects": self.external_effects,
            "ledger_effect_attempts": self.ledger_effect_attempts,
        }


@dataclass(frozen=True, slots=True)
class FaultInjectionSetting:
    """注入したFaultの設定。P1 Caseで必須。

    どのFaultをどこへ入れたかを残さないと、そのEvidenceが
    「何の条件下での観測か」を後から確かめられない。
    """

    fault_point: str
    fault_kind: str
    deterministic: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "fault_point": self.fault_point,
            "fault_kind": self.fault_kind,
            "deterministic": self.deterministic,
        }


@dataclass(frozen=True, slots=True)
class CaseRunResult:
    """Runnerが観測した結果。**期待値も status も含まない。**"""

    case_id: str
    gate_ids: tuple[str, ...]
    input_fixture_path: Path
    observed_state: str
    observed_error_code: str | None
    observed_events: tuple[str, ...]
    side_effects: SideEffectObservation | None
    #: どのSubjectについての観測か。無いと Evidence 単体で対象を確かめられない。
    actual_subject_id: str = ""
    #: 観測時点の Stream Head。Ledger を観測しない Unit Case では None のままにする。
    #: **0 で埋めない。** 埋めれば「見て 0 だった」と偽ることになる。
    ledger_head_before: int | None = None
    ledger_head_after: int | None = None
    test_node_ids: tuple[str, ...] = ()
    #: Fault注入下で走らせたCaseはこれを持つ。
    fault_injection: FaultInjectionSetting | None = None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _evidence_hash(body: dict[str, Any]) -> str:
    """Evidence本体のHash。`evidence_hash` 自身は入力に含めない。"""
    payload = {key: value for key, value in body.items() if key != "evidence_hash"}
    return (
        "sha256:" + hashlib.sha256(_HASH_PREFIX.encode("utf-8") + _canonical(payload)).hexdigest()
    )


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", "-c", "core.quotepath=false", *args],  # noqa: S607
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise EvidenceEmissionError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _require_clean_tree(repo_root: Path) -> str:
    """Clean Tree を要求し、Commit SHAを返す。

    汚れたツリーの観測は、どのソースに対する観測なのか確定しない。
    """
    status = _git(repo_root, "status", "--short").strip()
    if status:
        count = len(status.splitlines())
        raise EvidenceEmissionError(
            f"DIRTY_TREE: {count} 件の未Commit変更がある。"
            "Evidenceがどのソースに対する観測か確定しない"
        )
    return _git(repo_root, "rev-parse", "HEAD").strip()


def _resolve_output(evidence_dir: Path, repo_root: Path, case_id: str) -> Path:
    """出力先を決め、Repository外かつTraversalが無いことを確かめる。"""
    evidence_root = evidence_dir.resolve()
    repo = repo_root.resolve()
    if evidence_root == repo or repo in evidence_root.parents:
        raise EvidenceEmissionError(
            f"EVIDENCE_IN_REPOSITORY: {evidence_root} は Repository({repo}) 配下である"
        )

    # Case ID からPathを組む。`..` を含むIDを弾いてから結合する。
    if not _CASE_ID_PATTERN.match(case_id):
        raise EvidenceEmissionError(f"INVALID_CASE_ID: {case_id!r}")
    test_id, _, case_name = case_id.partition("/")
    target = (evidence_root / "cases" / test_id / f"{case_name}.json").resolve()
    if evidence_root not in target.parents:
        raise EvidenceEmissionError(f"PATH_TRAVERSAL: {target} は {evidence_root} の外である")
    if target.exists():
        raise EvidenceEmissionError(
            f"EVIDENCE_REUSE: {target} は既に存在する。"
            "上書きは失敗を成功で塗り替える経路になる。新しい RELEASE_ID を使うこと"
        )
    return target


def _expected_case(registries: Path, case_id: str) -> dict[str, Any]:
    test_id, _, case_name = case_id.partition("/")
    rows = yaml.safe_load((registries / "tests.yaml").read_text(encoding="utf-8"))["test_cases"]
    for row in rows:
        if row["test_id"] == test_id and row["case_id"] == case_name:
            return dict(row)
    raise EvidenceEmissionError(f"UNKNOWN_CASE: {case_id} は Registry に無い")


def _derive_layer(expected: dict[str, Any], result: CaseRunResult) -> tuple[str, str, str | None]:
    """`evidence_kind` と `event_observation` を Registry と観測から決める。

    **Runner の都合で決めない。** 免除の根拠は Registry 正本の
    `event_observation_policy` だけである（Owner Decision DCR-5 / E1）。
    宣言の無い Case は従来どおり Ledger 観測を要求する。

    `NOT_APPLICABLE` を名乗れるのは、Registry がそう宣言し、かつ期待Event列が
    空である Case に限る。期待列が非空なら作用を主張しているので、免除と矛盾する。
    """
    policy = expected.get("event_observation_policy")
    if policy is not None and policy not in (_POLICY_NOT_APPLICABLE, _POLICY_REQUIRED_EMPTY):
        raise EvidenceEmissionError(
            f"UNKNOWN_EVENT_OBSERVATION_POLICY: {expected['test_id']}/{expected['case_id']} "
            f"の {policy!r} は Registry の語彙に無い"
        )

    if policy is None and not list(expected["expected_event_sequence"] or []):
        # 期待Event列が空なだけでは、`REQUIRED_EMPTY`（見て0件）なのか
        # `NOT_APPLICABLE`（見ない）なのか決まらない。**推測で埋めない。**
        # 埋めた側へ倒すと、Ledgerを一度も読んでいないCaseが「0件を観測した」と
        # 主張するEvidenceになるか、逆に免除されるべきCaseが永久に落ちる。
        raise EvidenceEmissionError(
            "EVENT_OBSERVATION_POLICY_MISSING: "
            f"{expected['test_id']}/{expected['case_id']} は期待Event列が空だが "
            "event_observation_policy がRegistryに無い。"
            "REQUIRED_EMPTY と NOT_APPLICABLE のどちらかをRegistryで宣言すること"
        )

    if policy == _POLICY_NOT_APPLICABLE:
        if list(expected["expected_event_sequence"] or []):
            raise EvidenceEmissionError(
                "POLICY_CONTRADICTS_EXPECTATION: "
                f"{expected['test_id']}/{expected['case_id']} は NOT_APPLICABLE を宣言しながら "
                "期待Event列が非空である"
            )
        if result.observed_events:
            # 免除された Case が Event を持ち帰っている。どちらが本当か決められない。
            raise EvidenceEmissionError(
                "UNEXPECTED_EVENT_OBSERVATION: "
                f"{expected['test_id']}/{expected['case_id']} は NOT_APPLICABLE だが "
                f"{len(result.observed_events)} 件の Event を観測している"
            )
        return _KIND_UNIT, _OBS_NOT_APPLICABLE, policy

    # ここから先は Ledger を観測した Case である。observed_events は実測値であり、
    # 空であることは「見て0件だった」を意味する（case_runner が未観測を弾く）。
    if result.observed_events:
        return _KIND_ORCHESTRATION, _OBS_OBSERVED, policy
    return _KIND_ORCHESTRATION, _OBS_OBSERVED_EMPTY, policy


def _derive_status(
    expected: dict[str, Any], result: CaseRunResult, event_observation: str
) -> tuple[str, list[str]]:
    """期待値と観測値の突合から `status` を導出する。

    **ここが唯一の status 決定経路である。** 引数に status は無い。
    """
    mismatches: list[str] = []
    if result.side_effects is None:
        # 測っていないものをPASSにしない。0だったこととは別である。
        mismatches.append("side_effect_observation_missing")
    if not result.actual_subject_id:
        # どのSubjectについての観測か決まらない結果をPASSにしない。
        mismatches.append("actual_subject_id_missing")

    if result.observed_state != expected["expected_state"]:
        mismatches.append("state")
    expected_code = expected["expected_error_code"]
    if result.observed_error_code != expected_code:
        mismatches.append("error_code")
    if event_observation != _OBS_NOT_APPLICABLE and list(result.observed_events) != list(
        expected["expected_event_sequence"] or []
    ):
        # 非該当の Case は観測していないので突合しない。**空配列同士の一致を
        # 「一致した」と記録すると、見ていないことが検証済みに化ける。**
        mismatches.append("event_sequence")

    return ("PASS" if not mismatches else "FAIL"), mismatches


def _schema_catalog_hash(repo_root: Path) -> str:
    """Snapshotが持つSchema Catalog Hash。どのSchema版への観測かを束縛する。"""
    snapshot = repo_root / "registry-snapshot.json"
    if not snapshot.is_file():
        raise EvidenceEmissionError(f"MISSING_REGISTRY_SNAPSHOT: {snapshot}")
    value = json.loads(snapshot.read_text(encoding="utf-8")).get("schema_catalog_hash")
    if not value:
        raise EvidenceEmissionError("MISSING_SCHEMA_CATALOG_HASH")
    return str(value)


def _design_identity(repo_root: Path) -> tuple[str, str]:
    """設計書Hashと Registry Snapshot Hash を実測する。

    どの設計・どのRegistryに対する観測かを Evidence へ束縛する。
    """
    design = repo_root / "design-v1.25-runtime-go.md"
    snapshot = repo_root / "registry-snapshot.json"
    if not design.is_file() or not snapshot.is_file():
        raise EvidenceEmissionError(
            f"MISSING_DESIGN_BINDING: {design.name} または {snapshot.name} が無い"
        )
    snapshot_body = json.loads(snapshot.read_text(encoding="utf-8"))
    return _sha256_file(design), str(snapshot_body["registry_snapshot_hash"])


def emit_case_evidence(
    result: CaseRunResult,
    *,
    evidence_dir: Path,
    repo_root: Path,
    environment_manifest: Path,
    registries: Path,
) -> Path:
    """Case Evidenceを1件生成し、書き出したPathを返す。

    生成条件を満たさない場合は `EvidenceEmissionError` を送出し、
    **File を1つも作らない**。
    """
    expected = _expected_case(registries, result.case_id)
    commit_sha = _require_clean_tree(repo_root)
    target = _resolve_output(evidence_dir, repo_root, result.case_id)

    if not result.input_fixture_path.is_file():
        raise EvidenceEmissionError(f"MISSING_INPUT_FIXTURE: {result.input_fixture_path}")
    if not environment_manifest.is_file():
        raise EvidenceEmissionError(f"MISSING_ENVIRONMENT_MANIFEST: {environment_manifest}")

    evidence_kind, event_observation, policy = _derive_layer(expected, result)
    status, mismatches = _derive_status(expected, result, event_observation)

    design_hash, registry_snapshot_hash = _design_identity(repo_root)
    test_id, _, _ = result.case_id.partition("/")

    body: dict[str, Any] = {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "evidence_kind": evidence_kind,
        "event_observation": event_observation,
        # Registry が宣言した Policy を写す。Verifier が Release 束縛の
        # Snapshot と照合できるようにするためである（§26.2.1）。
        "event_observation_policy": policy,
        "case_id": result.case_id,
        "test_id": test_id,
        "gate_ids": sorted(result.gate_ids),
        "status": status,
        "mismatched_fields": sorted(mismatches),
        "implementation_commit_sha": commit_sha,
        "design_sha256": design_hash,
        "registry_snapshot_hash": registry_snapshot_hash,
        "schema_catalog_hash": _schema_catalog_hash(repo_root),
        "fault_injection": (result.fault_injection.as_dict() if result.fault_injection else None),
        "source_tree_clean": True,
        "input_fixture_path": result.input_fixture_path.name,
        "input_fixture_hash": _sha256_file(result.input_fixture_path),
        "environment_manifest_hash": _sha256_file(environment_manifest),
        "expected": {
            "state": expected["expected_state"],
            "error_code": expected["expected_error_code"],
            "event_sequence": list(expected["expected_event_sequence"] or []),
            "expectation_descriptor_hash": expected["expectation_descriptor_hash"],
        },
        "observed": {
            "state": result.observed_state,
            "error_code": result.observed_error_code,
            # 非該当は null。`[]` を残すと event_observation を読まない消費側が
            # 「観測して0件」と読む（§26.2.1）。
            "event_sequence": (
                None if event_observation == _OBS_NOT_APPLICABLE else list(result.observed_events)
            ),
            "actual_subject_id": result.actual_subject_id or None,
            # 観測していない Case は null のままにする。0 を書けば
            # 「見て 0 だった」と偽ることになる（Schema 3.0 も null を要求する）。
            "ledger_head_before": (
                None if event_observation == _OBS_NOT_APPLICABLE else result.ledger_head_before
            ),
            "ledger_head_after": (
                None if event_observation == _OBS_NOT_APPLICABLE else result.ledger_head_after
            ),
        },
        "side_effects": result.side_effects.as_dict() if result.side_effects else None,
        "test_node_ids": sorted(result.test_node_ids),
        "producer": "tools/emit_case_evidence.py",
    }
    body["evidence_hash"] = _evidence_hash(body)

    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".json.tmp")
    temp.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(target)
    return target


def verify_case_evidence(path: Path) -> None:
    """既存Evidenceの `evidence_hash` を測り直して突合する。

    改変（statusの書換え、Fixture Hashの差し替え）を検出する。
    """
    body = json.loads(path.read_text(encoding="utf-8"))
    recorded = body.get("evidence_hash")
    derived = _evidence_hash(body)
    if recorded != derived:
        raise EvidenceEmissionError(
            f"EVIDENCE_HASH_MISMATCH: {path} recorded={recorded} derived={derived}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", type=Path, help="既存Evidenceのhashを検証する")
    args = parser.parse_args(argv)

    if args.verify is None:
        print(
            "このToolはRunnerから import して使う。"
            "PASSを手で書ける CLI 経路は用意しない。--verify だけを提供する。",
            file=sys.stderr,
        )
        return 4
    try:
        verify_case_evidence(args.verify)
    except EvidenceEmissionError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    print(f"evidence hash ok: {args.verify}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
