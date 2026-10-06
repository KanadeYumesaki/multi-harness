"""Effect実行のOrchestrator。Policy Freshnessを最終Port呼出の直前で再検証する。

## Domainの判定だけでEffectを開始できない構造にする

Domain（`policy_freshness.py`）は「許すか」を答えるだけで、作用は起こさない。
しかし**呼出側がその答えを無視できる**なら、判定は助言でしかない。

本Orchestratorは、Effect Portを**自分だけが保持する**。呼出側はPortへ直接
届かず、`execute` を通すしかない。`execute` は必ず鮮度を再検証してから
Portへ触る。これは不変条件#3「Fencing Tokenを最終Storage書込み直前に再検証する。
Application層の検証だけで通さない」と同じ形である。

## 二重検証である

上流（Plan構築時など）で既に鮮度を確かめていても、ここで**もう一度**確かめる。

* 上流の検査から実際のI/Oまでには時間が経つ。その間に期限が切れうる。
* 上流を通らない経路が後から足されても、ここで止まる。

「さっき確かめた」を根拠にしない。判定と作用のあいだに時間があるなら、
作用の直前で確かめ直す。

## Stale判定後にRetry／Fallback／Queue投入をしない

止めたあとに「別の手で通す」経路があれば、止めた意味が無い。
`PolicyStaleError` は捕捉して再試行する対象ではなく、
**その実行を終わらせる**ためのものである。Orchestratorは自分では
一切の再試行・代替Provider・Queue投入を行わない。
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Final

from harness.domain._registry_generated import ErrorCode, EventType
from harness.domain.config_drift import ConfigDriftVerdict, RuntimeAttestation, detect_config_drift
from harness.domain.errors import HarnessError
from harness.domain.hashing import hash_canonical
from harness.domain.policy_freshness import (
    EffectKind,
    PolicyFreshnessDecision,
    PolicyFreshnessVerdict,
    evaluate_policy_freshness,
)
from harness.ports.effect_execution import (
    ClockPort,
    ExternalSendPort,
    PaidExecutionPort,
    ProcessLaunchPort,
    WorkspaceWritePort,
)
from harness.ports.event_ledger import EventLedgerPort, NewEvent
from harness.ports.operation_control import OperationAdmissionPort

__all__ = [
    "ConfigDriftError",
    "EffectOrchestrator",
    "EffectOutcome",
    "EffectRequest",
    "PolicyStaleError",
]

# Ledger Event payloadのDomain分離（§1.11）。
_EVENT_ARTIFACT_TYPE: Final[str] = "policy-freshness-event"
_EVENT_SCHEMA_MAJOR: Final[int] = 1


class PolicyStaleError(HarnessError):
    """Policyが古いため作用を起こさなかった。

    **捕捉して再試行しない。** 再試行は新しい作用であり、
    古いPolicyを根拠にした作用であることは変わらない。
    """

    def __init__(self, decision: PolicyFreshnessDecision, message: str) -> None:
        code = decision.error_code
        if code is None:  # pragma: no cover - 構成上到達しない
            code = ErrorCode.RUNTIME_SPEC_MISMATCH
        super().__init__(code, message)
        self.decision: Final[PolicyFreshnessDecision] = decision


class ConfigDriftError(HarnessError):
    """実行直前の実測が、承認時のRuntime Specと違う。

    **再試行しない。** 同じ設定で測れば同じ不一致が出る。
    別Providerへ Fallback するのは承認からさらに遠ざかる。
    """

    def __init__(self, verdict: ConfigDriftVerdict, message: str) -> None:
        code = verdict.error_code
        if code is None:  # pragma: no cover - 構成上到達しない
            code = ErrorCode.RUNTIME_SPEC_MISMATCH
        super().__init__(code, message)
        self.verdict: Final[ConfigDriftVerdict] = verdict


@dataclass(frozen=True, slots=True)
class EffectRequest:
    """1回の作用要求。"""

    attempt_id: str
    stream_id: str
    effect_kind: EffectKind
    policy_expires_at: str
    #: 既に外部作用を起こした可能性があるか。
    effect_attempted: bool = False
    endpoint: str = ""
    payload: bytes = b""
    argv: tuple[str, ...] = ()
    relative_path: str = ""
    tokens: int = 0
    prompt: str = ""
    #: Runtime Attestation。渡された場合、Effect開始前にDriftを検出する。
    attestation: RuntimeAttestation | None = None


@dataclass(frozen=True, slots=True)
class EffectOutcome:
    """作用の結果。止めた場合も結果である。"""

    attempt_state: str
    decision: PolicyFreshnessDecision
    appended_events: tuple[EventType, ...] = ()
    result: object | None = None

    @property
    def blocked(self) -> bool:
        return self.decision.blocked


@dataclass
class EffectOrchestrator:
    """Effect Portを保持し、鮮度検証を通した呼出だけをPortへ届ける。

    Portは private 属性ではなく通常のFieldだが、**呼出側はOrchestrator経由でしか
    作用を起こさない**という契約である。統合試験はPortをSpyに差し替えて
    「1度も呼ばれていない」を数える。
    """

    clock: ClockPort
    ledger: EventLedgerPort
    external_send: ExternalSendPort
    process_launch: ProcessLaunchPort
    workspace_write: WorkspaceWritePort
    paid_execution: PaidExecutionPort
    operation_gate: OperationAdmissionPort | None = None
    #: 監査用。Orchestratorが再検証した回数。
    revalidations: int = field(default=0, init=False)

    def execute(self, request: EffectRequest) -> EffectOutcome:
        with (
            self.operation_gate.admission(request.attempt_id, kind="effect")
            if self.operation_gate is not None
            else nullcontext()
        ):
            return self._execute(request)

    def _execute(self, request: EffectRequest) -> EffectOutcome:
        """鮮度を再検証し、通った場合だけPortを呼ぶ。

        **Portへ触る前に必ずここを通る。** 判定が `FRESH` でなければ
        1つのPortも呼ばない。
        """
        decision = self._revalidate(request)

        # Config Drift は Effect 開始「前」に見る。1つでも起こしてから
        # 気付いても、起きたことは取り消せない。
        if request.attestation is not None and decision.verdict is PolicyFreshnessVerdict.FRESH:
            drift = detect_config_drift(request.attestation)
            if drift.drifted:
                self._append_events(request.stream_id, drift.events, drift.state)
                raise ConfigDriftError(
                    drift,
                    f"runtime spec drifted for {request.attempt_id}; "
                    f"{request.effect_kind.value} not started",
                )

        if decision.verdict is PolicyFreshnessVerdict.RECOVERY_ONLY:
            # 既に起きたかもしれない作用がある。照合だけを許す。
            # 新しい作用は起こさないので、どのEffect Portも呼ばない。
            events = self._append(request.stream_id, decision)
            return EffectOutcome(
                attempt_state=decision.attempt_state,
                decision=decision,
                appended_events=events,
            )

        if decision.verdict is PolicyFreshnessVerdict.BLOCKED_STALE:
            events = self._append(request.stream_id, decision)
            raise PolicyStaleError(
                decision,
                f"policy expired at {request.policy_expires_at}; "
                f"{request.effect_kind.value} not started "
                f"(events={[e.value for e in events]})",
            )

        result = self._dispatch(request)
        return EffectOutcome(attempt_state="EXECUTING", decision=decision, result=result)

    def _revalidate(self, request: EffectRequest) -> PolicyFreshnessDecision:
        """最終Port呼出の直前で鮮度を測り直す。

        時刻はClock Portから取る。ここで直接時刻を参照すると、
        期限境界の試験が書けなくなる。
        """
        self.revalidations += 1
        return evaluate_policy_freshness(
            effect_kind=request.effect_kind,
            policy_expires_at=request.policy_expires_at,
            now=self.clock.now(),
            effect_attempted=request.effect_attempted,
        )

    def _append(self, stream_id: str, decision: PolicyFreshnessDecision) -> tuple[EventType, ...]:
        """判定のEvent列をLedgerへAppendする。

        Ledgerは**作用の記録**であって作用そのものではない。止めた事実を
        残さなければ、後から「なぜ動かなかったか」を示せない。
        """
        return self._append_events(stream_id, decision.events, decision.attempt_state)

    def _append_events(
        self, stream_id: str, events: tuple[EventType, ...], attempt_state: str
    ) -> tuple[EventType, ...]:
        """Event列をLedgerへAppendする。

        Ledgerは**作用の記録**であって作用そのものではない。止めた事実を
        残さなければ、後から「なぜ動かなかったか」を示せない。
        """
        if not events:
            return ()
        head = self.ledger.stream_head(stream_id)
        recorded_at = self.clock.now()
        self.ledger.append(
            [
                NewEvent(
                    stream_id=stream_id,
                    event_type=event.value,
                    # 本文はArtifactへ置き、LedgerにはHashだけを持つ（§15.7）。
                    # 判定内容は決定論的なので、同じ判定は同じHashになる。
                    payload_hash=hash_canonical(
                        {
                            "event_type": event.value,
                            "attempt_state": attempt_state,
                        },
                        artifact_type=_EVENT_ARTIFACT_TYPE,
                        schema_major=_EVENT_SCHEMA_MAJOR,
                    ),
                    recorded_at=recorded_at,
                )
                for event in events
            ],
            expected_stream_sequence=head,
        )
        return events

    def _dispatch(self, request: EffectRequest) -> object:
        """鮮度を満たした場合だけ呼ばれる実I/O。"""
        kind = request.effect_kind
        if kind is EffectKind.EXTERNAL_SEND:
            return self.external_send.send(request.endpoint, request.payload)
        if kind is EffectKind.LOCAL_READ:
            return self.process_launch.launch(list(request.argv))
        if kind is EffectKind.WORKSPACE_WRITE:
            return self.workspace_write.commit(request.relative_path, request.payload)
        reservation = self.paid_execution.reserve_budget(request.tokens)
        return self.paid_execution.invoke_provider(reservation, request.prompt)
