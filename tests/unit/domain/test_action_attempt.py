from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from harness.domain.attempt import ActionAttempt
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.event_order import EventAppendRequest, verify_append_order
from harness.domain.hashing import hash_canonical
from harness.domain.run_terminal import RunTerminalRequest, evaluate_run_terminal

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "support"))
from case_probe import observe_case
from ledger_probe import LedgerProbe

pytestmark = pytest.mark.unit


def _hash(label: str):
    return hash_canonical({"label": label}, artifact_type="test-value", schema_major=1)


def _attempt() -> ActionAttempt:
    return ActionAttempt(attempt_id="attempt-1", action_id="action-1", attempt_number=1)


def _observe_unreconciled(observation: Any, case_id: str, terminal: str) -> None:
    """未照合Effectがある終端遷移を Canonical 経路で観測する。

    `ActionAttempt` は State しか持たず、Registry が要求する Error Code と
    Event 列を生成しない。終端判定の Canonical 経路は `evaluate_run_terminal`
    であり、同 test_id の別 Case が既にこの経路で実観測している。
    **Domain の予測列をそのまま観測値にせず、Ledger へ積んでから読む。**
    """
    verdict = evaluate_run_terminal(
        RunTerminalRequest(
            run_id="run-1",
            requested_terminal_state=terminal,
            release_decision_id="rd-1",
            unreconciled_effect_count=1,
        ),
        now="2026-08-19T00:00:00Z",
    )
    ledger = LedgerProbe()
    head_before = ledger.head
    ledger.append_names([event.value for event in verdict.events])

    observe_case(
        observation,
        case_id,
        state=verdict.state,
        subject_id="run-1",
        error_code=verdict.error_code.value if verdict.error_code else None,
        ledger=ledger,
        head_before=head_before,
        payload={
            "run_id": "run-1",
            "requested_terminal_state": terminal,
            "release_decision_id": "rd-1",
            "unreconciled_effect_count": 1,
        },
    )


def _observe_action_started_missing(observation: Any) -> None:
    """`ACTION_STARTED` を欠いた Append を Canonical 経路で観測する。

    Registry の `expected_event_sequence` は `['RUNTIME_ATTESTED']` である。
    これは**拒否前に既にAppend済み**の Event であり、拒否された Append は
    何も足さない（設計書§19.1.1）。Ledger へ RUNTIME_ATTESTED を載せてから
    順序違反の Append を試み、Head が動かないことを数える。
    """
    ledger = LedgerProbe()
    ledger.append_names(["RUNTIME_ATTESTED"])
    head_before = ledger.head

    request = EventAppendRequest(
        append_result_id="ar-1",
        stream_id="ACTION_ATTEMPT_STREAM",
        expected_stream_sequence=0,
        event_types=("ACTION_STARTED",),
        attempt_state="RUNTIME_VERIFIED",
    )
    verdict = verify_append_order(request, current_head=head_before)
    assert verdict.attempt_state == "RUNTIME_VERIFIED"

    observe_case(
        observation,
        "AT-EVENT-ORDER-001/ACTION_STARTED_MISSING",
        state=verdict.state,
        subject_id=request.append_result_id,
        error_code=verdict.error_code.value if verdict.error_code else None,
        ledger=ledger,
        head_before=head_before,
        payload={
            "append_result_id": request.append_result_id,
            "stream_id": request.stream_id,
            "expected_stream_sequence": request.expected_stream_sequence,
            "event_types": list(request.event_types),
            "attempt_state": request.attempt_state,
            "current_head": head_before,
        },
    )
    # 拒否された Append は Ledger へ何も足さない。
    assert observation.ledger_head_after == head_before


@pytest.mark.case("AT-EVENT-ORDER-001/ACTION_STARTED_MISSING")
def test_action_attempt_requires_plan_before_claim_and_runtime_before_start(
    case_observation: Any,
) -> None:
    attempt = _attempt()
    with pytest.raises(HarnessError) as claim_error:
        attempt.claim(worker_id="worker-1", claim_id="claim-1")
    assert claim_error.value.code is ErrorCode.EVENT_ORDER_VIOLATION

    planned = attempt.with_plan(_hash("content"), _hash("execution"))
    ready = planned.ready()
    claimed = ready.claim(worker_id="worker-1", claim_id="claim-1")
    leased = claimed.with_lease(lease_id="lease-1", fencing_token=1)
    attested = leased.with_runtime_attestation(_hash("attestation"))
    running = attested.start("2026-08-15T00:00:00Z")

    assert running.state == "RUNNING"
    assert running.execution_plan_hash == planned.execution_plan_hash

    _observe_action_started_missing(case_observation)


@pytest.mark.case("AT-RUN-TERMINAL-001/UNRECONCILED_TO_COMPLETED")
def test_effect_attempt_requires_journal_and_receipt_before_success(
    case_observation: Any,
) -> None:
    running = (
        _attempt()
        .with_plan(_hash("content"), _hash("execution"))
        .ready()
        .claim(worker_id="worker-1", claim_id="claim-1")
        .with_lease(lease_id="lease-1", fencing_token=1)
        .with_runtime_attestation(_hash("attestation"))
        .start("2026-08-15T00:00:00Z")
    )
    with pytest.raises(HarnessError) as success_error:
        running.succeed("2026-08-15T00:01:00Z")
    assert success_error.value.code is ErrorCode.UNRECONCILED_EFFECT_PRESENT

    prepared = running.prepare_effect("journal-1")
    receipted = prepared.with_receipt("receipt-1")
    succeeded = receipted.succeed("2026-08-15T00:01:00Z")
    assert succeeded.state == "SUCCEEDED"
    assert succeeded.receipt_ids == ("receipt-1",)

    _observe_unreconciled(
        case_observation, "AT-RUN-TERMINAL-001/UNRECONCILED_TO_COMPLETED", "COMPLETED"
    )


@pytest.mark.case("AT-RUN-TERMINAL-001/UNRECONCILED_TO_FAILED")
def test_terminal_attempt_cannot_resume_and_failure_requires_error_classification(
    case_observation: Any,
) -> None:
    ready = _attempt().with_plan(_hash("content"), _hash("execution")).ready()
    with pytest.raises(ValueError):
        ready.fail(
            retryable=False,
            error_classification="",
            ended_at="2026-08-15T00:01:00Z",
        )

    failed = ready.fail(
        retryable=False,
        error_classification="VALIDATION_ERROR",
        ended_at="2026-08-15T00:01:00Z",
    )
    with pytest.raises(HarnessError) as resume_error:
        failed.ready()
    assert resume_error.value.code is ErrorCode.EVENT_ORDER_VIOLATION

    _observe_unreconciled(case_observation, "AT-RUN-TERMINAL-001/UNRECONCILED_TO_FAILED", "FAILED")
