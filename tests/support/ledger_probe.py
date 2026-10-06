"""Case観測用の Ledger／副作用 Probe。全Batchで同じ観測をする。

## なぜ共有するか

Batch ごとに Spy を書くと、Batch ごとに観測の意味が微妙にずれる。
`observed_event_sequence` は「実際に正本LedgerへAppendされたEvent列」という
**1つの意味**でなければならない（設計書§19.1.1）。観測器も1つにする。

## Head と Append 列を同時に持つ

列だけを見ると、拒否されたはずのAppendが実は成功していた場合を見逃す。
`head` と `appended` を常に一緒に持ち、両者の食い違いを Probe 自身が拒否する。

* Head が動いたのに列が伸びていない → 記録漏れ
* 列が伸びたのに Head が動いていない → Append を数え損ねている

どちらも「観測できている」と言えない状態なので、その場で例外にする。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["LedgerProbe", "SideEffectProbe", "record_case"]


@dataclass(frozen=True, slots=True)
class _Event:
    """`event_type` だけを持つ最小Event。Probeが数えるために使う。"""

    event_type: str


class LedgerProbe:
    """正本Ledgerの代替。Appendされた列とHeadを同時に持つ。

    Case ごとに新しく作る。**別Caseのインスタンスを共有しない。**
    共有すると他CaseのEventが混ざり、どのCaseの観測か決まらなくなる。
    """

    def __init__(self, stream_id: str = "ACTION_ATTEMPT_STREAM") -> None:
        self.stream_id = stream_id
        self._appended: list[str] = []
        self._head = 0

    # -- 観測値 -----------------------------------------------------------

    @property
    def appended(self) -> list[str]:
        self._check_consistent()
        return list(self._appended)

    @property
    def head(self) -> int:
        self._check_consistent()
        return self._head

    @property
    def effect_attempts(self) -> int:
        started = {"ACTION_PREPARED", "ACTION_EXECUTING", "EXECUTION_ATTEMPTED", "EFFECT_ATTEMPTED"}
        return sum(1 for name in self._appended if name in started)

    # -- 書込み -----------------------------------------------------------

    def append(self, events: Any, *, expected_stream_sequence: int | None = None) -> int:
        """Event列をAppendしてHeadを進める。CAS不一致は拒否する。"""
        listed = [getattr(e, "event_type", e) for e in events]
        if not listed:
            raise ValueError("空のAppend要求。Eventの無いAppendを黙って受理しない")
        if expected_stream_sequence is not None and expected_stream_sequence != self._head:
            raise AssertionError(
                f"Stream headとCASが噛み合っていない: "
                f"expected={expected_stream_sequence} head={self._head}"
            )
        self._appended.extend(str(name) for name in listed)
        self._head += len(listed)
        return self._head

    def append_names(self, names: list[str] | tuple[str, ...]) -> None:
        """Domainが決めた列を、Application層と同じようにLedgerへ積む。"""
        if names:
            self.append([_Event(str(n)) for n in names], expected_stream_sequence=self._head)

    def reject(self) -> None:
        """拒否されたAppend。Ledgerへ何も残さない。Headも動かない。"""
        return

    # -- 整合 -------------------------------------------------------------

    def _check_consistent(self) -> None:
        if self._head != len(self._appended):
            raise AssertionError(
                f"LedgerProbeが不整合である: head={self._head} "
                f"appended={len(self._appended)} 件。観測できていない"
            )


@dataclass
class SideEffectProbe:
    """副作用の実測カウンタ。**0で初期化するのではなく、0を数える。**

    存在すること自体が「測った」証拠である。Probeを組まずに0を書くのは
    未測定を0件と偽ることになる。
    """

    network_calls: int = 0
    network_bytes_sent: int = 0
    process_launches: int = 0
    workspace_commits: int = 0
    budget_reservations_created: int = 0
    provider_calls: int = 0
    _events: list[str] = field(default_factory=list, repr=False)

    @property
    def external_effects(self) -> int:
        return (
            self.network_calls
            + self.process_launches
            + self.workspace_commits
            + self.budget_reservations_created
            + self.provider_calls
        )

    def send(self, endpoint: str, payload: bytes) -> int:
        self.network_calls += 1
        self.network_bytes_sent += len(payload)
        return len(payload)

    def launch(self, argv: list[str]) -> int:
        self.process_launches += 1
        return 0

    def commit(self, relative_path: str, payload: bytes) -> str:
        self.workspace_commits += 1
        return "sha256:" + "0" * 64

    def reserve_budget(self, tokens: int) -> str:
        self.budget_reservations_created += 1
        return "reservation-1"

    def invoke_provider(self, reservation_id: str, prompt: str) -> str:
        self.provider_calls += 1
        return "response"


def record_case(
    observation: Any,
    *,
    state: str,
    subject_id: str,
    ledger: LedgerProbe,
    head_before: int,
    effects: SideEffectProbe,
    error_code: str | None = None,
    ledger_effect_attempts: int | None = None,
) -> None:
    """観測値を1箇所で記録する。Event列は必ずLedgerから読む。

    `ledger_effect_attempts` は、Ledgerへ直接Appendしない層（Delegation の
    純Domain判定など）が**実測したEffect試行数**を渡すための受け口である。
    Effect試行数はEvent列ではなく副作用の計測値であり、Registryも
    `assertions: effect_attempts == N` としてEvent列とは別に持つ。
    **Event列へ架空のEventを足して表現しない。**

    ここを通さない記録経路を作らない。`observation.record()` はEvent列を
    受け取らないので、`observe_ledger()` を呼ばなければ Adapter が
    `LEDGER_NOT_OBSERVED` で Evidence 生成を拒否する。
    """
    observation.record(state=state, subject_id=subject_id, error_code=error_code)
    observation.observe_ledger(ledger, head_before=head_before)
    observation.observe_side_effects(
        network_calls=effects.network_calls,
        process_launches=effects.process_launches,
        workspace_commits=effects.workspace_commits,
        external_effects=effects.external_effects,
        ledger_effect_attempts=(
            ledger.effect_attempts
            if ledger_effect_attempts is None
            else int(ledger_effect_attempts)
        ),
    )

    # Head と 列 の食い違いをCase側でも固定する（設計書§19.1.1）。
    grew = observation.ledger_head_after > observation.ledger_head_before
    has_new = len(observation.observed_events) > observation.ledger_head_before
    if grew != has_new:
        raise AssertionError(
            f"Headと Event列が食い違う: "
            f"before={observation.ledger_head_before} "
            f"after={observation.ledger_head_after} "
            f"events={len(observation.observed_events)}"
        )
