"""CLI Workbench Preview が外側へ出す境界。

Application はここだけを知る。`subprocess`、`os`、`sqlite3` は infrastructure が持つ。

## Runner に「安全だ」と言わせない

`CliRunResult` は **観測できたものだけ** を持つ。stderr の本文は返さない。返した
瞬間に、未検査の外部 Bytes が Log と例外へ入る経路ができる。長さと分類だけを返す。

## 起動体の同一性は Runner が定義する

`CliLaunchSpec.runtime_hash` は entrypoint 1 File の Hash ではなく、実際に起動する
実行体・argv・env allowlist・cwd 種別・制限をまとめた Hash である。**Plan はこの
Hash へ束縛し、起動直前に `verify` で再計算して照合する。**
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from harness.domain.cli_invocation import CliInvocationJournal
from harness.domain.hashing import ContentHash
from harness.domain.workbench import WorkbenchPreference

__all__ = [
    "BoundaryProbePort",
    "BoundaryVerdict",
    "CliLaunchSpec",
    "CliOutcome",
    "CliProfilePort",
    "CliProviderStatus",
    "CliRunResult",
    "CliRunnerPort",
    "Containment",
    "DescendantCleanup",
    "IdSourcePort",
    "InvocationJournalStorePort",
    "WorkbenchRecord",
    "WorkbenchStorePort",
]


class DescendantCleanup(Enum):
    """起動した Process Group の後始末を、どこまで確かめられたか。

    Runner が戻ったあとに子孫が動き続けていると、timeout も「1 実行まで」も
    保証できない。**「親を wait できた」を後片付け完了と呼ばない。**
    """

    #: 封じ込めた PID namespace に生存 Process が 1 つも残っていないことを確認した。
    CONFIRMED_EMPTY = "CONFIRMED_EMPTY"
    #: KILL のあとも Process が残っている。
    RESIDUAL_PROCESSES = "RESIDUAL_PROCESSES"
    #: namespace を識別できない、`/proc` を読めないなどで残存を確かめられない。
    UNVERIFIABLE = "UNVERIFIABLE"


class Containment:
    """起動する Process をどこまで封じ込めているか。**Runner の判定根拠になる。**

    `PID_NAMESPACE` のときだけ、子孫の全数を数えられる（namespace からは
    `setsid()` でも `fork` でも離脱できない）。`NONE` のときは全数確認の手立てが
    無いので、後片付けは常に `UNVERIFIABLE` になる。
    """

    #: Sandbox Launcher が PID namespace を作り、その中で exec する。
    PID_NAMESPACE = "PID_NAMESPACE"
    #: 封じ込めなし。子孫の全数は確かめられない。
    NONE = "NONE"


@dataclass(frozen=True, slots=True)
class BoundaryVerdict:
    """起動前の能力検証の結果。**「測れた」と「満たした」を分ける。**"""

    satisfied: bool
    blocking_reasons: tuple[str, ...]
    details: Mapping[str, Any]


class BoundaryProbePort(Protocol):
    """この Machine で必要な境界が成立するかを測る境界。

    実装は Kernel の能力を **実際に触って** 測る。設定を読んだだけで満たしたと
    言わない。満たせないときは理由を返し、呼出側は Provider を起動しない。
    """

    def verdict(self, provider_id: str) -> BoundaryVerdict: ...


class CliOutcome(Enum):
    """Process がどう終わったか。**「送っていない」を推測しない。**"""

    #: Process が自分で終了し、終了 Code を観測できた。
    COMPLETED = "COMPLETED"
    #: 上限時間で打ち切った。送信済みか否かは分からない。
    TIMEOUT = "TIMEOUT"
    #: stdout か stderr が上限を超えた。読み込み中に打ち切った。
    OUTPUT_LIMIT_EXCEEDED = "OUTPUT_LIMIT_EXCEEDED"
    #: 起動そのものができなかった。**このときだけ「送っていない」と言える。**
    SPAWN_FAILED = "SPAWN_FAILED"
    #: 上記のどれとも判定できない。
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class CliLaunchSpec:
    """1 回の起動を完全に決める値。UI から自由に組み立てられない。"""

    provider_id: str
    model_id: str
    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: str
    runtime_hash: ContentHash
    runtime_projection: Mapping[str, Any]
    #: 封じ込めの種別。既定は「無し」。**主張は組立側が明示する。**
    containment: str = Containment.NONE
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        if not self.argv or not all(isinstance(item, str) and item for item in self.argv[:1]):
            raise ValueError("argv must start with a non-empty executable path")
        if not self.provider_id or not self.model_id or not self.cwd:
            raise ValueError("provider, model and cwd must not be empty")


@dataclass(frozen=True, slots=True)
class CliRunResult:
    """起動の観測結果。**stderr 本文も未検査 stdout の解釈も持ち出さない。**"""

    outcome: CliOutcome
    exit_code: int | None
    stdout: bytes
    stdout_truncated: bool
    stderr_bytes_observed: int
    stderr_truncated: bool
    diagnostic_id: str
    #: 起動した Process Group の後始末の結果。
    descendant_cleanup: DescendantCleanup = DescendantCleanup.UNVERIFIABLE
    #: 後始末のあとも残っている Process の PID。Secret ではなく復旧の手掛かりである。
    residual_pids: tuple[int, ...] = ()
    #: stderr本文ではなく、固定表から選んだ診断分類だけ。
    stderr_classifications: tuple[str, ...] = ()
    stderr_source_locations: tuple[tuple[str, int], ...] = ()

    @property
    def descendants_settled(self) -> bool:
        """子孫が 1 つも残っていないことを **確認できた** か。"""
        return self.descendant_cleanup is DescendantCleanup.CONFIRMED_EMPTY


@dataclass(frozen=True, slots=True)
class CliProviderStatus:
    """画面に出す Provider の状態。**推測した「ログイン済み」を混ぜない。**"""

    provider_id: str
    display_name: str
    installed: bool
    package_version: str | None
    profile_verified: bool
    restriction_summary: tuple[str, ...]
    residual_risks: tuple[str, ...]
    login_state: str
    login_hint: str
    models: tuple[str, ...]
    blocking_reason: str | None
    model_efforts: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    model_labels: Mapping[str, str] = field(default_factory=dict)


class CliProfilePort(Protocol):
    """検証済み profile から起動仕様を組み立てる境界。"""

    def statuses(self) -> tuple[CliProviderStatus, ...]: ...

    def resolve(
        self, *, provider_id: str, model_id: str, reasoning_effort: str | None = None
    ) -> CliLaunchSpec:
        """Provider と Model から起動仕様を作る。未登録は拒否する。"""
        ...

    def verify(self, spec: CliLaunchSpec) -> None:
        """起動直前の再照合。実行体・設定・強制 Policy が変わっていたら停止する。"""
        ...

    def attest(self, spec: CliLaunchSpec) -> Mapping[str, Any]:
        """実行前制限が **実際に効いていること** を測って返す。

        「設定を書いた」ではなく「触れられなかった」「無効だった」を返す。測れない
        場合は例外で止める。**測れないまま起動させない。** 返した値は Plan へ束縛
        され、起動直前にもう一度測って照合される。
        """
        ...


class CliRunnerPort(Protocol):
    def run(
        self,
        spec: CliLaunchSpec,
        *,
        stdin_payload: bytes,
        timeout_seconds: int,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> CliRunResult:
        """1 回だけ起動する。**Runner は再試行しない。**"""
        ...

    def stop(self) -> None:
        """走っている Process を止める。**終了時の後片付けのためだけにある。**

        止めたことは「送っていない」を意味しない。呼出側は結果を
        `EFFECT_UNKNOWN` として扱う。走っていなければ何もしない。
        """
        ...


class IdSourcePort(Protocol):
    def new_id(self) -> str: ...


@dataclass(frozen=True, slots=True)
class WorkbenchRecord:
    """Session の不変 Record。本文は CAS にあり、ここは Hash だけを持つ。"""

    session_id: str
    state: str
    version: int
    document_hash: ContentHash


@dataclass(frozen=True, slots=True)
class SavedConversationHistory:
    """Complete, verified local messages. IDs stay in the binding, outside model context."""

    messages: tuple[dict[str, Any], ...]
    source_hash: ContentHash


class ConversationHistoryPort(Protocol):
    def read_history(
        self, conversation_id: str, *, maximum_bytes: int
    ) -> SavedConversationHistory: ...


class WorkbenchStorePort(Protocol):
    def get(self, session_id: str) -> WorkbenchRecord | None: ...
    def list_recent(self, limit: int) -> tuple[WorkbenchRecord, ...]: ...
    def save(self, record: WorkbenchRecord, *, expected_version: int) -> None: ...


class InvocationJournalStorePort(Protocol):
    """`CliInvocationJournal` の永続化。**版付きで厳格に復元する。**"""

    def create_prepared(self, journal: CliInvocationJournal, *, session_id: str) -> None: ...
    def get_by_session(self, session_id: str) -> CliInvocationJournal | None: ...
    def update(self, journal: CliInvocationJournal, *, expected_store_version: int) -> None: ...


class WorkbenchPreferenceStorePort(Protocol):
    def get_preference(self, scope_hash: ContentHash) -> WorkbenchPreference | None: ...
    def save_preference(
        self, preference: WorkbenchPreference, *, expected_version: int
    ) -> None: ...
