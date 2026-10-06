"""§1.4.3 Run の終端遷移。ReceiptなしのRelease と 未照合Effectの終端を拒否する。

## 何を防ぐ規則か

Runを終端させることは「もう見なくてよい」と宣言することである。
確かめていないものが残っているうちに終端させると、その宣言が嘘になる。

| 終端要求 | 拒否条件 | 理由 |
|---|---|---|
| `COMPLETED` | Release Decisionが無い | 誰も「出してよい」と決めていない |
| `CANCELLED` | 未照合Effectがある | 外部で起きたかもしれない作用を放置して閉じる |

### `COMPLETED` に Release Decision を要求する

評価が終わったこと（`EVALUATION_COMPLETED`）と、出してよいと決めたことは別である。
Release Decision が無いまま `COMPLETED` にすると、承認の無い結果が
「完了した」という形で下流へ流れる。`BLOCKED` で止め、
`completed_event_count == 0` を保つ。

### 未照合Effectがあるまま `CANCELLED` にしない

`EFFECT_UNKNOWN` は「起きたかどうか判定できない」状態である。
判定できないものをCancelledにすると、**起きなかったことにされる**。
`BLOCKED_REPAIR_REQUIRED` へ落とし、`ended_at` を `null` のまま保つ。
終わっていないものに終了時刻を書かない。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from harness.domain._registry_generated import ErrorCode, EventType

__all__ = [
    "RunTerminalRequest",
    "RunTerminalVerdict",
    "evaluate_run_terminal",
]

_BLOCKED: Final[str] = "BLOCKED"
_BLOCKED_REPAIR_REQUIRED: Final[str] = "BLOCKED_REPAIR_REQUIRED"

# Release Decision を要さずに終端できる状態はない。
_TERMINAL_REQUIRING_RELEASE: Final[frozenset[str]] = frozenset({"COMPLETED"})
_TERMINAL_REQUIRING_RECONCILIATION: Final[frozenset[str]] = frozenset(
    {"CANCELLED", "COMPLETED", "FAILED"}
)


@dataclass(frozen=True, slots=True)
class RunTerminalRequest:
    """Runを終端させる要求。"""

    run_id: str
    requested_terminal_state: str
    release_decision_id: str | None
    #: 判定できていないEffectの数。0でなければ終端させない。
    unreconciled_effect_count: int


@dataclass(frozen=True, slots=True)
class RunTerminalVerdict:
    """終端判定の結果。"""

    run_id: str
    state: str
    error_code: ErrorCode | None
    events: tuple[EventType, ...]
    #: 終端できたときだけ値が入る。止めたときは `None` のままである。
    ended_at: str | None
    requested_terminal_state: str
    #: 終端できたときだけ 1。止めたら 0。
    completed_event_count: int

    @property
    def blocked(self) -> bool:
        return self.error_code is not None


def evaluate_run_terminal(
    request: RunTerminalRequest, *, now: str | None = None
) -> RunTerminalVerdict:
    """終端要求を判定する。

    未照合Effectの検査を先に行う。Release Decision があっても、
    判定できないEffectが残っているなら終端させない。
    **確かめていないものを、承認で上書きしない。**
    """
    if request.unreconciled_effect_count < 0:
        raise ValueError("unreconciled_effect_count must not be negative")

    if (
        request.requested_terminal_state in _TERMINAL_REQUIRING_RECONCILIATION
        and request.unreconciled_effect_count > 0
    ):
        return RunTerminalVerdict(
            run_id=request.run_id,
            state=_BLOCKED_REPAIR_REQUIRED,
            error_code=ErrorCode.UNRECONCILED_EFFECT_PRESENT,
            events=(EventType.EFFECT_UNKNOWN, EventType.ACTION_BLOCKED),
            # 終わっていないものに終了時刻を書かない。
            ended_at=None,
            requested_terminal_state=request.requested_terminal_state,
            completed_event_count=0,
        )

    if (
        request.requested_terminal_state in _TERMINAL_REQUIRING_RELEASE
        and not request.release_decision_id
    ):
        return RunTerminalVerdict(
            run_id=request.run_id,
            state=_BLOCKED,
            error_code=ErrorCode.RELEASE_DECISION_REQUIRED,
            events=(EventType.EVALUATION_COMPLETED, EventType.ACTION_BLOCKED),
            ended_at=None,
            requested_terminal_state=request.requested_terminal_state,
            completed_event_count=0,
        )

    return RunTerminalVerdict(
        run_id=request.run_id,
        state=request.requested_terminal_state,
        error_code=None,
        events=(),
        ended_at=now,
        requested_terminal_state=request.requested_terminal_state,
        completed_event_count=1,
    )
