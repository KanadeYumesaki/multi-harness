"""Case観測の記録。統合試験がここへ**測った値だけ**を書く。

## なぜ試験の外へ出すのか

統合試験は observed 値を関数内で計算して `assert` している。値は関数を抜けると
消えるため、Case Evidence を作る側から見えない。Evidence を作るには
「試験が何を観測したか」を試験の外へ出す必要がある。

## ここに書いてよいもの／いけないもの

**書いてよいのは観測値だけである。**

期待値（`tests.yaml` の `expected_*`）をここへ書かない。期待値を観測値として
記録すると、Evidence は必ず一致し、**何も検証していないのに全件PASSになる**。
突合は Emitter が Registry の期待値との間で行う。ここは片側だけを持つ。

`status` も持たない。合否は Emitter が導出する。

## 副作用は「測っていない」と「0件だった」を区別する

`side_effects` の既定は `None`（未測定）である。`observe_side_effects()` を
呼ばない限り `None` のままで、Emitter は FAIL にする。

**0 で初期化しない。** 0 は「副作用が無かった」という積極的な主張であり、
測っていない状態とは意味が正反対である。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

__all__ = ["CaseObservation", "ObservedSideEffects"]


@dataclass(frozen=True, slots=True)
class ObservedSideEffects:
    """実測した副作用の回数。存在すること自体が「測った」証拠である。"""

    network_calls: int
    process_launches: int
    workspace_commits: int
    external_effects: int
    ledger_effect_attempts: int

    def as_dict(self) -> dict[str, int]:
        return {
            "network_calls": self.network_calls,
            "process_launches": self.process_launches,
            "workspace_commits": self.workspace_commits,
            "external_effects": self.external_effects,
            "ledger_effect_attempts": self.ledger_effect_attempts,
        }


@dataclass
class CaseObservation:
    """1 Case分の観測。期待値もstatusも持たない。"""

    case_id: str
    node_id: str
    observed_state: str | None = None
    observed_error_code: str | None = None
    observed_events: tuple[str, ...] = ()
    ledger_head_before: int | None = None
    ledger_head_after: int | None = None
    actual_subject_id: str | None = None
    side_effects: ObservedSideEffects | None = None
    input_payload: dict[str, Any] | None = None
    fault_injection: dict[str, Any] | None = None
    unit_execution: dict[str, Any] | None = None
    typed_result: dict[str, Any] | None = None
    _recorded: bool = field(default=False, repr=False)
    _ledger_observed: bool = field(default=False, repr=False)

    # -- 記録API ---------------------------------------------------------

    def record(
        self,
        *,
        state: str,
        subject_id: str,
        error_code: str | None = None,
    ) -> None:
        """観測したStateとError Codeを記録する。

        `state` と `subject_id` は必須である。「どのSubjectが」「どの状態に
        なったか」の片方でも欠けると、その観測は何についての観測か決まらない。

        **Event列はここでは受け取らない。** `observe_ledger()` だけが設定する。
        任意の列を渡せる限り、Request列でもVerdict予測列でも期待値でも書けて
        しまい、規約は「守るつもり」に留まる（設計書§19.1.1）。
        """
        if not state:
            raise ValueError("observed_state が空である")
        if not subject_id:
            raise ValueError("actual_subject_id が空である")
        self.observed_state = state
        self.observed_error_code = error_code
        self.actual_subject_id = subject_id
        self._recorded = True

    def record_result(self, result: Any) -> None:
        """Read a typed production result; never infer its namespace from expectations.

        These decision results do not claim storage durability. Ledger and
        side-effect observations still have to be supplied by the executing test.
        Unknown result classes are deliberately unsupported.
        """
        from harness.application.approval_plan_orchestrator import AttemptGuardOutcome
        from harness.domain.attempt import ActionAttempt
        from harness.domain.event_order import AppendOrderVerdict
        from harness.domain.manifest_subject import ManifestValidationResult
        from harness.domain.run_terminal import RunTerminalVerdict

        contracts = {
            AttemptGuardOutcome: ("ACTION_ATTEMPT", "attempt_id"),
            AppendOrderVerdict: ("EVENT_APPEND_RESULT", "append_result_id"),
            ManifestValidationResult: ("MANIFEST_VALIDATION_RESULT", "manifest_validation_id"),
            RunTerminalVerdict: ("RUN", "run_id"),
        }
        contract = contracts.get(type(result))
        if contract is None:
            raise ValueError("UNSUPPORTED_OBSERVED_RESULT_TYPE")
        subject_type, id_field = contract
        if type(result) is AttemptGuardOutcome and (
            type(result.attempt) is not ActionAttempt
            or result.attempt_id != result.attempt.attempt_id
            or result.state != result.attempt.state
            or type(result.effect_invocations) is not int
            or result.effect_invocations < 0
        ):
            raise ValueError("ATTEMPT_RESULT_INCONSISTENT")
        self.record(
            state=result.state,
            subject_id=getattr(result, id_field),
            error_code=result.error_code.value if result.error_code else None,
        )
        self.typed_result = {
            "subject_type": subject_type,
            "result_class": type(result).__module__ + "." + type(result).__qualname__,
            "subject_id": self.actual_subject_id,
            "state": self.observed_state,
            "error_code": self.observed_error_code,
            "durability_observation": "NOT_APPLICABLE",
        }

        if type(result) is AttemptGuardOutcome:
            self.typed_result["attempt_record"] = {
                "result_class": "harness.domain.attempt.ActionAttempt",
                "subject_id": result.attempt.attempt_id,
                "state": result.attempt.state,
            }
            self.typed_result["effect_invocations"] = result.effect_invocations

    def observe_ledger(self, ledger: Any, *, head_before: int) -> None:
        """正本Ledger（またはSpy）からAppend列とHeadを**同時に**読む。

        列だけを見ると、拒否されたはずのAppendが実は成功していた場合を
        見逃す。Headを併せて記録し、拒否時にHeadが動いていないことを
        後から確かめられるようにする（設計書§19.1.1）。
        """
        self.observed_events = tuple(ledger.appended)
        self.ledger_head_before = int(head_before)
        self.ledger_head_after = int(ledger.head)
        self._ledger_observed = True

    def observe_side_effects(
        self,
        *,
        network_calls: int,
        process_launches: int,
        workspace_commits: int,
        external_effects: int,
        ledger_effect_attempts: int,
    ) -> None:
        """副作用を実測値で記録する。呼ばなければ `None` のままである。"""
        self.side_effects = ObservedSideEffects(
            network_calls=network_calls,
            process_launches=process_launches,
            workspace_commits=workspace_commits,
            external_effects=external_effects,
            ledger_effect_attempts=ledger_effect_attempts,
        )

    def observe_spy(self, spy: Any, ledger: Any) -> None:
        """副作用SpyとLedger Spyから実測値を読む。

        属性が欠けていれば例外にする。`getattr(..., 0)` で埋めると、
        Spyの形が変わった瞬間に「0件だった」という嘘を記録し始める。
        """
        self.observe_side_effects(
            network_calls=int(spy.network_calls),
            process_launches=int(spy.process_launches),
            workspace_commits=int(spy.workspace_commits),
            external_effects=int(spy.external_effects),
            ledger_effect_attempts=int(ledger.effect_attempts),
        )

    def record_input(self, payload: dict[str, Any]) -> None:
        """Caseへ与えた入力を記録する。Input Fixture Hashの元になる。"""
        # 決定的に書けない入力はFixture Hashにならない。ここで落とす。
        json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.input_payload = payload

    def record_fault(self, *, fault_point: str, fault_kind: str, deterministic: bool) -> None:
        self.fault_injection = {
            "fault_point": fault_point,
            "fault_kind": fault_kind,
            "deterministic": deterministic,
        }

    # -- 出力 -------------------------------------------------------------

    @property
    def complete(self) -> bool:
        return self._recorded and self.side_effects is not None and self._ledger_observed

    def as_dict(self) -> dict[str, Any]:
        return {
            "unit_execution": self.unit_execution,
            "typed_result": self.typed_result,
            "case_id": self.case_id,
            "node_id": self.node_id,
            "observed_state": self.observed_state,
            "observed_error_code": self.observed_error_code,
            "observed_event_sequence": list(self.observed_events),
            "ledger_observed": self._ledger_observed,
            "ledger_head_before": self.ledger_head_before,
            "ledger_head_after": self.ledger_head_after,
            "actual_subject_id": self.actual_subject_id,
            "side_effects": self.side_effects.as_dict() if self.side_effects else None,
            "input_payload": self.input_payload,
            "fault_injection": self.fault_injection,
            "recorded": self._recorded,
        }
