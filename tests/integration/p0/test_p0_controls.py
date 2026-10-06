"""残りP0 6 Case の統合試験。

対象：

* `AT-MANIFEST-SUBJECT-001/VALID_TYPED_SUBJECT`／`MIXED_SUBJECT_STATE`
* `AT-EVENT-ORDER-001/SEQUENCE_REGRESSION`
* `AT-RUN-TERMINAL-001/NO_RELEASE`／`UNRECONCILED_TO_CANCELLED`
* `AT-CONFIG-001/DRIFT`

期待値は `design-source/registries/tests.yaml` から読み取って突き合わせる。
試験側へ手入力しない（不変条件#18）。

**拒否経路では副作用カウンタが全て0であること**を毎回数える。
「止めると答えた」ことと「実際に何も起きなかった」ことは別である。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from harness.application.effect_orchestrator import (
    ConfigDriftError,
    EffectOrchestrator,
    EffectRequest,
)
from harness.domain.config_drift import RuntimeAttestation, detect_config_drift
from harness.domain.event_order import EventAppendRequest, verify_append_order
from harness.domain.hashing import ContentHash
from harness.domain.manifest_subject import (
    ManifestSubjectBinding,
    validate_manifest_subject,
)
from harness.domain.policy_freshness import EffectKind
from harness.domain.run_terminal import RunTerminalRequest, evaluate_run_terminal
from harness.ports.event_ledger import AppendResult

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
TESTS_YAML = REPO_ROOT / "design-source" / "registries" / "tests.yaml"

PLAN_HASH = ContentHash.parse("sha256:" + "a" * 64)
OTHER_HASH = ContentHash.parse("sha256:" + "b" * 64)
FRESH_UNTIL = "2026-08-18T00:00:00Z"
NOW = "2026-08-17T00:00:00Z"


def _case(test_id: str, case_id: str) -> dict[str, Any]:
    rows = yaml.safe_load(TESTS_YAML.read_text(encoding="utf-8"))["test_cases"]
    for row in rows:
        if row["test_id"] == test_id and row["case_id"] == case_id:
            return dict(row)
    raise AssertionError(f"Registryに {test_id}/{case_id} が無い")


# --------------------------------------------------------------------------
# 副作用Spy。Policy Freshness の統合試験と同じ観測項目を数える。
# --------------------------------------------------------------------------


@dataclass
class FrozenClock:
    value: str = NOW

    def now(self) -> str:
        return self.value


@dataclass
class SideEffectSpy:
    network_calls: int = 0
    network_bytes_sent: int = 0
    process_launches: int = 0
    workspace_commits: int = 0
    budget_reservations_created: int = 0
    provider_calls: int = 0

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


@dataclass(frozen=True)
class _LedgerEvent:
    """LedgerSpyがAppendを数えるための最小Event。`event_type` だけを持つ。"""

    event_type: str


@dataclass
class LedgerSpy:
    appended: list[str] = field(default_factory=list)
    head: int = 0

    def append(self, events: Any, *, expected_stream_sequence: int) -> AppendResult:
        listed = list(events)
        assert expected_stream_sequence == self.head, "Stream headとCASが噛み合っていない"
        first = self.head + 1
        for event in listed:
            self.appended.append(event.event_type)
        self.head += len(listed)
        return AppendResult(
            appended_count=len(listed),
            first_sequence_number=first,
            last_sequence_number=self.head,
            head_event_hash=ContentHash.parse("sha256:" + "0" * 64),
        )

    def stream_head(self, stream_id: str) -> int:
        return self.head

    def load_stream(self, stream_id: str, *, after_sequence: int = 0) -> list[Any]:
        return []

    def verify_chain(self, stream_id: str) -> Any:
        raise NotImplementedError

    @property
    def effect_attempts(self) -> int:
        started = {"ACTION_PREPARED", "ACTION_EXECUTING", "EFFECT_ATTEMPTED"}
        return sum(1 for name in self.appended if name in started)


def _orchestrator() -> tuple[EffectOrchestrator, SideEffectSpy, LedgerSpy]:
    spy = SideEffectSpy()
    ledger = LedgerSpy()
    return (
        EffectOrchestrator(
            clock=FrozenClock(),
            ledger=ledger,
            external_send=spy,
            process_launch=spy,
            workspace_write=spy,
            paid_execution=spy,
        ),
        spy,
        ledger,
    )


def _binding_payload(binding: ManifestSubjectBinding) -> dict[str, Any]:
    """Input Fixture 用の決定的な入力表現。Fixture Hashの元になる。"""
    return {
        "manifest_validation_id": binding.manifest_validation_id,
        "expected_subject_type": binding.expected_subject_type,
        "expected_subject_id": binding.expected_subject_id,
        "expected_state": binding.expected_state,
        "plan_content_hash": str(binding.plan_content_hash),
        "approval_plan_content_hash": str(binding.approval_plan_content_hash),
        "attempt_plan_content_hash": str(binding.attempt_plan_content_hash),
    }


def _append_all(ledger: LedgerSpy, event_types: list[str]) -> None:
    """Domainが決めたEvent列を、Application層と同じようにLedgerへ積む。

    Verdictの列をそのまま観測値にしない。**Ledgerに残った列だけ**が
    observed_event_sequence である（設計書§19.1.1）。
    """
    if not event_types:
        return
    ledger.append(
        [_LedgerEvent(name) for name in event_types],
        expected_stream_sequence=ledger.head,
    )


def _observe_effects(observation: Any, spy: SideEffectSpy, ledger: LedgerSpy) -> None:
    """観測した副作用カウンタを記録へ渡す。

    Spyを持たないDomain呼び出しでも、Spyを**組んでから**呼ぶ。
    0を書き込むのではなく、0であることを数える。両者は意味が違う。
    """
    observation.observe_spy(spy, ledger)


def _assert_no_side_effects(spy: SideEffectSpy, ledger: LedgerSpy) -> None:
    """拒否経路で要求される全カウンタが0であること。"""
    assert spy.network_calls == 0
    assert spy.process_launches == 0
    assert spy.workspace_commits == 0
    assert spy.external_effects == 0
    assert ledger.effect_attempts == 0
    assert spy.network_bytes_sent == 0
    assert spy.budget_reservations_created == 0
    assert spy.provider_calls == 0


def _binding(**overrides: Any) -> ManifestSubjectBinding:
    base: dict[str, Any] = {
        "manifest_validation_id": "mv-1",
        "expected_subject_type": "ACTION_ATTEMPT",
        "expected_subject_id": "attempt-1",
        "expected_state": "SUCCEEDED",
        "plan_content_hash": PLAN_HASH,
        "approval_plan_content_hash": PLAN_HASH,
        "attempt_plan_content_hash": PLAN_HASH,
    }
    base.update(overrides)
    return ManifestSubjectBinding(**base)


# ==========================================================================
# 1. InvocationManifest Subject
# ==========================================================================


@pytest.mark.case("AT-MANIFEST-SUBJECT-001/VALID_TYPED_SUBJECT")
def test_typed_expected_state_subject_is_accepted(case_observation: Any) -> None:
    """Subject型の名前空間に属するStateを宣言したManifestは通ること。"""
    expected = _case("AT-MANIFEST-SUBJECT-001", "VALID_TYPED_SUBJECT")
    binding = _binding()
    _, spy, ledger = _orchestrator()
    case_observation.record_input({"binding": _binding_payload(binding)})

    result = validate_manifest_subject(binding)

    assert result.state == expected["expected_state"]
    assert result.error_code is None
    assert result.accepted is True

    head_before = ledger.head
    case_observation.record_result(result)
    # Manifest Subject 検証は Ledger へ何もAppendしない。**見て0件**である。
    case_observation.observe_ledger(ledger, head_before=head_before)
    assert case_observation.observed_events == tuple(expected["expected_event_sequence"] or [])
    _observe_effects(case_observation, spy, ledger)


@pytest.mark.case("AT-MANIFEST-SUBJECT-001/MIXED_SUBJECT_STATE")
def test_action_state_used_for_outbox_subject_is_rejected(case_observation: Any) -> None:
    """`OUTBOX_RECORD` のSubjectへ `ACTION_ATTEMPT` のStateを書いたら拒否すること。

    `SUCCEEDED` はEnum値として実在するので、名前だけ見れば正しく見える。
    しかし `OUTBOX_RECORD` は決してその状態にならない。**成立しない条件**を
    宣言したManifestを通すと、判定できないものを判定できたことにする。
    """
    expected = _case("AT-MANIFEST-SUBJECT-001", "MIXED_SUBJECT_STATE")
    binding = _binding(expected_subject_type="OUTBOX_RECORD", expected_state="SUCCEEDED")
    _, spy, ledger = _orchestrator()
    case_observation.record_input({"binding": _binding_payload(binding)})

    result = validate_manifest_subject(binding)

    assert result.state == expected["expected_state"]
    assert result.error_code is not None
    assert result.error_code.value == expected["expected_error_code"]
    assert result.rejected is True

    head_before = ledger.head
    case_observation.record_result(result)
    # 拒否判定も Ledger へは何もAppendしない。**見て0件**である。
    case_observation.observe_ledger(ledger, head_before=head_before)
    assert case_observation.observed_events == tuple(expected["expected_event_sequence"] or [])
    _observe_effects(case_observation, spy, ledger)


@pytest.mark.parametrize(
    "subject_type,state",
    [
        ("RUN", "SUCCEEDED"),  # SUCCEEDED は ACTION_ATTEMPT のもの
        ("ACTION_ATTEMPT", "COMPLETED"),  # COMPLETED は RUN のもの
        ("OUTBOX_RECORD", "CANCELLED"),
        ("EVENT_APPEND_RESULT", "PLANNING"),
    ],
)
def test_cross_namespace_state_is_always_rejected(subject_type: str, state: str) -> None:
    """他の名前空間のStateを流用できないこと。"""
    result = validate_manifest_subject(
        _binding(expected_subject_type=subject_type, expected_state=state)
    )
    assert result.rejected is True
    assert result.error_code is not None
    assert result.error_code.value == "EXPECTED_STATE_SUBJECT_MISMATCH"


def test_unknown_subject_type_is_rejected() -> None:
    """未知のSubject型を通さないこと。判定基準が存在しない。"""
    result = validate_manifest_subject(_binding(expected_subject_type="NOT_A_NAMESPACE"))
    assert result.rejected is True


def test_plan_approval_attempt_must_share_one_plan_hash() -> None:
    """Plan／Approval／Attemptの束縛が食い違ったら拒否すること。

    片方だけ差し替えられると、承認された内容と実行された内容が
    食い違ったまま検証を通る。
    """
    for field_name in (
        "approval_plan_content_hash",
        "attempt_plan_content_hash",
        "plan_content_hash",
    ):
        result = validate_manifest_subject(_binding(**{field_name: OTHER_HASH}))
        assert result.rejected is True, field_name
        assert result.error_code is not None


def test_empty_subject_id_is_rejected() -> None:
    """Subject IDが空のManifestを通さないこと。"""
    assert validate_manifest_subject(_binding(expected_subject_id="")).rejected is True


# ==========================================================================
# 2. Event Order
# ==========================================================================


def _append_request(**overrides: Any) -> EventAppendRequest:
    base: dict[str, Any] = {
        "append_result_id": "ar-1",
        "stream_id": "ACTION_ATTEMPT_STREAM",
        "expected_stream_sequence": 5,
        "event_types": ("PLAN_RESOLVED",),
        "attempt_state": "WAITING_POLICY",
    }
    base.update(overrides)
    return EventAppendRequest(**base)


@pytest.mark.case("AT-EVENT-ORDER-001/SEQUENCE_REGRESSION")
def test_sequence_regression_is_rejected_without_changing_attempt_state(
    case_observation: Any,
) -> None:
    """Sequenceの逆行を拒否し、Attempt Stateを動かさないこと。

    Appendが拒否されたのにAttemptだけ進むと、Ledgerと状態Storeが食い違う。
    """
    expected = _case("AT-EVENT-ORDER-001", "SEQUENCE_REGRESSION")
    _, spy, ledger = _orchestrator()

    # `attempt_state == WAITING_POLICY` は PLAN_RESOLVED がAppend済みという意味である
    # （RegistryのassertionsとStateの対応）。その状態を**実際に作る**。
    ledger.append([_LedgerEvent("PLAN_RESOLVED")], expected_stream_sequence=0)
    head_before = ledger.head

    request = _append_request(expected_stream_sequence=3)
    case_observation.record_input(
        {
            "append_result_id": request.append_result_id,
            "stream_id": request.stream_id,
            "expected_stream_sequence": request.expected_stream_sequence,
            "event_types": list(request.event_types),
            "attempt_state": request.attempt_state,
            "current_head": head_before,
        }
    )

    verdict = verify_append_order(request, current_head=head_before)

    assert verdict.state == expected["expected_state"]
    assert verdict.error_code is not None
    assert verdict.error_code.value == expected["expected_error_code"]
    # Case assertions: attempt_state_unchanged == true / attempt_state == WAITING_POLICY
    assert verdict.attempt_state == "WAITING_POLICY"
    assert verdict.rejected is True
    # 拒否されたAppendはLedgerへ何も足さない。headが動いていないことを数える。
    assert ledger.head == head_before
    assert ledger.appended == expected["expected_event_sequence"]

    case_observation.record_result(verdict)
    # 観測元はLedgerである。Request列でもVerdict予測でも期待値でもない。
    case_observation.observe_ledger(ledger, head_before=head_before)
    assert case_observation.ledger_head_after == head_before, "拒否なのにHeadが動いた"
    _observe_effects(case_observation, spy, ledger)


def test_sequence_skip_ahead_is_also_rejected() -> None:
    """飛ばしも拒否すること。

    逆行だけを見ていると、間のEventが無いまま先へ進む経路が残る。
    """
    verdict = verify_append_order(_append_request(expected_stream_sequence=9), current_head=5)
    assert verdict.rejected is True
    assert verdict.error_code is not None
    assert verdict.error_code.value == "EVENT_ORDER_VIOLATION"


def test_matching_sequence_is_accepted() -> None:
    """一致すれば通ること。常に拒否する実装では検査にならない。"""
    verdict = verify_append_order(_append_request(expected_stream_sequence=5), current_head=5)
    assert verdict.accepted is True
    assert verdict.error_code is None


def test_append_at_empty_stream_head_is_accepted() -> None:
    """空Streamの先頭Appendが通ること（境界）。"""
    verdict = verify_append_order(_append_request(expected_stream_sequence=0), current_head=0)
    assert verdict.accepted is True


def test_appending_only_later_events_does_not_bypass_the_check() -> None:
    """後続Eventだけを足して通す経路が無いこと。

    Event列の中身が何であれ、順序判定は `expected == head` だけで決まる。
    """
    verdict = verify_append_order(
        _append_request(
            expected_stream_sequence=3,
            event_types=("ACTION_BLOCKED", "EVALUATION_COMPLETED"),
        ),
        current_head=5,
    )
    assert verdict.rejected is True


def test_empty_event_list_is_a_programming_error() -> None:
    """Eventの無いAppend要求を黙って受理しないこと。"""
    with pytest.raises(ValueError):
        verify_append_order(_append_request(event_types=()), current_head=5)


# ==========================================================================
# 3. Run Terminal
# ==========================================================================


@pytest.mark.case("AT-RUN-TERMINAL-001/NO_RELEASE")
def test_completion_without_release_decision_is_blocked(case_observation: Any) -> None:
    """Release Decisionが無いまま`COMPLETED`にしないこと。

    評価が終わったことと、出してよいと決めたことは別である。
    """
    expected = _case("AT-RUN-TERMINAL-001", "NO_RELEASE")
    _, spy, ledger = _orchestrator()
    case_observation.record_input(
        {
            "run_id": "run-1",
            "requested_terminal_state": "COMPLETED",
            "release_decision_id": None,
            "unreconciled_effect_count": 0,
        }
    )
    verdict = evaluate_run_terminal(
        RunTerminalRequest(
            run_id="run-1",
            requested_terminal_state="COMPLETED",
            release_decision_id=None,
            unreconciled_effect_count=0,
        )
    )
    assert verdict.state == expected["expected_state"]
    assert verdict.error_code is not None
    assert verdict.error_code.value == expected["expected_error_code"]
    assert [e.value for e in verdict.events] == expected["expected_event_sequence"]
    # Case assertions: completed_event_count == 0 / release_decision_id == null
    assert verdict.completed_event_count == 0
    assert verdict.ended_at is None

    head_before = ledger.head
    # Domainが決めた列を、Application層と同じようにLedgerへ積んでから観測する。
    _append_all(ledger, [event.value for event in verdict.events])

    case_observation.record_result(verdict)
    case_observation.observe_ledger(ledger, head_before=head_before)
    assert case_observation.observed_events == tuple(expected["expected_event_sequence"])
    _observe_effects(case_observation, spy, ledger)


@pytest.mark.case("AT-RUN-TERMINAL-001/UNRECONCILED_TO_CANCELLED")
def test_unreconciled_effect_blocks_cancelled(case_observation: Any) -> None:
    """未照合Effectがあるまま`CANCELLED`にしないこと。

    判定できないものをCancelledにすると、起きなかったことにされる。
    """
    expected = _case("AT-RUN-TERMINAL-001", "UNRECONCILED_TO_CANCELLED")
    _, spy, ledger = _orchestrator()
    case_observation.record_input(
        {
            "run_id": "run-1",
            "requested_terminal_state": "CANCELLED",
            "release_decision_id": "rd-1",
            "unreconciled_effect_count": 1,
        }
    )
    verdict = evaluate_run_terminal(
        RunTerminalRequest(
            run_id="run-1",
            requested_terminal_state="CANCELLED",
            release_decision_id="rd-1",
            unreconciled_effect_count=1,
        ),
        now="2026-08-17T00:00:00Z",
    )
    assert verdict.state == expected["expected_state"]
    assert verdict.error_code is not None
    assert verdict.error_code.value == expected["expected_error_code"]
    assert [e.value for e in verdict.events] == expected["expected_event_sequence"]
    # Case assertions: requested_terminal_state == CANCELLED / ended_at == null
    assert verdict.requested_terminal_state == "CANCELLED"

    head_before = ledger.head
    # Domainが決めた列を、Application層と同じようにLedgerへ積んでから観測する。
    _append_all(ledger, [event.value for event in verdict.events])

    case_observation.record_result(verdict)
    case_observation.observe_ledger(ledger, head_before=head_before)
    assert case_observation.observed_events == tuple(expected["expected_event_sequence"])
    _observe_effects(case_observation, spy, ledger)
    assert verdict.ended_at is None


def test_unreconciled_effect_outranks_a_present_release_decision() -> None:
    """Release Decisionがあっても未照合Effectがあれば終端させないこと。

    確かめていないものを、承認で上書きしない。
    """
    verdict = evaluate_run_terminal(
        RunTerminalRequest(
            run_id="run-1",
            requested_terminal_state="COMPLETED",
            release_decision_id="rd-1",
            unreconciled_effect_count=2,
        )
    )
    assert verdict.state == "BLOCKED_REPAIR_REQUIRED"
    assert verdict.ended_at is None


def test_completion_with_release_decision_and_no_unreconciled_effect_succeeds() -> None:
    """条件を満たせば終端できること。"""
    verdict = evaluate_run_terminal(
        RunTerminalRequest(
            run_id="run-1",
            requested_terminal_state="COMPLETED",
            release_decision_id="rd-1",
            unreconciled_effect_count=0,
        ),
        now="2026-08-17T00:00:00Z",
    )
    assert verdict.state == "COMPLETED"
    assert verdict.error_code is None
    assert verdict.completed_event_count == 1
    assert verdict.ended_at == "2026-08-17T00:00:00Z"


def test_cancel_without_unreconciled_effect_succeeds() -> None:
    """未照合が無ければCancelできること。"""
    verdict = evaluate_run_terminal(
        RunTerminalRequest(
            run_id="run-1",
            requested_terminal_state="CANCELLED",
            release_decision_id=None,
            unreconciled_effect_count=0,
        ),
        now="2026-08-17T00:00:00Z",
    )
    assert verdict.state == "CANCELLED"
    assert verdict.error_code is None


def test_negative_unreconciled_count_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        evaluate_run_terminal(
            RunTerminalRequest(
                run_id="run-1",
                requested_terminal_state="CANCELLED",
                release_decision_id=None,
                unreconciled_effect_count=-1,
            )
        )


# ==========================================================================
# 4. Config Drift
# ==========================================================================


def _attestation(attested: ContentHash) -> RuntimeAttestation:
    return RuntimeAttestation(
        attempt_id="attempt-1",
        resolved_spec_hash=PLAN_HASH,
        attested_spec_hash=attested,
    )


def _drift_request(attested: ContentHash) -> EffectRequest:
    return EffectRequest(
        attempt_id="attempt-1",
        stream_id="ACTION_ATTEMPT_STREAM",
        effect_kind=EffectKind.EXTERNAL_SEND,
        policy_expires_at=FRESH_UNTIL,
        endpoint="https://example.invalid/x",
        payload=b"payload",
        attestation=_attestation(attested),
    )


@pytest.mark.case("AT-CONFIG-001/DRIFT")
def test_provider_config_drift_blocks_before_any_effect(case_observation: Any) -> None:
    """実測が承認時のSpecと違えば、Effectを1つも起こさずに止めること。"""
    expected = _case("AT-CONFIG-001", "DRIFT")
    orchestrator, spy, ledger = _orchestrator()
    case_observation.record_input({"observed_runtime_spec_hash": str(OTHER_HASH)})

    with pytest.raises(ConfigDriftError) as excinfo:
        orchestrator.execute(_drift_request(OTHER_HASH))

    error = excinfo.value
    assert error.code.value == expected["expected_error_code"]
    assert error.verdict.state == expected["expected_state"]
    # Ledgerに期待どおりの順序で残る。
    assert ledger.appended == expected["expected_event_sequence"]
    # 拒否経路：全カウンタ0。
    _assert_no_side_effects(spy, ledger)

    case_observation.record(
        state=error.verdict.state,
        subject_id="attempt-config-drift",
        error_code=error.code.value,
    )
    # Orchestrator が実際にAppendした列を読む。head は 0 から動いている。
    case_observation.observe_ledger(ledger, head_before=0)
    assert case_observation.ledger_head_after == len(expected["expected_event_sequence"])
    _observe_effects(case_observation, spy, ledger)


def test_config_drift_is_not_retryable_or_fallbackable() -> None:
    """Driftを再試行・Fallbackへ流さないこと。

    同じ設定で測れば同じ不一致が出る。別Providerへ切り替えるのは
    承認からさらに遠ざかる。
    """
    verdict = detect_config_drift(_attestation(OTHER_HASH))
    assert verdict.drifted is True
    assert verdict.auto_reexecution_allowed is False
    assert verdict.fallback_allowed is False


def test_repeated_drift_never_reaches_the_port() -> None:
    """再試行しても作用が起きないこと。"""
    orchestrator, spy, ledger = _orchestrator()
    for _ in range(3):
        with pytest.raises(ConfigDriftError):
            orchestrator.execute(_drift_request(OTHER_HASH))
    _assert_no_side_effects(spy, ledger)


@pytest.mark.parametrize("kind", list(EffectKind))
def test_drift_blocks_every_effect_kind(kind: EffectKind) -> None:
    """作用の種類を変えてもDriftで止まること（Fallback禁止）。"""
    orchestrator, spy, ledger = _orchestrator()
    request = EffectRequest(
        attempt_id="attempt-1",
        stream_id="ACTION_ATTEMPT_STREAM",
        effect_kind=kind,
        policy_expires_at=FRESH_UNTIL,
        endpoint="https://example.invalid/x",
        payload=b"payload",
        argv=("/bin/true",),
        relative_path="out.txt",
        tokens=10,
        prompt="p",
        attestation=_attestation(OTHER_HASH),
    )
    with pytest.raises(ConfigDriftError):
        orchestrator.execute(request)
    _assert_no_side_effects(spy, ledger)


def test_matching_attestation_reaches_the_port_exactly_once() -> None:
    """一致すれば実際に作用が起きること。**対象Portはちょうど1回**。"""
    orchestrator, spy, ledger = _orchestrator()
    outcome = orchestrator.execute(_drift_request(PLAN_HASH))
    assert outcome.blocked is False
    assert spy.network_calls == 1
    assert spy.external_effects == 1
    # 他のPortは呼ばれていない。
    assert spy.process_launches == 0
    assert spy.workspace_commits == 0
    assert spy.provider_calls == 0
    # 止めていないので ACTION_BLOCKED は出ない。
    assert "ACTION_BLOCKED" not in ledger.appended


@pytest.mark.parametrize(
    "kind,counter",
    [
        (EffectKind.EXTERNAL_SEND, "network_calls"),
        (EffectKind.LOCAL_READ, "process_launches"),
        (EffectKind.WORKSPACE_WRITE, "workspace_commits"),
        (EffectKind.PAID_EXECUTION, "provider_calls"),
    ],
)
def test_fresh_path_invokes_its_port_exactly_once(kind: EffectKind, counter: str) -> None:
    """許可経路では対象Portがちょうど1回呼ばれること。

    0回なら止まっている。2回以上なら重複実行である。
    """
    orchestrator, spy, _ = _orchestrator()
    orchestrator.execute(
        EffectRequest(
            attempt_id="attempt-1",
            stream_id="ACTION_ATTEMPT_STREAM",
            effect_kind=kind,
            policy_expires_at=FRESH_UNTIL,
            endpoint="https://example.invalid/x",
            payload=b"payload",
            argv=("/bin/true",),
            relative_path="out.txt",
            tokens=10,
            prompt="p",
            attestation=_attestation(PLAN_HASH),
        )
    )
    assert getattr(spy, counter) == 1


def test_drift_is_checked_before_effects_even_when_policy_is_fresh() -> None:
    """Policyが新しくてもDriftで止まること。

    2つのGateは独立している。片方が通ったことをもう片方の根拠にしない。
    """
    orchestrator, spy, ledger = _orchestrator()
    with pytest.raises(ConfigDriftError):
        orchestrator.execute(_drift_request(OTHER_HASH))
    assert orchestrator.revalidations == 1
    _assert_no_side_effects(spy, ledger)
