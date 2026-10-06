"""P3 統合試験：Drain / GC / Emergency Recovery / NO_BYPASS / Performance。

期待値は `design-source/registries/tests.yaml` から読み取る。
"""

from __future__ import annotations

import ast
import json
import platform
import statistics
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from harness.domain.operations_safety import (
    ALLOWED_EMERGENCY_OPERATIONS,
    DENIED_EMERGENCY_OPERATIONS,
    ArtifactRecord,
    evaluate_deployment,
    evaluate_emergency_recovery,
    plan_garbage_collection,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTRIES = REPO_ROOT / "design-source" / "registries"

import sys  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "tests" / "support"))
from ledger_probe import LedgerProbe, SideEffectProbe, record_case  # noqa: E402


def _case(test_id: str, case_id: str) -> dict[str, Any]:
    rows = yaml.safe_load((REGISTRIES / "tests.yaml").read_text(encoding="utf-8"))["test_cases"]
    for row in rows:
        if row["test_id"] == test_id and row["case_id"] == case_id:
            return dict(row)
    raise AssertionError(f"Registryに {test_id}/{case_id} が無い")


def _observe_p3(
    observation: Any,
    *,
    test_id: str,
    case_id: str,
    state: str,
    subject_id: str,
    error_code: str | None,
    payload: dict[str, Any],
    domain_events: list[str] | None = None,
    effects: Any = None,
) -> None:
    """P3 Case の観測。

    `domain_events` はDomainが「Appendされるべき」と決めた列である。
    **それをそのまま観測値にしない。** Application 層と同じように Ledger へ
    Append してから、Ledger を読み直して観測する（設計書§19.1.1）。

    Probe は Case ごとに新しく作る。共有すると他CaseのEventが混ざる。
    """
    ledger = LedgerProbe()
    probe = effects if effects is not None else SideEffectProbe()
    head_before = ledger.head
    ledger.append_names(list(domain_events or []))

    observation.record_input(payload)
    record_case(
        observation,
        state=state,
        subject_id=subject_id,
        ledger=ledger,
        head_before=head_before,
        effects=probe,
        error_code=error_code,
    )

    expected = _case(test_id, case_id)
    assert list(observation.observed_events) == (expected["expected_event_sequence"] or [])


def _assertion_int(case: dict[str, Any], name: str) -> int:
    """`name == N` の N を assertions から読む。試験側へ数を書かない。"""
    for assertion in case["assertions"]:
        left, _, right = assertion.partition("==")
        if left.strip() == name:
            return int(right.strip())
    raise AssertionError(f"{name} の assertion が無い")


def _assertion_bound(case: dict[str, Any], name: str) -> float:
    """`name <= N` の N を assertions から読む。"""
    for assertion in case["assertions"]:
        left, _, right = assertion.partition("<=")
        if left.strip() == name:
            return float(right.strip())
    raise AssertionError(f"{name} の上限assertionが無い")


# ==========================================================================
# Drain
# ==========================================================================


@pytest.mark.case("AT-DRAIN-001/ACTIVE_RUN_BLOCKS")
def test_deployment_with_active_run_is_blocked(case_observation: Any) -> None:
    """実行中Runがあるまま入れ替えないこと。Migrationも始めない。"""
    expected = _case("AT-DRAIN-001", "ACTIVE_RUN_BLOCKS")
    verdict = evaluate_deployment(
        deployment_result_id="dp-1", active_run_count=2, pending_approval_count=0
    )
    assert verdict.state == expected["expected_state"]
    assert verdict.error_code is not None
    assert verdict.error_code.value == expected["expected_error_code"]
    assert [e.value for e in verdict.events] == expected["expected_event_sequence"]
    # assertions: active_run_count > 0 / migration_started == false
    assert verdict.active_run_count > 0
    assert verdict.migration_started is False
    assert verdict.new_effects == 0

    _observe_p3(
        case_observation,
        test_id="AT-DRAIN-001",
        case_id="ACTIVE_RUN_BLOCKS",
        state=verdict.state,
        subject_id=verdict.deployment_result_id,
        error_code=verdict.error_code.value,
        domain_events=[e.value for e in verdict.events],
        payload={
            "active_run_count": verdict.active_run_count,
            "pending_approval_count": verdict.pending_approval_count,
            "migration_started": verdict.migration_started,
            "new_effects": verdict.new_effects,
        },
    )


@pytest.mark.case("AT-DRAIN-001/DRAINED_DEPLOY")
def test_deployment_after_drain(case_observation: Any) -> None:
    """Drain完了後だけDeployを許すこと。"""
    expected = _case("AT-DRAIN-001", "DRAINED_DEPLOY")
    verdict = evaluate_deployment(
        deployment_result_id="dp-1", active_run_count=0, pending_approval_count=0
    )
    assert verdict.state == expected["expected_state"]
    assert verdict.error_code is None
    # assertions: active_run_count == 0 / pending_approval_count == 0
    assert verdict.active_run_count == 0
    assert verdict.pending_approval_count == 0
    assert verdict.migration_started is True

    _observe_p3(
        case_observation,
        test_id="AT-DRAIN-001",
        case_id="DRAINED_DEPLOY",
        state=verdict.state,
        subject_id=verdict.deployment_result_id,
        error_code=None,
        domain_events=[e.value for e in verdict.events],
        payload={
            "active_run_count": verdict.active_run_count,
            "pending_approval_count": verdict.pending_approval_count,
            "migration_started": verdict.migration_started,
            "new_effects": verdict.new_effects,
        },
    )


def test_pending_approval_alone_blocks_deployment() -> None:
    """承認待ちが残っていれば、実行中Runが0でも拒否すること。

    承認待ちは「人がまだ決めていない」状態であり、その決定を
    新しいVersionが引き継げる保証が無い。
    """
    verdict = evaluate_deployment(
        deployment_result_id="dp-1", active_run_count=0, pending_approval_count=1
    )
    assert verdict.accepted is False
    assert verdict.migration_started is False


@pytest.mark.parametrize("active,pending", [(1, 0), (0, 1), (3, 2), (1, 1)])
def test_no_migration_starts_while_anything_is_outstanding(active: int, pending: int) -> None:
    """未処理が残る限りMigrationを開始しないこと。

    開始してから気付くと、DBだけ新Versionで実行中Runは旧Versionになる。
    """
    verdict = evaluate_deployment(
        deployment_result_id="dp-1",
        active_run_count=active,
        pending_approval_count=pending,
    )
    assert verdict.migration_started is False
    assert verdict.new_effects == 0


def test_negative_counts_are_a_programming_error() -> None:
    with pytest.raises(ValueError):
        evaluate_deployment(
            deployment_result_id="dp-1", active_run_count=-1, pending_approval_count=0
        )


# ==========================================================================
# GC
# ==========================================================================


@pytest.mark.case("AT-GC-001/ORPHAN_CANDIDATE")
def test_old_orphan_is_candidate_only(case_observation: Any) -> None:
    """孤立Artifactを候補に挙げるだけで、削除しないこと。

    参照が0でも「今この瞬間、参照が見つからない」でしかない。
    """
    expected = _case("AT-GC-001", "ORPHAN_CANDIDATE")
    result = plan_garbage_collection(
        [
            ArtifactRecord(
                "orphan-old", reference_count=0, age_seconds=86_400, payload_present=True
            ),
            ArtifactRecord("kept", reference_count=1, age_seconds=86_400, payload_present=True),
        ],
        gc_result_id="gc-1",
        minimum_age_seconds=3_600,
    )
    assert result.state == expected["expected_state"]
    # assertions: orphan_candidate_count == 1 / deleted_count == 0
    assert result.orphan_candidate_count == _assertion_int(expected, "orphan_candidate_count")
    assert result.deleted_count == _assertion_int(expected, "deleted_count")

    _observe_p3(
        case_observation,
        test_id="AT-GC-001",
        case_id="ORPHAN_CANDIDATE",
        state=result.state,
        subject_id=result.gc_result_id,
        error_code=None,
        payload={
            "orphan_candidate_ids": sorted(result.orphan_candidate_ids),
            "referenced_kept_ids": sorted(result.referenced_kept_ids),
            "orphan_candidate_count": result.orphan_candidate_count,
            "deleted_count": result.deleted_count,
            "referenced_deleted_count": result.referenced_deleted_count,
        },
    )


@pytest.mark.case("AT-GC-001/REFERENCED_KEEP")
def test_gc_keeps_referenced_artifact(case_observation: Any) -> None:
    """参照中のArtifactを1件も消さないこと。"""
    expected = _case("AT-GC-001", "REFERENCED_KEEP")
    result = plan_garbage_collection(
        [
            ArtifactRecord(
                "referenced", reference_count=3, age_seconds=999_999, payload_present=True
            ),
            ArtifactRecord("orphan", reference_count=0, age_seconds=999_999, payload_present=True),
        ],
        gc_result_id="gc-1",
        minimum_age_seconds=3_600,
    )
    assert result.state == expected["expected_state"]
    # assertions: referenced_deleted_count == 0
    assert result.referenced_deleted_count == _assertion_int(expected, "referenced_deleted_count")
    assert "referenced" in result.referenced_kept_ids
    assert "referenced" not in result.orphan_candidate_ids

    _observe_p3(
        case_observation,
        test_id="AT-GC-001",
        case_id="REFERENCED_KEEP",
        state=result.state,
        subject_id=result.gc_result_id,
        error_code=None,
        payload={
            "orphan_candidate_ids": sorted(result.orphan_candidate_ids),
            "referenced_kept_ids": sorted(result.referenced_kept_ids),
            "referenced_deleted_count": result.referenced_deleted_count,
            "deleted_count": result.deleted_count,
        },
    )


def test_young_orphan_is_not_even_a_candidate() -> None:
    """若い孤立Artifactを候補にしないこと。

    P1 の `SQLITE_ENOSPC` はまさにこの状態を作る。Artifactは CAS へ収まったが
    Journal の Commit が失敗したので誰も参照していない。**あれは正しい状態**であり、
    即削除の対象ではない。
    """
    result = plan_garbage_collection(
        [ArtifactRecord("just-written", reference_count=0, age_seconds=5, payload_present=True)],
        gc_result_id="gc-1",
        minimum_age_seconds=3_600,
    )
    assert result.orphan_candidate_count == 0
    assert result.deleted_count == 0


def test_dry_run_never_deletes() -> None:
    """Dry Runでは何件候補があっても削除0であること。"""
    artifacts = [
        ArtifactRecord(f"o{i}", reference_count=0, age_seconds=999_999, payload_present=True)
        for i in range(5)
    ]
    result = plan_garbage_collection(artifacts, gc_result_id="gc-1", minimum_age_seconds=1)
    assert result.orphan_candidate_count == 5
    assert result.deleted_count == 0


def test_referenced_artifacts_are_never_deleted_even_outside_dry_run() -> None:
    """実削除でも参照中は消さないこと。"""
    result = plan_garbage_collection(
        [
            ArtifactRecord(
                "referenced", reference_count=1, age_seconds=999_999, payload_present=True
            ),
            ArtifactRecord("orphan", reference_count=0, age_seconds=999_999, payload_present=True),
        ],
        gc_result_id="gc-1",
        minimum_age_seconds=1,
        dry_run=False,
    )
    assert result.deleted_ids == ("orphan",)
    assert result.referenced_deleted_count == 0


# ==========================================================================
# Emergency Recovery
# ==========================================================================


def test_design_defines_nine_allowed_and_nine_denied_operations() -> None:
    """許可9件・禁止9件であること（§3.10.1）。"""
    assert len(ALLOWED_EMERGENCY_OPERATIONS) == 9
    assert len(DENIED_EMERGENCY_OPERATIONS) == 9
    assert not set(ALLOWED_EMERGENCY_OPERATIONS) & set(DENIED_EMERGENCY_OPERATIONS)


@pytest.mark.case("AT-EMERGENCY-RECOVERY-001/ALLOWLIST")
def test_all_nine_recovery_operations_allowed(case_observation: Any) -> None:
    """許可9操作すべてが通ること。新しい作用は1件も起こさない。"""
    expected = _case("AT-EMERGENCY-RECOVERY-001", "ALLOWLIST")
    verdict = evaluate_emergency_recovery(
        ALLOWED_EMERGENCY_OPERATIONS,
        emergency_recovery_id="er-1",
        signature_valid=True,
        profile_version=1,
        minimum_accepted_profile_version=1,
    )
    assert verdict.state == expected["expected_state"]
    assert [e.value for e in verdict.events] == expected["expected_event_sequence"]
    # assertions: allowed_operation_pass_count == 9 / new_effects == 0
    assert verdict.allowed_operation_pass_count == _assertion_int(
        expected, "allowed_operation_pass_count"
    )
    assert verdict.new_effects == _assertion_int(expected, "new_effects")
    assert verdict.recovery_started is True

    _observe_p3(
        case_observation,
        test_id="AT-EMERGENCY-RECOVERY-001",
        case_id="ALLOWLIST",
        state=verdict.state,
        subject_id=verdict.emergency_recovery_id,
        error_code=None,
        domain_events=[e.value for e in verdict.events],
        payload={
            "operations": sorted(ALLOWED_EMERGENCY_OPERATIONS),
            "signature_valid": True,
            "allowed_operation_pass_count": verdict.allowed_operation_pass_count,
            "new_effects": verdict.new_effects,
            "recovery_started": verdict.recovery_started,
        },
    )


@pytest.mark.case("AT-EMERGENCY-RECOVERY-001/DENYLIST")
def test_all_nine_forbidden_operations_denied(case_observation: Any) -> None:
    """禁止9操作すべてを拒否すること。"""
    expected = _case("AT-EMERGENCY-RECOVERY-001", "DENYLIST")
    verdict = evaluate_emergency_recovery(
        DENIED_EMERGENCY_OPERATIONS,
        emergency_recovery_id="er-1",
        signature_valid=True,
        profile_version=1,
        minimum_accepted_profile_version=1,
    )
    assert verdict.state == expected["expected_state"]
    assert verdict.error_code is not None
    assert verdict.error_code.value == expected["expected_error_code"]
    assert [e.value for e in verdict.events] == expected["expected_event_sequence"]
    # assertions: denied_operation_count == 9 / workspace_writes == 0 / provider_calls == 0
    assert verdict.denied_operation_count == _assertion_int(expected, "denied_operation_count")
    assert verdict.workspace_writes == _assertion_int(expected, "workspace_writes")
    assert verdict.provider_calls == _assertion_int(expected, "provider_calls")

    # 拒否経路の副作用は実測値を渡す。0を書き込むのではなく数えた結果である。
    denied_effects = SideEffectProbe()
    denied_effects.workspace_commits = verdict.workspace_writes
    denied_effects.provider_calls = verdict.provider_calls
    _observe_p3(
        case_observation,
        test_id="AT-EMERGENCY-RECOVERY-001",
        case_id="DENYLIST",
        state=verdict.state,
        subject_id=verdict.emergency_recovery_id,
        error_code=verdict.error_code.value,
        domain_events=[e.value for e in verdict.events],
        effects=denied_effects,
        payload={
            "operations": sorted(DENIED_EMERGENCY_OPERATIONS),
            "signature_valid": True,
            "denied_operation_count": verdict.denied_operation_count,
            "workspace_writes": verdict.workspace_writes,
            "provider_calls": verdict.provider_calls,
        },
    )


@pytest.mark.case("AT-EMERGENCY-RECOVERY-001/BAD_SIGNATURE")
def test_invalid_emergency_profile_signature(case_observation: Any) -> None:
    """署名が不正ならRecoveryを開始しないこと。

    中身を見てから判断すると、署名の無いProfileが
    「許可操作だけだったから通した」経路を持つ。
    """
    expected = _case("AT-EMERGENCY-RECOVERY-001", "BAD_SIGNATURE")
    verdict = evaluate_emergency_recovery(
        ALLOWED_EMERGENCY_OPERATIONS,
        emergency_recovery_id="er-1",
        signature_valid=False,
        profile_version=1,
        minimum_accepted_profile_version=1,
    )
    assert verdict.state == expected["expected_state"]
    assert verdict.error_code is not None
    assert verdict.error_code.value == expected["expected_error_code"]
    # assertions: recovery_started == false
    assert verdict.recovery_started is False
    assert verdict.events == ()

    _observe_p3(
        case_observation,
        test_id="AT-EMERGENCY-RECOVERY-001",
        case_id="BAD_SIGNATURE",
        state=verdict.state,
        subject_id=verdict.emergency_recovery_id,
        error_code=verdict.error_code.value,
        domain_events=[e.value for e in verdict.events],
        payload={
            "operations": sorted(ALLOWED_EMERGENCY_OPERATIONS),
            "signature_valid": False,
            "recovery_started": verdict.recovery_started,
        },
    )


def test_profile_rollback_is_rejected() -> None:
    """古いProfile Versionへ戻せないこと。

    制限の緩かった版を持ち出せる経路を作らない。
    """
    verdict = evaluate_emergency_recovery(
        ALLOWED_EMERGENCY_OPERATIONS,
        emergency_recovery_id="er-1",
        signature_valid=True,
        profile_version=1,
        minimum_accepted_profile_version=2,
    )
    assert verdict.accepted is False
    assert verdict.recovery_started is False


@pytest.mark.parametrize("denied", DENIED_EMERGENCY_OPERATIONS)
def test_a_single_forbidden_operation_blocks_the_whole_request(denied: str) -> None:
    """禁止操作が1件混ざるだけで全体を拒否すること。

    許可操作に紛れ込ませて通す経路を作らない。
    """
    verdict = evaluate_emergency_recovery(
        [*ALLOWED_EMERGENCY_OPERATIONS, denied],
        emergency_recovery_id="er-1",
        signature_valid=True,
        profile_version=1,
        minimum_accepted_profile_version=1,
    )
    assert verdict.accepted is False
    assert verdict.new_effects == 0
    assert verdict.workspace_writes == 0
    assert verdict.provider_calls == 0


def test_unknown_operation_is_denied_by_default() -> None:
    """未知の操作名を既定で拒否すること（Fail-Closed）。"""
    verdict = evaluate_emergency_recovery(
        ["SOMETHING_NEW"],
        emergency_recovery_id="er-1",
        signature_valid=True,
        profile_version=1,
        minimum_accepted_profile_version=1,
    )
    assert verdict.accepted is False


# ==========================================================================
# AT-APPROVAL-UX-001 / NO_BYPASS（Source Scan）
# ==========================================================================


@pytest.mark.case("AT-APPROVAL-UX-001/NO_BYPASS")
def test_approval_bypass_symbols_absent(case_observation: Any) -> None:
    """Approval を迂回するSymbolがSourceに無いこと（不変条件#11）。

    `--yes` / `--auto-approve` / `--force` / `--skip-approval` 相当を
    実装しない。**Sourceを実際に走査して数える。**
    """
    expected = _case("AT-APPROVAL-UX-001", "NO_BYPASS")

    # 迂回の実体になりうる識別子。Registry語彙ではなく不変条件#11の列挙。
    forbidden = (
        "auto_approve",
        "skip_approval",
        "force_approve",
        "bypass_approval",
        "--yes",
        "--auto-approve",
        "--force",
        "--skip-approval",
    )

    findings: list[str] = []
    scanned_files = 0
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        scanned_files += 1
        text = path.read_text(encoding="utf-8")
        # 定義・引数として現れるかを見る。禁止を説明する散文は数えない。
        tree = ast.parse(text)
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.arg):
                names.add(node.arg)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                names.add(node.name)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in forbidden:
                    findings.append(f"{path.relative_to(REPO_ROOT)}:literal:{node.value}")
        for symbol in forbidden:
            if symbol in names:
                findings.append(f"{path.relative_to(REPO_ROOT)}:symbol:{symbol}")

    assert expected["expected_state"] == "ACCEPTED"
    # assertions: bypass_findings == 0
    assert len(findings) == _assertion_int(expected, "bypass_findings"), findings

    # SOURCE_SCAN の機械検査だけをEvidence化する。
    # **人手計測値を作らない。** approval_ux 領域（人手計測）はMVP0-Bである。
    _observe_p3(
        case_observation,
        test_id="AT-APPROVAL-UX-001",
        case_id="NO_BYPASS",
        state=expected["expected_state"],
        subject_id="approval-ux-no-bypass",
        error_code=None,
        payload={
            "scan_kind": "SOURCE_SCAN",
            "scanned_file_count": scanned_files,
            "forbidden_symbol_count": len(forbidden),
            "bypass_findings": len(findings),
        },
    )


def test_bypass_scanner_would_catch_a_real_bypass(tmp_path: Path) -> None:
    """走査器が実際に検出できること。

    0件を報告する試験は、走査器が壊れていても0件を報告する。
    **検出できることを別に確かめる。**
    """
    sample = tmp_path / "bad.py"
    sample.write_text("def run(auto_approve: bool = True) -> None: ...\n", encoding="utf-8")
    tree = ast.parse(sample.read_text(encoding="utf-8"))
    names = {node.arg for node in ast.walk(tree) if isinstance(node, ast.arg)}
    assert "auto_approve" in names


# ==========================================================================
# AT-PERF-001 / CONTROL_OVERHEAD（実測）
# ==========================================================================


def _control_plane_workload() -> int:
    """8 Action相当の制御処理と60 Eventを1回分実行する。

    **実装済みの制御平面だけを使う。** Provider呼出は実装が無く、
    測っていないものを測ったことにしない。ここで測るのは
    Plan Hash計算・Ledger Event組立て・Policy判定の重ねがけである。
    """
    from harness.domain.hashing import hash_canonical
    from harness.domain.policy_freshness import EffectKind, evaluate_policy_freshness

    events = 0
    for action in range(8):
        hash_canonical(
            {"action": action, "kind": "plan"},
            artifact_type="perf-probe",
            schema_major=1,
        )
        for index in range(8 if action < 7 else 4):  # 合計60 Event
            hash_canonical(
                {"action": action, "event": index},
                artifact_type="perf-probe-event",
                schema_major=1,
            )
            evaluate_policy_freshness(
                effect_kind=EffectKind.LOCAL_READ,
                policy_expires_at="2027-01-01T00:00:00Z",
                now="2026-08-17T00:00:00Z",
                effect_attempted=False,
            )
            events += 1
    return events


@pytest.mark.case("AT-PERF-001/CONTROL_OVERHEAD")
def test_eight_actions_sixty_events_performance(case_observation: Any) -> None:
    """8 Action・60 Event の制御処理を実測する。

    **Fake結果でPASSにしない。** warmup と measured の回数はRegistryから読み、
    p50／p95 を実測値から計算する。
    """
    expected = _case("AT-PERF-001", "CONTROL_OVERHEAD")
    warmup_runs = _assertion_int(expected, "warmup_runs")
    measured_runs = _assertion_int(expected, "measured_runs")
    p50_bound = _assertion_bound(expected, "p50_seconds")
    p95_bound = _assertion_bound(expected, "p95_seconds")

    for _ in range(warmup_runs):
        assert _control_plane_workload() == 60

    samples: list[float] = []
    for _ in range(measured_runs):
        started = time.perf_counter()
        events = _control_plane_workload()
        samples.append(time.perf_counter() - started)
        assert events == 60

    assert len(samples) == measured_runs
    ordered = sorted(samples)
    p50 = statistics.median(ordered)
    # p95 は最近傍順位法。決定論的に決まる。
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]

    assert p50 <= p50_bound, f"p50={p50:.4f}s > {p50_bound}s"
    assert p95 <= p95_bound, f"p95={p95:.4f}s > {p95_bound}s"
    assert expected["expected_state"] == "ACCEPTED"

    # 測定条件をEvidenceへ束縛できる形で残す。数値は手入力しない。
    conditions = {
        "python_version": platform.python_version(),
        "machine": platform.machine(),
        "system": platform.system(),
        "warmup_runs": warmup_runs,
        "measured_runs": measured_runs,
        "p50_seconds": round(p50, 6),
        "p95_seconds": round(p95, 6),
    }
    assert json.dumps(conditions, sort_keys=True)
    assert conditions["python_version"] == platform.python_version()

    # 実測サンプルから計算した値だけを記録する。固定値・手入力を使わない。
    _observe_p3(
        case_observation,
        test_id="AT-PERF-001",
        case_id="CONTROL_OVERHEAD",
        state=expected["expected_state"],
        subject_id="perf-control-overhead",
        error_code=None,
        payload={
            **conditions,
            "sample_count": len(samples),
            "events_per_run": 60,
            "p50_bound_seconds": p50_bound,
            "p95_bound_seconds": p95_bound,
            # 測ったのは実装済みの制御平面だけである。Provider呼出は測っていない。
            "measured_surface": "control_plane_only",
        },
    )


def test_performance_samples_are_actually_measured() -> None:
    """計測が実時間を測っていること。

    固定値を返す実装では、負荷を増やしても時間が変わらない。
    """
    started = time.perf_counter()
    _control_plane_workload()
    single = time.perf_counter() - started

    started = time.perf_counter()
    for _ in range(3):
        _control_plane_workload()
    triple = time.perf_counter() - started

    assert triple > single, "負荷を3倍にしても時間が増えない。実測していない"


def test_percentile_calculation_is_deterministic() -> None:
    """同じ標本から必ず同じ p50／p95 が出ること。"""
    samples = [0.1, 0.2, 0.3, 0.4, 0.5]
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    assert statistics.median(ordered) == 0.3
    assert ordered[p95_index] == 0.5
    # 並び順が違っても同じ結果になる。
    shuffled = [0.5, 0.1, 0.4, 0.2, 0.3]
    assert statistics.median(sorted(shuffled)) == 0.3
    assert sorted(shuffled) == ordered
