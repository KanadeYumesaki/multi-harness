from __future__ import annotations

import pytest

from harness.domain.effect import (
    EffectState,
    OperationJournal,
    RecoveryDecision,
    RecoveryObservation,
    decide_recovery,
)
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_canonical

pytestmark = pytest.mark.unit


def _hash(label: str):
    return hash_canonical({"label": label}, artifact_type="test-value", schema_major=1)


def _journal() -> OperationJournal:
    return OperationJournal(
        operation_journal_id="journal-1",
        operation_id="operation-1",
        effect_id="effect-1",
        run_id="run-1",
        action_id="action-1",
        attempt_id="attempt-1",
        operation_type="LOCAL_FILE_COMMIT",
        target_resource_identity="workspace:demo/file.txt",
        fencing_token=3,
        before_hash=_hash("before"),
        expected_after_hash=_hash("after"),
        prepared_event_id="event-prepared",
        prepared_at="2026-08-15T00:00:00Z",
        durability_level="STORAGE_SYNC",
    )


def test_effect_journal_requires_durable_prepare_before_attempt() -> None:
    journal = _journal()
    assert journal.state is EffectState.PREPARED_DURABLE

    attempted = journal.mark_execution_attempted(
        event_id="event-attempted", at="2026-08-15T00:01:00Z"
    )
    observed = attempted.mark_observed(
        observed_hash=journal.expected_after_hash,
        observation_method="reopen-and-hash",
        at="2026-08-15T00:02:00Z",
    )
    receipted = observed.store_receipt("receipt-1")

    assert receipted.state is EffectState.RECEIPT_DURABLE
    with pytest.raises(HarnessError) as error:
        journal.mark_observed(
            observed_hash=journal.expected_after_hash,
            observation_method="reopen-and-hash",
            at="2026-08-15T00:02:00Z",
        )
    assert error.value.code is ErrorCode.EFFECT_UNKNOWN


def test_recovery_requires_evidence_before_retry_and_never_retries_unknown_effect() -> None:
    journal = _journal().mark_execution_attempted(
        event_id="event-attempted", at="2026-08-15T00:01:00Z"
    )
    unknown = decide_recovery(
        journal,
        RecoveryObservation(
            observed_hash=None,
            base_hash_matches=False,
            effect_absence_proven=False,
            receipt_exists=False,
        ),
    )
    assert unknown is RecoveryDecision.EFFECT_UNKNOWN

    retriable = decide_recovery(
        journal,
        RecoveryObservation(
            observed_hash=journal.before_hash,
            base_hash_matches=True,
            effect_absence_proven=True,
            receipt_exists=False,
        ),
    )
    assert retriable is RecoveryDecision.REQUIRES_NEW_APPROVED_ATTEMPT


def test_recovery_completes_only_after_observed_hash_and_receipt_evidence() -> None:
    journal = _journal().mark_execution_attempted(
        event_id="event-attempted", at="2026-08-15T00:01:00Z"
    )
    observed = decide_recovery(
        journal,
        RecoveryObservation(
            observed_hash=journal.expected_after_hash,
            base_hash_matches=False,
            effect_absence_proven=False,
            receipt_exists=False,
        ),
    )
    assert observed is RecoveryDecision.COMPLETE_OBSERVATION_AND_RECEIPT

    receipted = journal.mark_observed(
        observed_hash=journal.expected_after_hash,
        observation_method="reopen-and-hash",
        at="2026-08-15T00:02:00Z",
    ).store_receipt("receipt-1")
    assert (
        decide_recovery(
            receipted,
            RecoveryObservation(
                observed_hash=journal.expected_after_hash,
                base_hash_matches=False,
                effect_absence_proven=False,
                receipt_exists=True,
            ),
        )
        is RecoveryDecision.COMPLETE_ACTION_COMMIT
    )
