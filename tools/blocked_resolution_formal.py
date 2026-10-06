#!/usr/bin/env python3
"""正式Case Evidence／Manifest Bytes束縛でBlock Recordの解消根拠を検証する。

## 旧自己Hashを新方式へ読み替えない

旧 `tools/emit_case_evidence.py` の Evidence は Body 全体の正準化Hashを
`evidence_hash` として自分の中へ持つ。正式Envelope（`tools/collect_unit_cases.py`）は
それを持たず、**Manifest の `evidence_manifest_hash` が File Bytes の SHA-256** である。
別物なので、片方の検証をもう片方へ流用しない。本Moduleは正式方式だけを扱い、
旧方式は `tools/resolve_blocked_record.py` の Legacy 経路がそのまま持つ。

## 判定器を書き直さない

Case 本体の契約は未変更の `verify_runtime_go.py` が正本である。ここでは
`check_evidence_file` / `verify_common_evidence` / `check_event_observation` /
`check_bound_file` をそのまま呼ぶ。条件を緩めた写しを作らない。

## 対象Caseは自由文から拾わない

Record本文へCase名が書かれていることは根拠にならない。対象集合は保存済みOwner
決定（`docs/decision/`）と**現在のRegistry Snapshot**の突合から導く。決定当時は
Unit だったCaseが現在そう分類されていなければ拒否する。

## 未観測を0件へ変換しない

`side_effects` の各counterは int であることを型で確かめる。欠落・`None`・`bool` は
「測っていない」であり、0 として数えない（不変条件#16）。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
for _entry in (str(ROOT), str(ROOT / "tools")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import verify_runtime_go as verifier  # noqa: E402

from build_registry_snapshot import domain_hash  # noqa: E402
from emit_unit_area_evidence import (  # noqa: E402
    AreaEvidenceError,
    _unit_case_ids,
    _validate_binding,
)

#: `blocked/records/<id>.json` へ結合してよい Block ID の形。
#: `/` も `.` も通さないので、結合前に Path Escape を締め出せる。
BLOCKER_ID_RE = re.compile(r"^BLK-[0-9]{8}-[A-Z0-9-]+$")

#: 本Moduleの正式方式が閉じられる**唯一の論理Task**。
#:
#: 対象Case集合は `docs/decision/` の保存済みOwner決定から導くが、その決定は
#: 「F-2 の7件を Unit Evidence へ分離する」ことだけを述べている。**別Taskの塞がりが
#: 同じ証跡で解けるとは一言も言っていない。** Task を見ずに証跡だけで閉じられると、
#: たとえば Backup 復旧が未確認のままの Record を F-2 の Unit Evidence で閉じられる。
#: 証跡の適用条件は「その証跡がその塞がりを解いたこと」であり、Scope や正本Hashが
#: 合っていることではない。
#:
#: 別Taskを閉じたくなったら、その Task の対象集合と適用条件を持つ経路を新しく作る。
#: ここを緩めて汎用の「正式方式」にしない。
APPLICABLE_TASK_ID = "F2-UNIT-EVIDENCE-SEPARATION"

#: Unit Case 側の観測Contract。Collector が書いた版とだけ突合する。
MONITOR_CONTRACT = "unit-execution-monitor/1"
RECEIPT_CONTRACT = "case-execution-record/1"
UNIT_AREA = "unit_case_suite"
POLICY_NOT_APPLICABLE = "NOT_APPLICABLE"
UNIT_COUNTER_FIELDS = (
    "network_calls",
    "process_launches",
    "workspace_commits",
    "external_effects",
    "ledger_effect_attempts",
)


class FormalResolutionError(RuntimeError):
    """正式Evidenceとして受理できない。**部分的な更新を残さない。**"""


def _is_int(value: Any) -> bool:
    """`bool` を int として数えない。`True` が 1 件の観測に化けるのを防ぐ。"""
    return type(value) is int


def require_applicable_task(record: dict[str, Any]) -> str:
    """この証跡がその塞がりへ適用できるかを、Task で結び付けて確かめる。

    **Scope と正本Hashが合っているだけでは足りない。** 同じ Release Scope の別Taskは
    いくらでもあり、そのどれもが F-2 の Unit Evidence では解けない。
    """
    task_id = record.get("task_id")
    if task_id != APPLICABLE_TASK_ID:
        raise FormalResolutionError(
            f"FORMAL_MODE_NOT_APPLICABLE: 正式方式が閉じられるのは "
            f"task_id={APPLICABLE_TASK_ID!r} だけである。"
            f"{record.get('blocker_id')!r} は task_id={task_id!r} であり、"
            "F-2 の Unit Case Evidence はこの塞がりを解いていない。"
            "別Taskには、そのTaskの対象集合と適用条件を持つ経路を作ること"
        )
    return str(task_id)


def record_path(records_dir: Path, blocker_id: Any) -> Path:
    """Block ID を Path へ結合する前に検証する。Escape と Symlink を拒否する。"""
    if not isinstance(blocker_id, str) or not BLOCKER_ID_RE.fullmatch(blocker_id):
        raise FormalResolutionError(f"INVALID_BLOCKER_ID: {blocker_id!r}")
    records = records_dir.resolve()
    if not records.is_dir():
        raise FormalResolutionError(f"MISSING_RECORDS_DIR: {records}")
    target = records / f"{blocker_id}.json"
    if target.parent != records:
        raise FormalResolutionError(f"BLOCKER_ID_ESCAPES_RECORDS_DIR: {blocker_id}")
    if target.is_symlink():
        raise FormalResolutionError(f"BLOCKER_RECORD_IS_SYMLINK: {blocker_id}")
    if not target.is_file():
        raise FormalResolutionError(f"UNKNOWN_BLOCKER: {target}")
    if target.resolve().parent != records:
        raise FormalResolutionError(f"BLOCKER_RECORD_RESOLVES_OUTSIDE: {blocker_id}")
    return target


@dataclass(frozen=True, slots=True)
class ChainLink:
    """`supersedes` 1段分。歴史Recordの中身は読むだけで書き換えない。"""

    blocker_id: str
    status: str
    task_id: str
    release_scope: str
    design_sha256: str
    registry_snapshot_hash: str
    acceptance: tuple[str, ...]
    supersedes: str | None
    successor_blocker_id: str | None
    record_sha256: str


def walk_chain(records_dir: Path, head_id: str) -> list[ChainLink]:
    """先頭から `supersedes` を辿る。循環・欠落・自己参照で停止する。"""
    links: list[ChainLink] = []
    seen: set[str] = set()
    current: str | None = head_id
    while current is not None:
        if current in seen:
            raise FormalResolutionError(f"SUPERSEDES_CYCLE: {current}")
        seen.add(current)
        path = record_path(records_dir, current)
        raw = path.read_bytes()
        body = json.loads(raw.decode("utf-8"))
        if body.get("blocker_id") != current:
            # File名とRecord本文の名乗りが違う。どちらが正かを推測しない。
            raise FormalResolutionError(
                f"BLOCKER_ID_FILENAME_MISMATCH: {current} の本文は "
                f"{body.get('blocker_id')!r} を名乗っている"
            )
        previous = body.get("supersedes")
        if previous is not None and not isinstance(previous, str):
            raise FormalResolutionError(f"INVALID_SUPERSEDES_TYPE: {current}")
        if previous == current:
            raise FormalResolutionError(f"SELF_SUPERSEDES: {current}")
        acceptance = ((body.get("next_action") or {}).get("acceptance")) or []
        if not isinstance(acceptance, list) or any(
            not isinstance(item, str) or not item.strip() for item in acceptance
        ):
            raise FormalResolutionError(f"INVALID_ACCEPTANCE: {current}")
        links.append(
            ChainLink(
                blocker_id=current,
                status=str(body.get("status")),
                task_id=str(body.get("task_id")),
                release_scope=str(body.get("release_scope")),
                design_sha256=str(body.get("design_sha256")),
                registry_snapshot_hash=str(body.get("registry_snapshot_hash")),
                acceptance=tuple(acceptance),
                supersedes=previous,
                successor_blocker_id=(body.get("resolution") or {}).get("successor_blocker_id"),
                record_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
            )
        )
        current = previous
    return links


def validate_chain(links: list[ChainLink]) -> dict[str, Any]:
    """継承の形を検査し、継承した受入条件を返す。

    設計改訂による `design_sha256` / `registry_snapshot_hash` の更新は**正常な進行**
    であり、Errorにしない。分岐・二重OPEN・Task/Scope不一致・後継Linkの食い違いだけを
    拒否する。
    """
    if not links:
        raise FormalResolutionError("EMPTY_SUPERSEDES_CHAIN")
    head = links[0]
    if head.status != "OPEN":
        raise FormalResolutionError(f"HEAD_NOT_OPEN: {head.blocker_id} は {head.status}")
    open_ids = [link.blocker_id for link in links if link.status == "OPEN"]
    if open_ids != [head.blocker_id]:
        raise FormalResolutionError(f"MULTIPLE_OPEN_IN_CHAIN: {open_ids}")
    for link in links[1:]:
        if link.task_id != head.task_id:
            raise FormalResolutionError(
                f"CHAIN_TASK_MISMATCH: {link.blocker_id} task_id={link.task_id!r} "
                f"!= {head.task_id!r}"
            )
        if link.release_scope != head.release_scope:
            raise FormalResolutionError(
                f"CHAIN_SCOPE_MISMATCH: {link.blocker_id} "
                f"release_scope={link.release_scope!r} != {head.release_scope!r}"
            )
    for successor, predecessor in itertools.pairwise(links):
        declared = predecessor.successor_blocker_id
        if declared is not None and declared != successor.blocker_id:
            raise FormalResolutionError(
                f"CHAIN_LINK_NOT_BIDIRECTIONAL: {predecessor.blocker_id} は "
                f"{declared!r} を後継と記録しているが {successor.blocker_id} から辿られた"
            )

    inherited: list[str] = []
    for link in links:
        for item in link.acceptance:
            if item not in inherited:
                inherited.append(item)
    return {
        "chain": [link.blocker_id for link in links],
        "chain_length": len(links),
        "head": head.blocker_id,
        "task_id": head.task_id,
        "release_scope": head.release_scope,
        "inherited_acceptance": inherited,
        "acceptance_by_record": {link.blocker_id: list(link.acceptance) for link in links},
        "design_hash_history": [
            {"blocker_id": link.blocker_id, "design_sha256": link.design_sha256} for link in links
        ],
        "registry_hash_history": [
            {
                "blocker_id": link.blocker_id,
                "registry_snapshot_hash": link.registry_snapshot_hash,
            }
            for link in links
        ],
        "record_sha256": {link.blocker_id: link.record_sha256 for link in links},
    }


def derive_target_cases(decision_dir: Path, snapshot: dict[str, Any], scope: str) -> dict[str, Any]:
    """保存済みOwner決定と現行Registryの突合から対象Case集合を導く。

    **Record の自由文は読まない。** 2つの独立した保存文書が同じ集合を示し、かつ
    現在の Registry がそれを Unit（`NOT_APPLICABLE`）と分類しているときだけ通す。
    """
    matrix_path = decision_dir / "F1-F2-impact-matrix.json"
    dcr_path = decision_dir / "OWNER-DECISION-UNIT-EVIDENCE-DCR.json"
    for path in (matrix_path, dcr_path):
        if not path.is_file():
            raise FormalResolutionError(f"MISSING_OWNER_DECISION: {path}")
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    dcr = json.loads(dcr_path.read_text(encoding="utf-8"))

    approval = ((matrix.get("owner_approval") or {}).get("answers") or {}).get("F2-APPROVAL") or {}
    if approval.get("choice") != "F2-ALL":
        raise FormalResolutionError(f"F2_APPROVAL_NOT_RECORDED: choice={approval.get('choice')!r}")
    if (matrix.get("owner_approval") or {}).get("unanswered"):
        raise FormalResolutionError("F2_OWNER_APPROVAL_INCOMPLETE")
    if dcr.get("unanswered"):
        raise FormalResolutionError(f"UNIT_EVIDENCE_DCR_UNANSWERED: {dcr['unanswered']}")
    answers = dcr.get("answers") or {}
    missing_answers = sorted({f"DCR-{n}" for n in range(1, 6)} - set(answers))
    if missing_answers:
        raise FormalResolutionError(f"UNIT_EVIDENCE_DCR_MISSING: {missing_answers}")

    from_matrix = sorted(set((matrix.get("candidates") or {}).get("F-2") or []))
    from_dcr = sorted(set(((dcr.get("gate") or {}).get("4_f2_cases") or {}).get("cases") or []))
    if not from_matrix or from_matrix != from_dcr:
        # 2つの保存文書が食い違う。どちらが正かを推測しない。
        raise FormalResolutionError(
            f"F2_CASE_SET_DISAGREEMENT: matrix={from_matrix} dcr={from_dcr}"
        )

    if scope not in snapshot.get("scopes", {}):
        raise FormalResolutionError(f"UNKNOWN_RELEASE_SCOPE: {scope}")
    required = {f"{t}/{c}" for t, c in snapshot["scopes"][scope]["required_cases"]}
    units = _unit_case_ids(snapshot, scope)
    unit_set = set(units)
    for key in from_matrix:
        if key not in required:
            raise FormalResolutionError(f"F2_CASE_OUT_OF_SCOPE: {key} は {scope} の必須ではない")
        if key not in unit_set:
            # 決定当時 Unit でも、いまの分類器がそう言っていなければ通さない。
            raise FormalResolutionError(
                f"F2_CASE_NOT_CLASSIFIED_AS_UNIT: {key} の "
                f"event_observation_policy は {POLICY_NOT_APPLICABLE} ではない"
            )
        if list(snapshot["expectations"][key].get("expected_event_sequence") or []):
            raise FormalResolutionError(f"F2_CASE_EXPECTS_EVENTS: {key} は期待Event列が非空である")
    return {
        # この対象集合が解くのはこの Task だけである。消費側は必ず突合すること。
        "applicable_task_id": APPLICABLE_TASK_ID,
        "target_case_ids": from_matrix,
        "target_case_count": len(from_matrix),
        "unit_case_ids": units,
        "unit_case_count": len(units),
        "owner_decision_sources": {
            matrix_path.name: verifier.sha256_file(matrix_path),
            dcr_path.name: verifier.sha256_file(dcr_path),
        },
        "owner_answers": {key: answers[key]["choice"] for key in sorted(answers)},
    }


def verify_unit_observations(raw_path: Path, key: str, doc: dict[str, Any]) -> dict[str, Any]:
    """Raw観測を読み直し、実測counterと子Processの観測範囲を確かめる。

    **0 を書き足さない。** counter が int でなければ「測っていない」として拒否する。
    観測限界（`limits`）は消さずに持ち帰り、解消Reportへそのまま残す。
    """
    try:
        lines = [line for line in raw_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        records = [json.loads(line) for line in lines]
    except (OSError, ValueError) as exc:
        raise FormalResolutionError(f"UNIT_RAW_UNREADABLE: {key}: {exc}") from exc
    if not records:
        raise FormalResolutionError(f"UNIT_OBSERVATION_MISSING: {key}")

    nodes: list[str] = []
    limits: list[str] = []
    scopes: list[str] = []
    child_scopes: list[str] = []
    assertion_count = 0
    for row in records:
        if row.get("case_id") != key or row.get("recorded") is not True:
            raise FormalResolutionError(f"UNIT_OBSERVATION_MISSING: {key}")
        node = row.get("node_id")
        if not isinstance(node, str) or not node.strip():
            raise FormalResolutionError(f"UNIT_NODE_MISSING: {key}")
        nodes.append(node)

        report = row.get("unit_execution")
        if not isinstance(report, dict):
            raise FormalResolutionError(f"UNIT_MONITOR_MISSING: {key}")
        if report.get("contract") != MONITOR_CONTRACT or report.get("complete") is not True:
            raise FormalResolutionError(f"UNIT_MONITOR_INCOMPLETE: {key}")
        if report.get("violations") != []:
            raise FormalResolutionError(
                f"UNIT_MONITOR_VIOLATIONS: {key} {report.get('violations')}"
            )
        scope_name = report.get("scope")
        if not isinstance(scope_name, str) or not scope_name.strip():
            raise FormalResolutionError(f"UNIT_MONITOR_SCOPE_MISSING: {key}")
        scopes.append(scope_name)
        if not _is_int(report.get("audit_probes")) or report["audit_probes"] < 1:
            raise FormalResolutionError(f"UNIT_MONITOR_NOT_ARMED: {key}")
        observed_limits = report.get("limits")
        if (
            not isinstance(observed_limits, list)
            or not observed_limits
            or any(not isinstance(item, str) or not item.strip() for item in observed_limits)
        ):
            # 観測限界の宣言が無い監視を「完全に見た」として扱わない。
            raise FormalResolutionError(f"UNIT_MONITOR_LIMITS_MISSING: {key}")
        for item in observed_limits:
            if item not in limits:
                limits.append(item)

        counts = report.get("counts")
        if not isinstance(counts, dict) or set(counts) != set(UNIT_COUNTER_FIELDS):
            raise FormalResolutionError(f"UNIT_COUNTER_SET_INVALID: {key}")
        for field_name in UNIT_COUNTER_FIELDS:
            value = counts[field_name]
            if not _is_int(value):
                raise FormalResolutionError(f"UNIT_COUNTER_UNOBSERVED: {key}.{field_name}")
            if value != 0:
                raise FormalResolutionError(f"UNIT_COUNTER_NONZERO: {key}.{field_name}={value}")
        if row.get("side_effects") != counts:
            raise FormalResolutionError(f"UNIT_COUNTER_MISMATCH: {key}")
        if doc.get("side_effects") != counts:
            raise FormalResolutionError(f"UNIT_EVIDENCE_COUNTER_MISMATCH: {key}")

        children = report.get("child_reports")
        launches = report.get("driver_process_launches")
        if not isinstance(children, list) or not _is_int(launches) or launches != len(children):
            raise FormalResolutionError(f"UNIT_CHILD_COVERAGE_MISSING: {key}")
        for child in children:
            if not isinstance(child, dict):
                raise FormalResolutionError(f"UNIT_CHILD_MONITOR_INVALID: {key}")
            child_scope = child.get("scope")
            if (
                child.get("contract") != MONITOR_CONTRACT
                or child.get("complete") is not True
                or child.get("violations") != []
                or child.get("child_reports") != []
                or child.get("driver_process_launches") != 0
                or not _is_int(child.get("audit_probes"))
                or child["audit_probes"] < 1
                or not isinstance(child_scope, str)
                or not child_scope.strip()
            ):
                raise FormalResolutionError(f"UNIT_CHILD_MONITOR_INVALID: {key}")
            if child.get("counts") != counts:
                raise FormalResolutionError(f"UNIT_CHILD_COUNTER_MISMATCH: {key}")
            if child_scope not in child_scopes:
                child_scopes.append(child_scope)

        if report.get("durability_observation") != POLICY_NOT_APPLICABLE:
            raise FormalResolutionError(f"UNIT_DURABILITY_UNOBSERVED: {key}")
        if (
            row.get("ledger_observed") is not False
            or row.get("observed_event_sequence") != []
            or row.get("ledger_head_before") is not None
            or row.get("ledger_head_after") is not None
        ):
            raise FormalResolutionError(f"UNIT_LEDGER_POLICY_MISMATCH: {key}")

        assertions = report.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            raise FormalResolutionError(f"UNIT_ASSERTION_MISSING: {key}")
        for item in assertions:
            if (
                not isinstance(item, dict)
                or item.get("result") is not True
                or not item.get("expression")
            ):
                raise FormalResolutionError(f"UNIT_ASSERTION_FAILED: {key}: {item}")
        assertion_count += len(assertions)

        if report.get("subject_type") != doc.get("observed_subject_type"):
            raise FormalResolutionError(f"UNIT_SUBJECT_MISMATCH: {key}")
        if row.get("observed_state") != doc.get("observed_state"):
            raise FormalResolutionError(f"UNIT_STATE_MISMATCH: {key}")
        if row.get("observed_error_code") != doc.get("observed_error_code"):
            raise FormalResolutionError(f"UNIT_ERROR_MISMATCH: {key}")
        if row.get("actual_subject_id") != doc.get("actual_subject_id"):
            raise FormalResolutionError(f"UNIT_SUBJECT_ID_MISMATCH: {key}")

    if len(set(nodes)) != len(nodes):
        raise FormalResolutionError(f"UNIT_NODE_DUPLICATE: {key}")
    return {
        "observation_records": len(records),
        "node_ids": sorted(nodes),
        "monitor_scopes": sorted(set(scopes)),
        "child_monitor_scopes": sorted(child_scopes),
        "observation_limits": limits,
        "assertion_count": assertion_count,
    }


def _verify_receipt(execution_dir: Path, key: str, doc: dict[str, Any], raw_path: Path) -> dict:
    """Collector が残した実行Receiptを読み直す。Command と exit code を束縛する。"""
    receipt_path = execution_dir / "execution.json"
    if not receipt_path.is_file():
        raise FormalResolutionError(f"UNIT_EXECUTION_RECEIPT_MISSING: {key}")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("contract") != RECEIPT_CONTRACT or receipt.get("case_id") != key:
        raise FormalResolutionError(f"UNIT_EXECUTION_RECEIPT_INVALID: {key}")
    if receipt.get("exit_code") != 0:
        raise FormalResolutionError(f"UNIT_EXECUTION_NONZERO_EXIT: {key}")
    if receipt.get("raw_observations_hash") != verifier.sha256_file(raw_path):
        raise FormalResolutionError(f"UNIT_EXECUTION_RAW_HASH_MISMATCH: {key}")
    for field_name in ("command", "exit_code", "started_at", "recorded_at", "runner_source_hash"):
        if receipt.get(field_name) != doc.get(field_name):
            raise FormalResolutionError(f"UNIT_EXECUTION_RECEIPT_DIVERGES: {key}.{field_name}")
    return {
        "receipt_path": str(receipt_path),
        "receipt_sha256": verifier.sha256_file(receipt_path),
        "cwd": receipt.get("cwd"),
        "runner_source_hash": receipt.get("runner_source_hash"),
    }


def _git(repo_root: Path, *args: str) -> tuple[int, str]:
    result = subprocess.run(  # noqa: S603
        ["/usr/bin/git", "-c", "core.quotepath=false", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout.strip()


def _attributable(message: str, keys: list[str]) -> bool:
    """Verifier の未達Messageが対象Keyのものかを、構造的な形だけで判定する。

    **部分一致で致命的Errorを捨てない。** ここは「対象の未達が紛れていないか」を
    見るための追加検査であり、対象Caseそのものは1件ずつ独立に検証してある。
    """
    for key in keys:
        if message.startswith(f"{key}:") or message.startswith(f"{key}."):
            return True
        if message in (
            f"required test case not in manifest: {key}",
            f"case not PASS: {key}",
            f"required evidence area not in manifest: {key}",
            f"evidence area not PASS: {key}",
        ):
            return True
    return False


def _verify_case(
    *,
    key: str,
    row: dict[str, Any],
    manifest: dict[str, Any],
    snapshot: dict[str, Any],
    evidence_root: Path,
    scope: str,
) -> dict[str, Any]:
    """1 Case を未変更Verifierの関数で検証する。Findings は Case 単位で持つ。"""
    findings = verifier.Findings()
    if row.get("status") != verifier.PASS:
        raise FormalResolutionError(f"CASE_NOT_PASS: {key} status={row.get('status')!r}")
    declared = row.get("evidence_manifest_hash")
    doc = verifier.check_evidence_file(evidence_root, row, key, declared, findings)
    if doc is None or not findings.ok:
        raise FormalResolutionError(
            f"CASE_EVIDENCE_INVALID: {key}: {findings.errors + findings.missing}"
        )
    path = verifier.resolve_evidence(evidence_root, row["evidence_path"], key, findings)
    if path is None or not findings.ok:
        raise FormalResolutionError(f"CASE_EVIDENCE_PATH_INVALID: {key}")
    measured = verifier.sha256_file(path)
    if measured != declared:
        # Manifest が束縛しているのは File Bytes である。自己Hashで代用しない。
        raise FormalResolutionError(
            f"CASE_EVIDENCE_BYTES_MISMATCH: {key} manifest={declared} actual={measured}"
        )

    verifier.verify_common_evidence(doc, manifest, key, findings)
    exp = snapshot["expectations"].get(key)
    if exp is None:
        raise FormalResolutionError(f"CASE_EXPECTATION_MISSING: {key}")
    test_id, _, case_name = key.partition("/")
    for field_name, actual, want in (
        ("release_scope", doc.get("release_scope"), scope),
        ("test_id", doc.get("test_id"), test_id),
        ("case_id", doc.get("case_id"), case_name),
        ("status", doc.get("status"), row.get("status")),
        ("input_fixture_hash", doc.get("input_fixture_hash"), row.get("input_fixture_hash")),
        (
            "expectation_descriptor_hash",
            doc.get("expectation_descriptor_hash"),
            exp["expectation_descriptor_hash"],
        ),
        ("observed_subject_type", doc.get("observed_subject_type"), exp["expected_subject_type"]),
        ("observed_state", doc.get("observed_state"), exp["expected_state"]),
        ("observed_error_code", doc.get("observed_error_code"), exp["expected_error_code"]),
        ("durability_tier", doc.get("durability_tier"), exp.get("durability_tier")),
    ):
        if actual != want:
            findings.error(f"{key}: evidence {field_name}={actual!r} != expected {want!r}")
    verifier.check_event_observation(doc, exp, key, findings)
    verifier.check_bound_file(
        evidence_root,
        doc.get("input_fixture_path"),
        row.get("input_fixture_hash"),
        f"{key}.input_fixture",
        findings,
    )
    raw_path = verifier.check_bound_file(
        evidence_root,
        doc.get("raw_result_path"),
        doc.get("raw_result_hash"),
        f"{key}.raw_result",
        findings,
    )
    if not verifier.is_hash(doc.get("runner_source_hash")):
        findings.miss(f"{key}.runner_source_hash")
    command = doc.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(arg, str) for arg in command)
    ):
        findings.miss(f"{key}.command(list[str])")
    if doc.get("exit_code") != 0:
        findings.error(f"{key}: exit_code must be 0, got {doc.get('exit_code')!r}")
    assertions = doc.get("assertions")
    if not isinstance(assertions, list) or not assertions:
        findings.miss(f"{key}.assertions")
    else:
        for item in assertions:
            if not isinstance(item, dict) or item.get("result") is not True:
                findings.error(f"{key}: assertion not satisfied: {item}")
    if not findings.ok:
        raise FormalResolutionError(
            f"CASE_CONTRACT_INVALID: {key}: {findings.errors + findings.missing}"
        )
    if raw_path is None:
        raise FormalResolutionError(f"CASE_RAW_RESULT_MISSING: {key}")

    observations = verify_unit_observations(raw_path, key, doc)
    receipt = _verify_receipt(raw_path.parent, key, doc, raw_path)
    return {
        "case_id": key,
        "evidence_path": row["evidence_path"],
        "evidence_manifest_hash": declared,
        "evidence_file_sha256": measured,
        "status": doc["status"],
        "evidence_kind": doc.get("evidence_kind"),
        "event_observation": doc.get("event_observation"),
        "event_observation_policy": doc.get("event_observation_policy"),
        "observed_event_sequence_is_null": doc.get("observed_event_sequence") is None,
        "observed_state": doc.get("observed_state"),
        "observed_error_code": doc.get("observed_error_code"),
        "observed_subject_type": doc.get("observed_subject_type"),
        "actual_subject_id": doc.get("actual_subject_id"),
        "side_effects": doc.get("side_effects"),
        "raw_result_path": doc.get("raw_result_path"),
        "raw_result_hash": doc.get("raw_result_hash"),
        "input_fixture_path": doc.get("input_fixture_path"),
        "input_fixture_hash": doc.get("input_fixture_hash"),
        "command": list(command) if isinstance(command, list) else None,
        "exit_code": doc.get("exit_code"),
        "observations": observations,
        "execution_receipt": receipt,
    }


def _verify_unit_area(
    *,
    manifest: dict[str, Any],
    snapshot: dict[str, Any],
    evidence_root: Path,
    unit_case_ids: list[str],
) -> dict[str, Any]:
    """`unit_case_suite` の Case 集合と Case File Hash が正式Manifestと一致すること。"""
    rows = [
        a for a in (manifest.get("required_evidence_areas") or []) if a.get("area") == UNIT_AREA
    ]
    if len(rows) != 1:
        raise FormalResolutionError(f"UNIT_AREA_MISSING_OR_DUPLICATE: {len(rows)} 件")
    row = rows[0]
    findings = verifier.Findings()
    if row.get("status") != verifier.PASS:
        raise FormalResolutionError(f"UNIT_AREA_NOT_PASS: {row.get('status')!r}")
    doc = verifier.check_evidence_file(
        evidence_root, row, UNIT_AREA, row.get("evidence_manifest_hash"), findings
    )
    if doc is None or not findings.ok:
        raise FormalResolutionError(
            f"UNIT_AREA_EVIDENCE_INVALID: {findings.errors + findings.missing}"
        )
    verifier.verify_common_evidence(doc, manifest, UNIT_AREA, findings)
    if doc.get("area") != UNIT_AREA or doc.get("status") != row.get("status"):
        findings.error(f"{UNIT_AREA}: area/status が Manifest と一致しない")
    summary = doc.get("summary")
    if not isinstance(summary, dict) or not summary:
        raise FormalResolutionError("UNIT_AREA_SUMMARY_MISSING")

    declared_ids = summary.get("case_ids")
    if not isinstance(declared_ids, list) or sorted(declared_ids) != unit_case_ids:
        raise FormalResolutionError(
            f"UNIT_AREA_CASE_SET_MISMATCH: area={declared_ids!r} snapshot={unit_case_ids!r}"
        )
    if len(set(declared_ids)) != len(declared_ids):
        raise FormalResolutionError("UNIT_AREA_DUPLICATE_CASE_IDS")
    if summary.get("failing_case_ids"):
        raise FormalResolutionError(f"UNIT_AREA_FAILING_CASES: {summary['failing_case_ids']}")
    if summary.get("unit_case_count") != len(unit_case_ids):
        raise FormalResolutionError("UNIT_AREA_COUNT_MISMATCH")
    for field_name in ("design_sha256", "registry_snapshot_hash"):
        if summary.get(field_name) != snapshot.get(field_name):
            raise FormalResolutionError(f"UNIT_AREA_BINDING_MISMATCH: {field_name}")

    manifest_rows = {
        f"{c.get('test_id')}/{c.get('case_id')}": c for c in (manifest.get("test_cases") or [])
    }
    measured_hashes: list[str] = []
    for key in unit_case_ids:
        case_row = manifest_rows.get(key)
        if case_row is None:
            raise FormalResolutionError(f"UNIT_AREA_CASE_NOT_IN_MANIFEST: {key}")
        path = verifier.resolve_evidence(
            evidence_root, case_row.get("evidence_path"), f"{UNIT_AREA}/{key}", findings
        )
        if path is None:
            raise FormalResolutionError(f"UNIT_AREA_CASE_PATH_INVALID: {key}")
        measured = verifier.sha256_file(path)
        if measured != case_row.get("evidence_manifest_hash"):
            raise FormalResolutionError(f"UNIT_AREA_CASE_BYTES_MISMATCH: {key}")
        measured_hashes.append(measured)
    declared_hashes = summary.get("case_evidence_hashes")
    if not isinstance(declared_hashes, list) or sorted(declared_hashes) != sorted(measured_hashes):
        raise FormalResolutionError("UNIT_AREA_CASE_HASH_SET_MISMATCH")
    if not findings.ok:
        raise FormalResolutionError(
            f"UNIT_AREA_CONTRACT_INVALID: {findings.errors + findings.missing}"
        )
    return {
        "area": UNIT_AREA,
        "status": doc["status"],
        "evidence_path": row["evidence_path"],
        "evidence_manifest_hash": row["evidence_manifest_hash"],
        "evidence_file_sha256": verifier.sha256_file(
            evidence_root.resolve() / row["evidence_path"]
        ),
        "case_ids": sorted(declared_ids),
        "case_evidence_hashes": sorted(measured_hashes),
    }


def verify_formal_evidence(
    *,
    repo_root: Path,
    evidence_root: Path,
    manifest_path: Path,
    report_path: Path,
    scope: str,
    expected_commit: str,
    target_case_ids: list[str],
    unit_case_ids: list[str],
) -> dict[str, Any]:
    """正式Manifest／Report／Case Evidenceを実Bytesへ束縛して検証する。

    全体の `BLOCKED_EVIDENCE_MISSING` は本Taskと無関係な残件を含む。対象Case・
    Unit領域・共通束縛だけを閉じる条件とし、除外した未達は数えて残す。
    """
    repo_root = repo_root.resolve()
    evidence_root = evidence_root.resolve()
    manifest_path = manifest_path.resolve()
    report_path = report_path.resolve()
    if not evidence_root.is_dir():
        raise FormalResolutionError(f"MISSING_EVIDENCE_ROOT: {evidence_root}")
    if evidence_root == repo_root or repo_root in evidence_root.parents:
        raise FormalResolutionError(f"EVIDENCE_IN_REPOSITORY: {evidence_root}")
    for path in (manifest_path, report_path):
        if not path.is_file():
            raise FormalResolutionError(f"MISSING_INPUT: {path}")
        if evidence_root not in path.parents:
            raise FormalResolutionError(f"INPUT_OUTSIDE_EVIDENCE_ROOT: {path}")

    snapshot_path = repo_root / "registry-snapshot.json"
    design_path = repo_root / "design-v1.25-runtime-go.md"
    for path in (snapshot_path, design_path):
        if not path.is_file():
            raise FormalResolutionError(f"MISSING_CANON: {path}")
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    recomputed = domain_hash(
        "FDE-HARNESS/registry-snapshot/1/",
        {k: v for k, v in snapshot.items() if k not in ("registry_snapshot_hash", "generated_at")},
    )
    if snapshot.get("registry_snapshot_hash") != recomputed:
        raise FormalResolutionError("REGISTRY_SNAPSHOT_SELF_HASH_MISMATCH")
    design_sha256 = verifier.sha256_file(design_path)
    if snapshot.get("design_sha256") != design_sha256:
        raise FormalResolutionError(
            f"DESIGN_HASH_NOT_BOUND_TO_SNAPSHOT: design={design_sha256} "
            f"snapshot={snapshot.get('design_sha256')}"
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_sha256 = verifier.sha256_file(manifest_path)
    try:
        _validate_binding(snapshot, manifest, scope, evidence_root)
    except AreaEvidenceError as exc:
        raise FormalResolutionError(f"MANIFEST_BINDING_INVALID: {exc}") from exc

    if not verifier.GIT_SHA_RE.fullmatch(expected_commit or ""):
        raise FormalResolutionError(f"INVALID_IMPLEMENTATION_COMMIT: {expected_commit!r}")
    if manifest.get("implementation_commit_sha") != expected_commit:
        # 「どのCommitの証跡か」を明示入力と突合する。Manifestの自己申告だけで通さない。
        raise FormalResolutionError(
            f"COMMIT_MISMATCH: manifest={manifest.get('implementation_commit_sha')} "
            f"expected={expected_commit}"
        )
    exists, _ = _git(repo_root, "cat-file", "-e", f"{expected_commit}^{{commit}}")
    if exists != 0:
        raise FormalResolutionError(f"UNKNOWN_IMPLEMENTATION_COMMIT: {expected_commit}")
    ancestor, _ = _git(repo_root, "merge-base", "--is-ancestor", expected_commit, "HEAD")
    _, head_sha = _git(repo_root, "rev-parse", "HEAD")

    report = json.loads(report_path.read_text(encoding="utf-8"))
    verifier_path = repo_root / "verify_runtime_go.py"
    for field_name, want in (
        ("manifest_sha256", manifest_sha256),
        ("implementation_commit_sha", expected_commit),
        ("release_scope", scope),
        ("design_sha256", design_sha256),
        ("registry_snapshot_hash", snapshot["registry_snapshot_hash"]),
        ("verifier_source_sha256", verifier.sha256_file(verifier_path)),
        ("verifier_version", verifier.VERIFIER_VERSION),
    ):
        if report.get(field_name) != want:
            raise FormalResolutionError(
                f"REPORT_NOT_BOUND: {field_name}={report.get(field_name)!r} != {want!r}"
            )

    # まず対象を1件ずつ独立に検証する。Findings を Case 単位で持つので、
    # どの未達がどの Case のものかを Message の部分一致ではなく構造で決められる。
    rows: dict[str, dict[str, Any]] = {}
    for case in manifest.get("test_cases") or []:
        key = f"{case.get('test_id')}/{case.get('case_id')}"
        if key in rows:
            raise FormalResolutionError(f"DUPLICATE_MANIFEST_ROW: {key}")
        rows[key] = case
    cases = []
    for key in target_case_ids:
        row = rows.get(key)
        if row is None:
            raise FormalResolutionError(f"TARGET_CASE_NOT_IN_MANIFEST: {key}")
        cases.append(
            _verify_case(
                key=key,
                row=row,
                manifest=manifest,
                snapshot=snapshot,
                evidence_root=evidence_root,
                scope=scope,
            )
        )
    area = _verify_unit_area(
        manifest=manifest,
        snapshot=snapshot,
        evidence_root=evidence_root,
        unit_case_ids=unit_case_ids,
    )

    # そのうえで全体像を測る。errors はどこであっても能動的な不一致なので受理しない。
    # missing は F-2 と無関係な残件を含むため、対象へ帰属するものだけを拒否する。
    findings = verifier.Findings()
    seen: dict[str, str] = {}
    verifier.verify_environment(manifest, evidence_root, findings)
    verifier.verify_cases(manifest, snapshot, scope, evidence_root, findings, seen)
    verifier.verify_gates(manifest, snapshot, scope, evidence_root, findings, seen)
    verifier.verify_areas(manifest, snapshot, scope, evidence_root, findings, seen)
    if findings.errors:
        raise FormalResolutionError(f"MANIFEST_ERRORS_PRESENT: {findings.errors[:10]}")
    scoped_keys = sorted(set(target_case_ids) | {UNIT_AREA})
    blocking = [m for m in findings.missing if _attributable(m, scoped_keys)]
    if blocking:
        raise FormalResolutionError(f"TARGET_EVIDENCE_MISSING: {blocking}")

    excluded = sorted({m.split(":", 1)[0] for m in findings.missing})
    return {
        "evidence_mode": "formal",
        "verified_with": {
            "verifier_version": verifier.VERIFIER_VERSION,
            "verifier_source_sha256": verifier.sha256_file(verifier_path),
            "evidence_schema_version": verifier.EVIDENCE_SCHEMA_VERSION,
            "verifier_unchanged_by_this_task": True,
        },
        "canon": {
            "design_path": design_path.name,
            "design_sha256": design_sha256,
            "registry_snapshot_hash": snapshot["registry_snapshot_hash"],
            "registry_snapshot_file_sha256": verifier.sha256_file(snapshot_path),
            "schema_catalog_hash": snapshot.get("schema_catalog_hash"),
            "release_scope": scope,
        },
        "measured_source": {
            "implementation_commit_sha": expected_commit,
            "commit_is_ancestor_of_resolving_head": ancestor == 0,
            "resolving_head_sha": head_sha,
            "source_tree_clean": manifest.get("source_tree_clean"),
            "schema_set_hash": manifest.get("schema_set_hash"),
            "migration_head": manifest.get("migration_head"),
            "runtime_environment_hash": (manifest.get("runtime_environment") or {}).get(
                "environment_manifest_hash"
            ),
        },
        "evidence_inputs": {
            "evidence_root": str(evidence_root),
            "manifest_path": str(manifest_path),
            "manifest_sha256": manifest_sha256,
            "verification_report_path": str(report_path),
            "verification_report_sha256": verifier.sha256_file(report_path),
            "verification_report_decision": report.get("decision"),
        },
        "target_cases": cases,
        "unit_area": area,
        "coverage": {
            "target_case_count": len(cases),
            "unit_case_count": len(unit_case_ids),
            "manifest_case_rows": len(rows),
            "manifest_pass_rows": sum(
                1 for row in rows.values() if row.get("status") == verifier.PASS
            ),
            "required_case_count": len(snapshot["scopes"][scope]["required_cases"]),
            "required_gate_count": len(snapshot["scopes"][scope]["required_gate_ids"]),
            "required_area_count": len(snapshot["scopes"][scope]["required_evidence_areas"]),
        },
        "out_of_scope": {
            "note": (
                "本Taskの対象はF-2のUnit Caseと unit_case_suite 領域だけである。"
                "以下はF-2と無関係な残件であり、Runtime GO の未達として残る。"
                "本Recordの解消は全体GOを主張しない。"
            ),
            "global_error_count": len(findings.errors),
            "global_missing_count": len(findings.missing),
            "verification_report_decision": report.get("decision"),
            "excluded_missing_subjects": excluded,
        },
    }
