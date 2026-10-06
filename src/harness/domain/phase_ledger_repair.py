"""§1.14.1 Phase Store State と Ledger Event の写像を突き合わせる。

## 何を判定するか

Phase固有Storeが `X` という State に居るなら、正本Ledgerには §1.14.1 が定める
**正規Ledger Event** が残っているはずである。残っていなければ、Storeと
Ledgerが食い違っている。

本Moduleは**その食い違いを見つけるだけ**である。Ledgerへ書かない。
`os` も `sqlite3` も import しない。

## 欠落Eventを埋めない

見つけた欠落を「実は在った」ことにしない。過去のEventを後から差し込むと、
そのEventが起きた時刻も順序も嘘になる。**Ledgerは追記だけで訂正する**
（不変条件#1）ので、必要なのは Compensating Event であって、欠落の穴埋めでは
ない。だから `compensating_event_required` を立てて人の判断へ回す。

## 判定が3値である理由

`REPAIR_REQUIRED` と `REPAIRED` の2値にすると、判定できない状態を
どちらかへ倒すことになる。倒した先が `REPAIRED` なら壊れたまま先へ進み、
`REPAIR_REQUIRED` なら健全なStreamを永久に止める。`EFFECT_UNKNOWN` を
3つ目として残す。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from harness.domain._registry_generated import ErrorCode

__all__ = ["RepairDecision", "scan_phase_ledger_mapping"]

_REPAIR_REQUIRED = "REPAIR_REQUIRED"
_REPAIRED = "REPAIRED"
_EFFECT_UNKNOWN = "EFFECT_UNKNOWN"


@dataclass(frozen=True, slots=True)
class RepairDecision:
    """`REPAIR_DECISION` 名前空間の判定。**Ledgerへの書込みは含まない。**"""

    repair_decision_id: str
    state: str
    error_code: ErrorCode | None
    #: Storeの State に対して §1.14.1 が要求する正規Ledger Event。
    expected_events: tuple[str, ...]
    #: 実際にLedgerで観測できたEvent。
    observed_events: tuple[str, ...]
    #: 期待されていたのに観測できなかったEvent。**埋めない。**
    missing_events: tuple[str, ...]
    #: 訂正はAppendだけで行う（不変条件#1）。穴埋めではない。
    compensating_event_required: bool
    #: 同じ作用を二重に試していないか。実測値である。
    duplicate_effect_attempts: int

    @property
    def repair_required(self) -> bool:
        return self.state == _REPAIR_REQUIRED


def scan_phase_ledger_mapping(
    *,
    repair_decision_id: str,
    expected_events: Sequence[str],
    observed_events: Sequence[str],
    duplicate_effect_attempts: int,
    chain_intact: bool,
) -> RepairDecision:
    """Storeの State が要求するEventがLedgerに在るかを調べる。

    `expected_events` は §1.14.1 の写像表から**呼出側が引く**。表そのものを
    本Moduleへ写さない。写すと、設計書とCodeの2箇所に同じ表が生まれる。

    Chainが壊れている場合は欠落かどうかを判定できない。`EFFECT_UNKNOWN` で
    止める。**2値へ潰さない。**
    """
    if duplicate_effect_attempts < 0:
        raise ValueError("duplicate_effect_attempts must not be negative")

    observed = tuple(observed_events)
    expected = tuple(expected_events)

    if not chain_intact:
        # 連鎖が壊れていると、そのEventが「無い」のか「読めない」のか分からない。
        return RepairDecision(
            repair_decision_id=repair_decision_id,
            state=_EFFECT_UNKNOWN,
            error_code=ErrorCode.LEDGER_CHAIN_TAMPERED,
            expected_events=expected,
            observed_events=observed,
            missing_events=(),
            compensating_event_required=False,
            duplicate_effect_attempts=duplicate_effect_attempts,
        )

    seen = set(observed)
    missing = tuple(name for name in expected if name not in seen)
    if missing:
        return RepairDecision(
            repair_decision_id=repair_decision_id,
            state=_REPAIR_REQUIRED,
            error_code=ErrorCode.PHASE_LEDGER_EVENT_MISSING,
            expected_events=expected,
            observed_events=observed,
            missing_events=missing,
            # 欠落を差し込まない。Compensating EventをAppendして訂正する。
            compensating_event_required=True,
            duplicate_effect_attempts=duplicate_effect_attempts,
        )

    return RepairDecision(
        repair_decision_id=repair_decision_id,
        state=_REPAIRED,
        error_code=None,
        expected_events=expected,
        observed_events=observed,
        missing_events=(),
        compensating_event_required=False,
        duplicate_effect_attempts=duplicate_effect_attempts,
    )
