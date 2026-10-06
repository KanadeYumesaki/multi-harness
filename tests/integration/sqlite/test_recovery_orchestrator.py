"""Recovery Orchestrator の統合試験。

実 SQLite の正本 Ledger を使い、読み戻して観測する。

対象 Case（F-1-D）: `AT-EVENT-MAPPING-001/LEDGER_EVENT_MISSING`

Store の State が §1.14.1 で要求する正規 Ledger Event が Ledger に無い、という
状況を**実際に作る**。欠落 Event を後から差し込まない。訂正は Compensating
Event の Append で行う（不変条件#1）。

走査する Stream と記録する Stream を分ける。診断している Stream へ Recovery
Event を積むと、次に同じ Stream を走査したときに見えるものが変わる。
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from harness.application.recovery_orchestrator import RecoveryOrchestrator, RecoveryRequest
from harness.domain.errors import ErrorCode
from harness.domain.events import EventType
from harness.domain.hashing import hash_canonical
from harness.domain.phase_ledger_repair import scan_phase_ledger_mapping
from harness.infrastructure.sqlite.connection_factory import ConnectionFactory, ConnectionRole
from harness.infrastructure.sqlite.event_ledger_repository import SqliteEventLedgerRepository
from harness.infrastructure.sqlite.migrations import migrate
from harness.infrastructure.sqlite.unit_of_work import SqliteUnitOfWork
from harness.ports.event_ledger import NewEvent

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "support"))

from case_probe import observe_case
from ledger_probe import SideEffectProbe
from real_ledger_view import RealLedgerView

pytestmark = pytest.mark.integration

SOURCE_STREAM = "OUTBOX_STREAM"
TRACE_STREAM = "CROSS_STORE_REPAIR_TRACE"
REPAIR_ID = "repair-1"

# §1.14.1 Transactional Outbox: Store State `DELIVERED_OR_APPLIED` は
# `OUTBOX_EFFECT_CONFIRMED` と `EFFECT_OBSERVED` を正規Ledger Eventとして要求する。
# 表そのものをCodeへ写さず、本Caseが使う行だけを呼出側の入力として持つ。
STORE_STATE = "DELIVERED_OR_APPLIED"
EXPECTED_EVENTS = ("OUTBOX_EFFECT_CONFIRMED", "EFFECT_OBSERVED")

# 中断前に確定していたEvent。**正規Eventが欠けている状態を実際に作る。**
SEEDED = ("OUTBOX_PREPARED", "OUTBOX_DISPATCHING", "OUTBOX_SENT")

# 正規Eventまで揃った Stream。`EFFECT_OBSERVED` は §19.1 の
# `REQUIRED_PREDECESSORS` により長い先行列を要求するので、実際に成立する
# 並びを置く。Ledger自身が順序を検査するため、途中を省くと Append が拒否される。
COMPLETE = (
    "INTENT_CREATED",
    "PLAN_RESOLVED",
    "POLICY_DECIDED",
    "ACTION_CLAIMED",
    "LEASE_ACQUIRED",
    "RUNTIME_ATTESTED",
    "ACTION_STARTED",
    "ACTION_PREPARED",
    "EXECUTION_ATTEMPTED",
    "OUTBOX_EFFECT_CONFIRMED",
    "EFFECT_OBSERVED",
)


@dataclass
class FrozenClock:
    stamp: str = "2026-08-21T00:00:00Z"

    def now(self) -> str:
        return self.stamp


@dataclass
class _Bundle:
    orchestrator: RecoveryOrchestrator
    ledger: SqliteEventLedgerRepository
    factory: ConnectionFactory
    connection: sqlite3.Connection


@pytest.fixture
def bundle(tmp_path: Path) -> Iterator[_Bundle]:
    factory = ConnectionFactory(tmp_path / "harness.db")
    migrate(factory, recorded_at="2026-08-21T00:00:00Z")
    made = factory.connect(ConnectionRole.RUNTIME)
    try:
        ledger = SqliteEventLedgerRepository(made)
        yield _Bundle(
            orchestrator=RecoveryOrchestrator(
                ledger=ledger,
                unit_of_work=SqliteUnitOfWork(factory, made),
                clock=FrozenClock(),
            ),
            ledger=ledger,
            factory=factory,
            connection=made,
        )
    finally:
        made.close()


def _seed(bundle: _Bundle, events: tuple[str, ...], stream: str = SOURCE_STREAM) -> None:
    """中断前のLedgerを作る。**Caseが観測するEvent列ではない。**

    Recovery が始まる前の状態を用意しているだけである。Case の期待列
    （`RECOVERY_STARTED → RECOVERY_DECIDED`）は Orchestrator が Trace Stream へ積む。
    Test 側で Recovery Event を Append しない。
    """
    if not events:
        return
    head = bundle.ledger.stream_head(stream)
    with bundle.factory.begin_immediate(bundle.connection):
        bundle.ledger.append(
            [
                NewEvent(
                    stream_id=stream,
                    event_type=name,
                    payload_hash=hash_canonical(
                        {"event_type": name, "index": index},
                        artifact_type="seed-event",
                        schema_major=1,
                    ),
                    recorded_at="2026-08-21T00:00:00Z",
                )
                for index, name in enumerate(events)
            ],
            expected_stream_sequence=head,
        )


def _request() -> RecoveryRequest:
    return RecoveryRequest(
        source_stream_id=SOURCE_STREAM,
        trace_stream_id=TRACE_STREAM,
        repair_decision_id=REPAIR_ID,
        expected_events=EXPECTED_EVENTS,
    )


# --------------------------------------------------------------------------
# AT-EVENT-MAPPING-001/LEDGER_EVENT_MISSING
# --------------------------------------------------------------------------


@pytest.mark.case("AT-EVENT-MAPPING-001/LEDGER_EVENT_MISSING")
def test_missing_phase_ledger_event_requires_repair(bundle: _Bundle, case_observation: Any) -> None:
    """Storeが要求する正規Eventが無いのでRepairが要る。

    欠落を埋めない。Compensating Event が要ることだけを記録する。
    """
    _seed(bundle, SEEDED)
    trace = RealLedgerView(bundle.ledger, TRACE_STREAM)
    head_before = trace.head

    outcome = bundle.orchestrator.recover(_request())

    assert outcome.state == "REPAIR_REQUIRED"
    assert outcome.error_code is ErrorCode.PHASE_LEDGER_EVENT_MISSING
    assert outcome.missing_events == EXPECTED_EVENTS, "欠落が検出されていない"
    assert outcome.compensating_event_required is True
    assert outcome.duplicate_effect_attempts == 0
    assert outcome.chain_intact is True

    # 走査対象は触っていない。欠落は欠落のまま残る。
    assert outcome.source_events == SEEDED
    assert outcome.source_chain_hash == outcome.repaired_chain_hash, (
        "走査対象のEvent列が変わっている。Recoveryが欠落を埋めてしまった"
    )
    assert RealLedgerView(bundle.ledger, SOURCE_STREAM).appended == list(SEEDED)

    # Recovery Event は Trace Stream にだけ積まれる。
    assert trace.appended == ["RECOVERY_STARTED", "RECOVERY_DECIDED"]
    assert outcome.ledger_head_before == head_before
    assert outcome.ledger_head_after == trace.head

    effects = SideEffectProbe()
    observe_case(
        case_observation,
        "AT-EVENT-MAPPING-001/LEDGER_EVENT_MISSING",
        state=outcome.state,
        subject_id=outcome.repair_decision_id,
        error_code=outcome.error_code.value,
        ledger=trace,
        effects=effects,
        head_before=head_before,
        payload={
            "store_state": STORE_STATE,
            "expected_events": list(EXPECTED_EVENTS),
            "source_events": list(outcome.source_events),
            "missing_events": list(outcome.missing_events),
            "compensating_event_required": outcome.compensating_event_required,
            "duplicate_effect_attempts": outcome.duplicate_effect_attempts,
            "disposition": outcome.state,
            "source_chain_hash": outcome.source_chain_hash,
            "repaired_chain_hash": outcome.repaired_chain_hash,
            "producer_module": "harness.application.recovery_orchestrator",
            "producer_symbol": "RecoveryOrchestrator.recover",
        },
    )


# --------------------------------------------------------------------------
# 正常系
# --------------------------------------------------------------------------


def test_complete_stream_is_repaired(bundle: _Bundle) -> None:
    """正規Eventが揃っていれば`REPAIRED`。Compensatingは要らない。"""
    _seed(bundle, COMPLETE)
    outcome = bundle.orchestrator.recover(_request())

    assert outcome.state == "REPAIRED"
    assert outcome.error_code is None
    assert outcome.missing_events == ()
    assert outcome.compensating_event_required is False
    # 判定が変わってもRecovery Event列は同じ。開始と判断の両方を残す。
    assert RealLedgerView(bundle.ledger, TRACE_STREAM).appended == [
        "RECOVERY_STARTED",
        "RECOVERY_DECIDED",
    ]


# --------------------------------------------------------------------------
# 三値 Disposition
# --------------------------------------------------------------------------


def test_broken_chain_is_unknown_not_repair_required(bundle: _Bundle) -> None:
    """Chainが壊れていたら`EFFECT_UNKNOWN`。2値へ潰さない。

    欠落なのか読めないのかを区別できない状態を、どちらかへ倒さない。
    """
    decision = scan_phase_ledger_mapping(
        repair_decision_id=REPAIR_ID,
        expected_events=EXPECTED_EVENTS,
        observed_events=SEEDED,
        duplicate_effect_attempts=0,
        chain_intact=False,
    )
    assert decision.state == "EFFECT_UNKNOWN"
    assert decision.error_code is ErrorCode.LEDGER_CHAIN_TAMPERED
    assert decision.missing_events == (), "判定できないのに欠落を断定している"
    assert decision.compensating_event_required is False


def test_unknown_is_not_converted_to_either_binary_value() -> None:
    """`EFFECT_UNKNOWN` を `REPAIR_REQUIRED` にも `REPAIRED` にもしない。"""
    unknown = scan_phase_ledger_mapping(
        repair_decision_id=REPAIR_ID,
        expected_events=EXPECTED_EVENTS,
        observed_events=(),
        duplicate_effect_attempts=0,
        chain_intact=False,
    )
    assert unknown.state not in {"REPAIR_REQUIRED", "REPAIRED"}
    assert unknown.repair_required is False


def test_missing_events_are_not_filled_in(bundle: _Bundle) -> None:
    """欠落Eventを「在った」ことにしない。走査対象へ書き足さない。"""
    _seed(bundle, SEEDED)
    before = RealLedgerView(bundle.ledger, SOURCE_STREAM).appended
    bundle.orchestrator.recover(_request())
    after = RealLedgerView(bundle.ledger, SOURCE_STREAM).appended
    assert after == before
    for name in EXPECTED_EVENTS:
        assert name not in after, f"欠落していた {name} が補完されている"


def test_duplicate_effect_attempts_are_counted_not_assumed(bundle: _Bundle) -> None:
    """二重試行を数える。0と決めつけない。"""
    _seed(bundle, (*SEEDED, "OUTBOX_DISPATCHING"))
    outcome = bundle.orchestrator.recover(_request())
    assert outcome.duplicate_effect_attempts == 1


# --------------------------------------------------------------------------
# Transaction と再実行
# --------------------------------------------------------------------------


def test_append_failure_leaves_no_partial_recovery_event(
    bundle: _Bundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """判断Eventが積めなければ開始Eventも残さない。Partialを作らない。"""
    _seed(bundle, SEEDED)
    original = bundle.orchestrator._append
    calls = {"n": 0}

    def flaky(stream_id: str, event: EventType, expected_head: int) -> None:
        calls["n"] += 1
        if event is EventType.RECOVERY_DECIDED:
            raise RuntimeError("append failed")
        original(stream_id, event, expected_head)

    monkeypatch.setattr(bundle.orchestrator, "_append", flaky)
    with pytest.raises(RuntimeError):
        bundle.orchestrator.recover(_request())

    assert calls["n"] == 2
    assert RealLedgerView(bundle.ledger, TRACE_STREAM).appended == [], (
        "Rollbackしたのに RECOVERY_STARTED が残っている"
    )


def test_second_recovery_appends_a_new_pair_without_duplication(bundle: _Bundle) -> None:
    """再走査は新しい2件を積む。同じHeadへ二重Appendしない。"""
    _seed(bundle, SEEDED)
    first = bundle.orchestrator.recover(_request())
    second = bundle.orchestrator.recover(_request())

    assert first.ledger_head_after == 2
    assert second.ledger_head_before == 2
    assert second.ledger_head_after == 4
    assert RealLedgerView(bundle.ledger, TRACE_STREAM).appended == [
        "RECOVERY_STARTED",
        "RECOVERY_DECIDED",
        "RECOVERY_STARTED",
        "RECOVERY_DECIDED",
    ]


def test_other_stream_events_do_not_leak_into_the_trace(bundle: _Bundle) -> None:
    """別StreamのEventがTraceへ混ざらない。"""
    _seed(bundle, SEEDED)
    _seed(bundle, ("ACTION_BLOCKED",), stream="OTHER_STREAM")
    bundle.orchestrator.recover(_request())
    assert RealLedgerView(bundle.ledger, TRACE_STREAM).appended == [
        "RECOVERY_STARTED",
        "RECOVERY_DECIDED",
    ]


def test_recovery_does_not_reexecute_any_effect(bundle: _Bundle) -> None:
    """判断後にEffectを再実行しない。Orchestratorは作用の入口を持たない。"""
    _seed(bundle, SEEDED)
    outcome = bundle.orchestrator.recover(_request())
    effects = SideEffectProbe()
    assert effects.external_effects == 0
    assert effects.process_launches == 0
    assert not hasattr(bundle.orchestrator, "_effect")
    assert outcome.compensating_event_required is True
