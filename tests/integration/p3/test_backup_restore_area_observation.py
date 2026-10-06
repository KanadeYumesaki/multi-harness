"""`backup_restore` 領域の観測を**実際に1回動かし**、述語が欠陥を落とすことを確かめる。

## 何を測るか

`tools/backup_restore_observation.py` を本物の合成環境で1回走らせ、
`tools/backup_restore_predicates.py` が全Checkを `PASS` にすることを見る。そのうえで、
記録を1箇所ずつ壊して**非PASSへ落ちること**を確かめる。落ちない述語は、
本番が壊れたときも PASS を出す。

## Gateを無視する欠陥は、専用Fixtureで注入する

共有の稼働Codeを書き換えず、この試験Process内だけで `IntakeGate.admission` を
「常に通す」実装へ差し替える。子Processの `plan-task` は本物のままなので、
Collector が「区間内で作用が起きた」を検出できることを示せる。
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from backup_restore_observation import (
    RawLogs,
    observe_all,
    observe_restore_window,
)
from backup_restore_predicates import evaluate

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def observed(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    """本物の観測を1回だけ実行する。以降の試験はその記録を壊して使う。"""
    base = tmp_path_factory.mktemp("backup-restore-area")
    raw = base / "raw"
    raw.mkdir()
    observations = observe_all(repo=REPO_ROOT, workspace=base / "ws", logs=RawLogs(raw, base))
    return base, observations


def _status(verdict: dict[str, Any], check_id: str) -> str:
    return next(check for check in verdict["checks"] if check["check_id"] == check_id)["status"]


def _broken(observed: tuple[Path, dict[str, Any]]) -> dict[str, Any]:
    return copy.deepcopy(observed[1])


def test_real_observation_satisfies_every_predicate(
    observed: tuple[Path, dict[str, Any]],
) -> None:
    base, observations = observed
    verdict = evaluate(observations, raw_root=base)
    failing = [check for check in verdict["checks"] if check["status"] != "PASS"]
    assert verdict["status"] == "PASS", json.dumps(failing, ensure_ascii=False, indent=2)
    assert observations.get("halted") is None
    window = verdict["counts"]["restore_window"]
    # 拒否した試行数と、実行された作用数を別に数える。
    assert window["executed_effects"] == 0
    assert window["refused_in_process_attempts"] == window["refusal_counter_delta"] > 0
    assert window["refused_cli_attempts"] == 1


def test_missing_observation_is_unverified_not_pass(
    observed: tuple[Path, dict[str, Any]],
) -> None:
    broken = _broken(observed)
    del broken["drain"]["PENDING_APPROVAL"]["snapshot_before_drain"]
    verdict = evaluate(broken, raw_root=observed[0])
    assert _status(verdict, "D-PENDING_APPROVAL") == "UNVERIFIED"
    assert verdict["status"] == "UNVERIFIED"


def test_raw_logs_are_required(observed: tuple[Path, dict[str, Any]]) -> None:
    verdict = evaluate(observed[1], raw_root=None)
    assert verdict["status"] != "PASS"
    assert _status(verdict, "D-QUIESCENT") == "UNVERIFIED"


def test_halted_collection_is_not_pass(observed: tuple[Path, dict[str, Any]]) -> None:
    broken = _broken(observed)
    broken["halted"] = {"scenario": "drain", "reason": "CHILD_TIMEOUT", "partial": None}
    verdict = evaluate(broken, raw_root=observed[0])
    assert _status(verdict, "C-INTEGRITY") == "FAIL"


def test_implementation_from_another_tree_is_not_pass(
    observed: tuple[Path, dict[str, Any]],
) -> None:
    broken = _broken(observed)
    name = next(iter(broken["implementation_modules"]))
    broken["implementation_modules"][name] = "/tmp/elsewhere/intake_gate.py"  # noqa: S108
    verdict = evaluate(broken, raw_root=observed[0])
    assert _status(verdict, "C-INTEGRITY") == "FAIL"


def test_recorded_result_contradicting_the_raw_log_is_fail(
    observed: tuple[Path, dict[str, Any]],
) -> None:
    broken = _broken(observed)
    broken["drain"]["QUIESCENT"]["drain"]["stdout_json"]["state"] = "REJECTED"
    verdict = evaluate(broken, raw_root=observed[0])
    assert _status(verdict, "D-QUIESCENT") == "FAIL"


def test_unreadable_raw_stdout_is_fail(
    observed: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    import hashlib
    import shutil

    base, observations = observed
    copied = tmp_path / "evidence"
    shutil.copytree(base, copied)
    broken = copy.deepcopy(observations)
    call = broken["operator_flow"]["backup_verify"]
    payload = b"not json at all\n"
    (copied / call["stdout"]["path"]).write_bytes(payload)
    call["stdout"]["sha256"] = "sha256:" + hashlib.sha256(payload).hexdigest()
    call["stdout"]["bytes"] = len(payload)
    verdict = evaluate(broken, raw_root=copied)
    assert _status(verdict, "O-CLI-FLOW-CONTENT") == "FAIL"


def test_timed_out_child_is_fail(observed: tuple[Path, dict[str, Any]]) -> None:
    broken = _broken(observed)
    broken["restore_window"]["checkpoint"]["plan_task"]["timed_out"] = True
    verdict = evaluate(broken, raw_root=observed[0])
    assert _status(verdict, "R-WINDOW-REFUSALS") == "FAIL"


@pytest.mark.parametrize(
    "change",
    [
        {"returned": True, "exception_type": None, "error_code": None},
        {"error_code": "APPROVAL_REQUIRED"},
        {"exception_type": "harness.domain.errors.HarnessError"},
        {"caller": "collector-thread"},
    ],
)
def test_window_refusals_must_be_gate_refusals_from_another_thread(
    observed: tuple[Path, dict[str, Any]], change: dict[str, Any]
) -> None:
    broken = _broken(observed)
    broken["restore_window"]["checkpoint"]["attempts"][0].update(change)
    verdict = evaluate(broken, raw_root=observed[0])
    assert _status(verdict, "R-WINDOW-REFUSALS") == "FAIL"


def test_an_executed_effect_inside_the_window_is_fail(
    observed: tuple[Path, dict[str, Any]],
) -> None:
    broken = _broken(observed)
    broken["restore_window"]["checkpoint"]["snapshot_after_attempts"]["effect_port_calls"] += 1
    verdict = evaluate(broken, raw_root=observed[0])
    assert _status(verdict, "R-WINDOW-NO-EFFECT") == "FAIL"


def test_missing_refusal_counter_movement_is_fail(
    observed: tuple[Path, dict[str, Any]],
) -> None:
    broken = _broken(observed)
    snapshot = broken["restore_window"]["checkpoint"]["snapshot_after_attempts"]
    snapshot["operation_control"]["refusals"] -= 1
    verdict = evaluate(broken, raw_root=observed[0])
    assert _status(verdict, "R-WINDOW-NO-EFFECT") == "FAIL"


def test_drain_counts_must_match_the_database(observed: tuple[Path, dict[str, Any]]) -> None:
    broken = _broken(observed)
    broken["drain"]["ACTIVE_CLI_INVOCATION"]["snapshot_before_drain"][
        "cli_invocation_journal_states"
    ] = {"RESPONSE_CAPTURED": 1}
    verdict = evaluate(broken, raw_root=observed[0])
    assert _status(verdict, "D-ACTIVE_CLI_INVOCATION") == "FAIL"


def test_reopened_intake_after_a_failed_restore_is_fail(
    observed: tuple[Path, dict[str, Any]],
) -> None:
    broken = _broken(observed)
    snapshot = broken["failure_retention"]["COPY_FAILURE"]["separate_runtime"]["snapshot_after"]
    snapshot["operation_control"]["mode"] = "OPEN"
    verdict = evaluate(broken, raw_root=observed[0])
    assert _status(verdict, "R-COPY_FAILURE") == "FAIL"


def test_a_restore_start_that_deleted_the_conflict_is_fail(
    observed: tuple[Path, dict[str, Any]],
) -> None:
    broken = _broken(observed)
    entry = broken["restore_start_conflicts"]["PENDING_APPROVAL"]
    entry["snapshot_after_verify"]["approval_grant_statuses"] = {}
    verdict = evaluate(broken, raw_root=observed[0])
    assert _status(verdict, "R-START-PENDING_APPROVAL") == "FAIL"


@pytest.mark.parametrize(
    ("field", "value"),
    [("execution", "PERFORMED"), ("execution", "DONE")],
)
def test_a_claimed_resume_execution_is_not_pass(
    observed: tuple[Path, dict[str, Any]], field: str, value: str
) -> None:
    broken = _broken(observed)
    broken["operator_flow"]["resume"][field] = value
    # The fixture now really resumes. Recreate the false claim by retaining the
    # observed stopped state; changing PERFORMED to itself is no longer a defect.
    broken["operator_flow"]["resume"]["snapshot_after"] = copy.deepcopy(
        broken["operator_flow"]["snapshot_after_drain"]
    )
    verdict = evaluate(broken, raw_root=observed[0])
    assert _status(verdict, "O-RESUME-NOT-FAKED") == "FAIL"


def test_drain_verdict_presented_as_a_resume_is_fail(
    observed: tuple[Path, dict[str, Any]],
) -> None:
    broken = _broken(observed)
    broken["operator_flow"]["resume"]["decision"]["meaning"] = "INTAKE_RESUMED"
    verdict = evaluate(broken, raw_root=observed[0])
    assert _status(verdict, "O-RESUME-NOT-FAKED") == "FAIL"


def test_content_equality_is_not_taken_from_counts_alone(
    observed: tuple[Path, dict[str, Any]],
) -> None:
    broken = _broken(observed)
    restored = broken["operator_flow"]["content_comparison"]["restored"]
    restored["artifacts"][0]["hash"] = "sha256:" + "0" * 64
    verdict = evaluate(broken, raw_root=observed[0])
    assert _status(verdict, "O-CLI-FLOW-CONTENT") == "FAIL"


@contextmanager
def _gate_that_admits_everything() -> Iterator[None]:
    """専用Fixture。試験Process内だけで受付Gateを「常に通す」欠陥へ差し替える。"""
    from harness.application import intake_gate

    original = intake_gate.IntakeGate.admission

    @contextmanager
    def admission(self: Any, request_id: str, *, kind: str = "intake") -> Iterator[None]:
        yield

    intake_gate.IntakeGate.admission = admission  # type: ignore[method-assign]
    try:
        yield
    finally:
        intake_gate.IntakeGate.admission = original  # type: ignore[method-assign]


def test_collector_detects_a_gate_that_ignores_the_restore_window(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    with _gate_that_admits_everything():
        observations = observe_all(
            repo=REPO_ROOT,
            workspace=tmp_path / "ws",
            logs=RawLogs(raw, tmp_path),
            scenarios=(("restore_window", observe_restore_window),),
        )
    verdict = evaluate(observations, raw_root=tmp_path)
    assert _status(verdict, "R-WINDOW-REFUSALS") == "FAIL"
    assert _status(verdict, "R-WINDOW-NO-EFFECT") == "FAIL"
    assert verdict["status"] == "FAIL"
    assert verdict["counts"]["restore_window"]["executed_effects"] > 0


@pytest.mark.parametrize(
    "missing", ["inspection", "call", "replay", "intake_after", "verify_ledger"]
)
def test_operator_resume_requires_actual_execution_evidence(
    observed: tuple[Path, dict[str, Any]], missing: str
) -> None:
    broken = _broken(observed)
    del broken["operator_flow"]["resume"][missing]
    assert _status(evaluate(broken, raw_root=observed[0]), "O-RESUME-NOT-FAKED") == "UNVERIFIED"


def test_resume_with_intake_still_stopped_is_not_pass(
    observed: tuple[Path, dict[str, Any]],
) -> None:
    broken = _broken(observed)
    broken["operator_flow"]["resume"]["snapshot_after"]["operation_control"]["mode"] = "DRAINING"
    assert _status(evaluate(broken, raw_root=observed[0]), "O-RESUME-NOT-FAKED") == "FAIL"
