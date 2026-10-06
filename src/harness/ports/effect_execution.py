"""作用を実際に起こすPortの契約と、Clock Port。

## なぜ作用ごとにPortを分けるか

`AT-POLICY-STALE-001` は作用の種類ごとに別のError Codeで止まることを要求する。
1つのPortにまとめると「どの作用を止めたか」がPort境界から消え、
**止めたことを観測する側が種類を区別できなくなる**。

Portを分けておくと、統合試験は「そのPortが1度も呼ばれていない」を
直接数えられる。`network_calls == 0` のような Case assertion は、
Portの呼出回数として観測できる形になっていて初めて測れる。

## Clock を Port にする理由

Policy の鮮度判定は現在時刻に依存する。時刻を直接参照すると、
期限境界（`expires_at` ちょうど）の試験が書けない。
CLAUDE.md「時刻、UUID、乱数、Fault InjectionはPort経由で注入する」に従う。
"""

from __future__ import annotations

from typing import Protocol

__all__ = [
    "ClockPort",
    "ExternalSendPort",
    "PaidExecutionPort",
    "ProcessLaunchPort",
    "WorkspaceWritePort",
]


class ClockPort(Protocol):
    """現在時刻。Domainは直接参照せず、これを通して受け取る。"""

    def now(self) -> str:
        """RFC 3339 UTC（`...Z`）の現在時刻を返す。"""
        ...


class ExternalSendPort(Protocol):
    """外部への送信。Network I/Oを起こす。"""

    def send(self, endpoint: str, payload: bytes) -> int:
        """送信したByte数を返す。"""
        ...


class ProcessLaunchPort(Protocol):
    """Process起動。`list[str]` だけを受け取る（不変条件#8）。"""

    def launch(self, argv: list[str]) -> int:
        """終了Codeを返す。"""
        ...


class WorkspaceWritePort(Protocol):
    """Workspaceへの書込み確定。"""

    def commit(self, relative_path: str, payload: bytes) -> str:
        """確定したArtifactのHashを返す。"""
        ...


class PaidExecutionPort(Protocol):
    """課金を伴う実行。Budget予約とProvider呼出を含む。"""

    def reserve_budget(self, tokens: int) -> str:
        """予約IDを返す。"""
        ...

    def invoke_provider(self, reservation_id: str, prompt: str) -> str:
        """Provider応答を返す。"""
        ...
