"""Recovery Orchestrator。Recovery判断とLedger記録を分ける。

## 責務分離

`scan_phase_ledger_mapping`（Domain）は Store State と Ledger Event の
食い違いを**見つけるだけ**である。Ledgerへの読み書きもTransactionも持たない。

Recovery Orchestrator（Application）が Chain検証・Ledger読取・
`RECOVERY_STARTED`／`RECOVERY_DECIDED` のAppend・Transaction所有を担う。
判定そのものは行わない。

Recovery Service へ Ledger Port を渡さない。判断する層が記録も持つと、
記録の都合で判断が変わる余地ができる。

## 元の観測と Recovery 判断を混ぜない

`observed_events` は Recovery を始める**前**のLedgerの中身である。
`appended_events` は Recovery が足した2件である。両方を別々に持つ。

混ぜると「欠落していたEventが復元された」ように見える記録ができる。
**欠落は欠落のまま残す。** 訂正はCompensating EventのAppendで行う
（不変条件#1）。

## 判断後にEffectを再実行しない

`UNKNOWN` を含めどの判定でも、本Moduleは作用を起こさない。Queue・Retry・
Fallbackへ渡す経路も持たない。再実行はOperatorの判断を経た別の入口である。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from harness.domain.errors import ErrorCode
from harness.domain.events import EventType
from harness.domain.hashing import ContentHash, hash_canonical
from harness.domain.phase_ledger_repair import RepairDecision, scan_phase_ledger_mapping
from harness.ports.effect_execution import ClockPort
from harness.ports.event_ledger import EventLedgerPort, NewEvent
from harness.ports.unit_of_work import UnitOfWorkPort

__all__ = ["RecoveryOrchestrator", "RecoveryOutcome", "RecoveryRequest"]

_EVENT_ARTIFACT_TYPE = "recovery-event"
_EVENT_SCHEMA_MAJOR = 1
_EFFECT_ATTEMPT_EVENTS = frozenset(
    {
        EventType.EXECUTION_ATTEMPTED.value,
        EventType.ACTION_PREPARED.value,
        EventType.OUTBOX_DISPATCHING.value,
        EventType.REMOTE_REQUEST_DISPATCHING.value,
    }
)


@dataclass(frozen=True, slots=True)
class RecoveryRequest:
    """1 Stream に対する Recovery 走査要求。

    **走査する Stream と記録する Stream を分ける。** 診断している Stream へ
    Recovery Event を積むと、次に同じ Stream を走査したときに見えるものが
    変わる。欠落の証拠を、調べる行為自体が汚してしまう。
    """

    #: 走査対象。Phase Store に対応する Ledger Stream。**書き込まない。**
    source_stream_id: str
    #: Recovery Event を残す Stream（Case の `trace_scope`）。
    trace_stream_id: str
    repair_decision_id: str
    #: §1.14.1 の写像表から呼出側が引いた正規Ledger Event。表を写さない。
    expected_events: Sequence[str]


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """`REPAIR_DECISION` 名前空間のSubject。"""

    repair_decision_id: str
    state: str
    error_code: ErrorCode | None
    #: Recovery が Append した列。Case の期待値と突き合わせる。
    events: tuple[EventType, ...]
    #: Recovery を始める**前**のLedgerの中身。判断と混ぜない。
    source_events: tuple[str, ...]
    missing_events: tuple[str, ...]
    compensating_event_required: bool
    duplicate_effect_attempts: int
    ledger_head_before: int
    ledger_head_after: int
    #: 走査前のChain Hash。
    source_chain_hash: str
    #: Recovery後に走査対象を読み直したChain Hash。
    #: **`source_chain_hash` と等しいはずである。** 欠落は埋めない。
    repaired_chain_hash: str
    chain_intact: bool
    decision: RepairDecision


class RecoveryOrchestrator:
    """Chainを読み、判断を仰ぎ、Recovery Event列を残す。"""

    def __init__(
        self,
        *,
        ledger: EventLedgerPort,
        unit_of_work: UnitOfWorkPort,
        clock: ClockPort,
    ) -> None:
        self._ledger = ledger
        self._unit_of_work = unit_of_work
        self._clock = clock

    def recover(self, request: RecoveryRequest) -> RecoveryOutcome:
        # --- 1〜3. Head・Chain・欠落を読む。まだ何も書かない ----------------
        head_before = self._ledger.stream_head(request.trace_stream_id)
        chain = self._ledger.verify_chain(request.source_stream_id)
        entries = self._ledger.load_stream(request.source_stream_id)
        source_events = tuple(entry.event_type for entry in entries)
        source_chain_hash = self._chain_hash(source_events)
        duplicates = self._duplicate_effect_attempts(source_events)

        # --- 4. 判断を仰ぐ。Domainは Ledger を知らない ----------------------
        decision = scan_phase_ledger_mapping(
            repair_decision_id=request.repair_decision_id,
            expected_events=request.expected_events,
            observed_events=source_events,
            duplicate_effect_attempts=duplicates,
            chain_intact=chain.valid,
        )

        # --- 5〜8. Recovery Event を同一Transactionで残す --------------------
        # 開始と判断の順序を固定する。判断だけを残して開始が無い記録を作らない。
        with self._unit_of_work.begin_immediate():
            self._append(request.trace_stream_id, EventType.RECOVERY_STARTED, head_before)
            self._append(request.trace_stream_id, EventType.RECOVERY_DECIDED, head_before + 1)

        # --- 9〜10. 読み戻す。申告値を観測値にしない ------------------------
        head_after = self._ledger.stream_head(request.trace_stream_id)
        # 走査対象を読み直す。Recovery が欠落を埋めていないことを確かめる。
        after_events = tuple(
            entry.event_type for entry in self._ledger.load_stream(request.source_stream_id)
        )
        return RecoveryOutcome(
            repair_decision_id=decision.repair_decision_id,
            state=decision.state,
            error_code=decision.error_code,
            events=(EventType.RECOVERY_STARTED, EventType.RECOVERY_DECIDED),
            source_events=source_events,
            missing_events=decision.missing_events,
            compensating_event_required=decision.compensating_event_required,
            duplicate_effect_attempts=decision.duplicate_effect_attempts,
            ledger_head_before=head_before,
            ledger_head_after=head_after,
            source_chain_hash=source_chain_hash,
            repaired_chain_hash=self._chain_hash(after_events),
            chain_intact=chain.valid,
            decision=decision,
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _duplicate_effect_attempts(events: Sequence[str]) -> int:
        """同じ作用を二重に試した回数。**数える。0と書かない。**"""
        attempts = [name for name in events if name in _EFFECT_ATTEMPT_EVENTS]
        return max(0, len(attempts) - len(set(attempts)))

    @staticmethod
    def _chain_hash(events: Sequence[str]) -> str:
        """観測したEvent列のHash。Chainの同一性を後から比較するために持つ。"""
        return str(
            hash_canonical(
                {"events": list(events)},
                artifact_type=_EVENT_ARTIFACT_TYPE,
                schema_major=_EVENT_SCHEMA_MAJOR,
            )
        )

    def _append(self, stream_id: str, event: EventType, expected_head: int) -> None:
        self._ledger.append(
            [
                NewEvent(
                    stream_id=stream_id,
                    event_type=event.value,
                    payload_hash=self._payload_hash(event),
                    recorded_at=self._clock.now(),
                )
            ],
            expected_stream_sequence=expected_head,
        )

    @staticmethod
    def _payload_hash(event: EventType) -> ContentHash:
        # 本文はLedgerへ置かない。Event種別だけをHashにする（§15.7）。
        return hash_canonical(
            {"event_type": event.value},
            artifact_type=_EVENT_ARTIFACT_TYPE,
            schema_major=_EVENT_SCHEMA_MAJOR,
        )
