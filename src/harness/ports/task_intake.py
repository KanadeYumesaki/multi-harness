"""Task 取り込み経路の Port。

## なぜ Port へ出すか

`TaskPlanService` は「Task を読む」「Context を組む」の2つを順に呼ぶ。
どちらも既に Application 層の Service として在る。しかし CLAUDE.md §2 は
**application 層が参照してよいのは `ports/` と `domain/` だけ**と定める
（`tests/spec_lint/test_layer_dependencies.py` が機械検査している）。

Application 同士を直接 import すると、その規則が骨抜きになる。だから
協力者の契約をここへ置き、具象 Service は構造的にこれを満たす。
`ports/event_ledger.py` が `NewEvent`／`AppendResult` を、
`ports/safe_input_reader.py` が `ReadEvidence` を持つのと同じ扱いである。

**規則を緩めていない。** 依存の向きを正しくしただけである。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from harness.domain.context_budget import (
    ContextAssembly,
    TokenBudgetPolicy,
    TokenProfileSnapshot,
)
from harness.domain.errors import ErrorCode
from harness.domain.events import EventType
from harness.domain.hashing import ContentHash
from harness.domain.input_read import ReadDecision, ReadDenial
from harness.ports.masking import MaskingPolicyDenial
from harness.ports.safe_input_reader import ReadEvidence

__all__ = [
    "ContextAssemblyPort",
    "InputReadOutcome",
    "InputReadPort",
    "InputReadRequest",
]


@dataclass(frozen=True, slots=True)
class InputReadRequest:
    """1回の読取要求。"""

    stream_id: str
    read_decision_id: str
    capability_id: str
    relative_path: str


@dataclass(frozen=True, slots=True)
class InputReadOutcome:
    """`INPUT_READ_DECISION`名前空間のSubject。

    Readerの`ReadDenial`をそのままSubjectにしない。Readerの戻り値は判定であり、
    Orchestrationの結果ではない。Ledger Headのように、Readerが知らない事実を
    含む必要がある。
    """

    read_decision_id: str
    state: str
    error_code: ErrorCode | None
    events: tuple[EventType, ...]
    ledger_head_before: int
    ledger_head_after: int
    capability_path_hash: ContentHash
    classification: str | None
    payload: bytes | None
    read_evidence: ReadEvidence | None
    #: Masking Policy が拒否した場合だけ入る。Reader の拒否とは別物である。
    masking_denial: MaskingPolicyDenial | None = None

    @property
    def denied(self) -> bool:
        return self.state == ReadDecision.DENIED.value


class InputReadPort(Protocol):
    """読取判定と Ledger 記録を同一 Transaction で束ねる側。"""

    def read(self, request: InputReadRequest) -> InputReadOutcome: ...


@dataclass(frozen=True, slots=True)
class VerifiedContextInput:
    text: str
    evidence: ReadEvidence
    mandatory: bool = False
    compressible: bool = False
    compression_depth: int = 0


class ContextAssemblyPort(Protocol):
    """検証済み入力 1 件から Context Snapshot を組む側。

    Fragment の Role を決めるのは実装側の責務である（§3.6 手順2）。
    呼出側が Role を指定できる形にしない。**指定できれば、入力由来の本文へ
    Control Role を与える経路ができる。**
    """

    def build_verified_input_bundle(
        self,
        *,
        text: str,
        evidence: ReadEvidence,
        policy: TokenBudgetPolicy,
        profile: TokenProfileSnapshot,
        bundle_id: str,
        receipt_id: str,
        now: str,
        candidates: tuple[VerifiedContextInput, ...] = (),
        rejected_input_resources: tuple[dict[str, str], ...] = (),
    ) -> ContextAssembly: ...


class InputEnumerationPort(Protocol):
    def enumerate(self, request: InputReadRequest) -> list[str] | ReadDenial: ...
