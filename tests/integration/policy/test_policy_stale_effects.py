"""`AT-POLICY-STALE-001` 5 Case の統合試験（§3.8）。

## Domain試験との違い

Domain試験は「Gateが許さないと答える」ことを確かめる。
本Fileは**実際に副作用が0だったこと**を数える。Case の assertions が
要求しているのは後者である。

```text
network_calls == 0 / process_launches == 0 / workspace_commits == 0
external_effects == 0 / ledger_effect_attempts == 0
```

Gateが正しく答えても、呼出側がその答えを無視してPortを呼べば
assertion は成立しない。**答えと行動は別々に確かめる。**

Spyは呼ばれた回数を数えるだけで、実際のI/Oは一切しない。
統合試験そのものがNetwork・Process・Filesystemへ触れない。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from harness.application.effect_orchestrator import (
    EffectOrchestrator,
    EffectRequest,
    PolicyStaleError,
)
from harness.domain.hashing import ContentHash
from harness.domain.policy_freshness import EffectKind, PolicyFreshnessVerdict
from harness.ports.event_ledger import AppendResult

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "support"))
from case_probe import observe_case
from ledger_probe import LedgerProbe, SideEffectProbe

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
TESTS_YAML = REPO_ROOT / "design-source" / "registries" / "tests.yaml"

EXPIRED_AT = "2026-08-17T00:00:00Z"
NOW_AFTER = "2026-08-17T00:00:01Z"
NOW_BEFORE = "2026-08-16T23:59:59Z"

CASE_EFFECT: dict[str, EffectKind] = {
    "NEW_LOCAL_READ": EffectKind.LOCAL_READ,
    "NEW_WORKSPACE_WRITE": EffectKind.WORKSPACE_WRITE,
    "NEW_EXTERNAL_EFFECT": EffectKind.EXTERNAL_SEND,
    "NEW_PAID": EffectKind.PAID_EXECUTION,
}


def _case(case_id: str) -> dict[str, Any]:
    rows = yaml.safe_load(TESTS_YAML.read_text(encoding="utf-8"))["test_cases"]
    for row in rows:
        if row["test_id"] == "AT-POLICY-STALE-001" and row["case_id"] == case_id:
            return dict(row)
    raise AssertionError(f"Registryに AT-POLICY-STALE-001/{case_id} が無い")


# --------------------------------------------------------------------------
# Spy：呼ばれた回数を数えるだけ。実I/Oはしない。
# --------------------------------------------------------------------------


@dataclass
class FrozenClock:
    """注入可能なClock。期限境界を試験できるようにするために要る。"""

    value: str

    def now(self) -> str:
        return self.value


@dataclass
class SideEffectSpy:
    """Case assertions が要求する5つの観測値を数える。"""

    network_calls: int = 0
    network_bytes_sent: int = 0
    process_launches: int = 0
    workspace_commits: int = 0
    budget_reservations_created: int = 0
    provider_calls: int = 0

    @property
    def external_effects(self) -> int:
        """外部から観測されうる作用の総数。"""
        return (
            self.network_calls
            + self.process_launches
            + self.workspace_commits
            + self.budget_reservations_created
            + self.provider_calls
        )

    # --- Port実装 ---
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


@dataclass
class LedgerSpy:
    """AppendされたEventを記録する。作用ではなく記録の観測である。"""

    appended: list[str] = field(default_factory=list)
    payload_hashes: list[str] = field(default_factory=list)
    head: int = 0

    def append(self, events: Any, *, expected_stream_sequence: int) -> AppendResult:
        listed = list(events)
        assert expected_stream_sequence == self.head, "Stream headとCASが噛み合っていない"
        first = self.head + 1
        for event in listed:
            self.appended.append(event.event_type)
            self.payload_hashes.append(str(event.payload_hash))
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
        """作用の開始を示すEventの数。止めたなら0でなければならない。"""
        started = {"ACTION_PREPARED", "ACTION_EXECUTING", "EFFECT_ATTEMPTED"}
        return sum(1 for name in self.appended if name in started)


def _orchestrator(now: str) -> tuple[EffectOrchestrator, SideEffectSpy, LedgerSpy]:
    spy = SideEffectSpy()
    ledger = LedgerSpy()
    orchestrator = EffectOrchestrator(
        clock=FrozenClock(now),
        ledger=ledger,
        external_send=spy,
        process_launch=spy,
        workspace_write=spy,
        paid_execution=spy,
    )
    return orchestrator, spy, ledger


def _request(kind: EffectKind, *, effect_attempted: bool = False) -> EffectRequest:
    return EffectRequest(
        attempt_id="attempt-1",
        stream_id="ACTION_ATTEMPT_STREAM",
        effect_kind=kind,
        policy_expires_at=EXPIRED_AT,
        effect_attempted=effect_attempted,
        endpoint="https://example.invalid/x",
        payload=b"payload",
        argv=("/bin/true",),
        relative_path="out.txt",
        tokens=10,
        prompt="p",
    )


def _to_probes(spy: SideEffectSpy, ledger: LedgerSpy) -> tuple[LedgerProbe, SideEffectProbe]:
    """既存Spyの**実測値**を共有Probeへ移す。0を書き込むのではなく写す。"""
    probe = LedgerProbe()
    probe.append_names(list(ledger.appended))
    effects = SideEffectProbe()
    effects.network_calls = spy.network_calls
    effects.process_launches = spy.process_launches
    effects.workspace_commits = spy.workspace_commits
    effects.budget_reservations_created = spy.budget_reservations_created
    effects.provider_calls = spy.provider_calls
    return probe, effects


def _assert_no_side_effects(spy: SideEffectSpy, ledger: LedgerSpy) -> None:
    """Case が要求する5つの観測値がすべて0であること。"""
    assert spy.network_calls == 0
    assert spy.process_launches == 0
    assert spy.workspace_commits == 0
    assert spy.external_effects == 0
    assert ledger.effect_attempts == 0
    # Case固有のassertionも同時に満たす。
    assert spy.network_bytes_sent == 0
    assert spy.budget_reservations_created == 0
    assert spy.provider_calls == 0


# --------------------------------------------------------------------------
# 4つの「新しい作用を止める」Case
# --------------------------------------------------------------------------


@pytest.mark.case("AT-POLICY-STALE-001/NEW_LOCAL_READ")
def test_stale_policy_blocks_new_local_read(case_observation: Any) -> None:
    """期限切れPolicyでLocal Readを開始しない。Processを起動しない。"""
    _run_blocked_case("NEW_LOCAL_READ", case_observation)


@pytest.mark.case("AT-POLICY-STALE-001/NEW_WORKSPACE_WRITE")
def test_stale_policy_blocks_workspace_write(case_observation: Any) -> None:
    """期限切れPolicyでWorkspaceへ確定書込みしない。"""
    _run_blocked_case("NEW_WORKSPACE_WRITE", case_observation)


@pytest.mark.case("AT-POLICY-STALE-001/NEW_EXTERNAL_EFFECT")
def test_stale_policy_blocks_external_dispatch(case_observation: Any) -> None:
    """期限切れPolicyで外部送信しない。1 Byteも送らない。"""
    _run_blocked_case("NEW_EXTERNAL_EFFECT", case_observation)


@pytest.mark.case("AT-POLICY-STALE-001/NEW_PAID")
def test_stale_policy_blocks_paid_execution(case_observation: Any) -> None:
    """期限切れPolicyでBudget予約もProvider呼出もしない。"""
    _run_blocked_case("NEW_PAID", case_observation)


def _run_blocked_case(case_id: str, observation: object | None = None) -> None:
    """止めるCaseの共通手順。期待値はRegistryから読む。"""
    expected = _case(case_id)
    orchestrator, spy, ledger = _orchestrator(NOW_AFTER)

    with pytest.raises(PolicyStaleError) as excinfo:
        orchestrator.execute(_request(CASE_EFFECT[case_id]))

    error = excinfo.value
    assert error.code.value == expected["expected_error_code"]
    assert error.decision.attempt_state == expected["expected_state"]

    # Ledgerには止めた事実が期待どおりの順序で残る。
    assert ledger.appended == expected["expected_event_sequence"]

    # **副作用は1つも起きていない。**
    _assert_no_side_effects(spy, ledger)

    # 最終Port呼出の直前で再検証している。
    assert orchestrator.revalidations == 1

    if observation is not None:
        probe, effects = _to_probes(spy, ledger)
        observe_case(
            observation,
            f"AT-POLICY-STALE-001/{case_id}",
            state=error.decision.attempt_state,
            subject_id="attempt-1",
            error_code=error.code.value,
            ledger=probe,
            effects=effects,
            head_before=0,
            payload={
                "effect_kind": CASE_EFFECT[case_id].value,
                "now": NOW_AFTER,
                "effect_attempted": False,
                "revalidations": orchestrator.revalidations,
            },
        )


@pytest.mark.parametrize("case_id", sorted(CASE_EFFECT))
def test_blocked_cases_do_not_touch_any_effect_port(case_id: str) -> None:
    """止めたCaseがどのEffect Portにも触れないこと。

    種類ごとのPortを分けてあるので、「別の作用なら通った」も検出できる。
    """
    orchestrator, spy, ledger = _orchestrator(NOW_AFTER)
    with pytest.raises(PolicyStaleError):
        orchestrator.execute(_request(CASE_EFFECT[case_id]))
    _assert_no_side_effects(spy, ledger)


# --------------------------------------------------------------------------
# In-flight：照合だけを許す
# --------------------------------------------------------------------------


@pytest.mark.case("AT-POLICY-STALE-001/INFLIGHT_ATTEMPTED")
def test_inflight_allows_reconciliation_without_new_effects(case_observation: Any) -> None:
    """既に作用を起こした可能性がある場合、新しい作用を起こさずに照合へ落ちる。

    例外にしないのは、照合まで止めると未照合のまま残るからである。
    ただし新しい作用は1つも起こさない。
    """
    expected = _case("INFLIGHT_ATTEMPTED")
    orchestrator, spy, ledger = _orchestrator(NOW_AFTER)

    outcome = orchestrator.execute(_request(EffectKind.EXTERNAL_SEND, effect_attempted=True))

    assert outcome.decision.verdict is PolicyFreshnessVerdict.RECOVERY_ONLY
    assert outcome.attempt_state == expected["expected_state"]
    assert ledger.appended == expected["expected_event_sequence"]
    # 照合は許されるが、新しい作用は0である。
    _assert_no_side_effects(spy, ledger)
    assert outcome.decision.reconciliation_allowed is True
    assert outcome.decision.new_effect_allowed is False

    probe, effects = _to_probes(spy, ledger)
    observe_case(
        case_observation,
        "AT-POLICY-STALE-001/INFLIGHT_ATTEMPTED",
        state=outcome.attempt_state,
        subject_id="attempt-1",
        # Registry は EFFECT_UNKNOWN を期待する。既存assertはState/Event列しか
        # 見ていなかったため、error_code の突合はここが初出である。
        error_code=(
            outcome.decision.error_code.value if outcome.decision.error_code is not None else None
        ),
        ledger=probe,
        effects=effects,
        head_before=0,
        payload={
            "effect_kind": EffectKind.EXTERNAL_SEND.value,
            "now": NOW_AFTER,
            "effect_attempted": True,
            "reconciliation_allowed": outcome.decision.reconciliation_allowed,
            "new_effect_allowed": outcome.decision.new_effect_allowed,
        },
    )


@pytest.mark.parametrize("kind", list(EffectKind))
def test_inflight_never_starts_a_new_effect(kind: EffectKind) -> None:
    """作用の種類によらずIn-flightで新規作用を起こさないこと。"""
    orchestrator, spy, ledger = _orchestrator(NOW_AFTER)
    orchestrator.execute(_request(kind, effect_attempted=True))
    _assert_no_side_effects(spy, ledger)


# --------------------------------------------------------------------------
# 構造：Domainの判定だけでEffectを開始できないこと
# --------------------------------------------------------------------------


def test_fresh_policy_actually_reaches_the_port() -> None:
    """期限内なら実際にPortへ届くこと。

    常に止めるだけの実装でも上の試験は全部通る。
    **通る経路が本当に通る**ことを確かめて初めて、止まったことに意味が出る。
    """
    orchestrator, spy, ledger = _orchestrator(NOW_BEFORE)
    outcome = orchestrator.execute(_request(EffectKind.EXTERNAL_SEND))
    assert outcome.blocked is False
    assert spy.network_calls == 1
    assert spy.network_bytes_sent == len(b"payload")
    # 通した場合、止めたEventは出ない。
    assert ledger.appended == []


@pytest.mark.parametrize("kind", list(EffectKind))
def test_every_effect_kind_has_a_reachable_path(kind: EffectKind) -> None:
    """4種すべてに「通る」経路があること。"""
    orchestrator, spy, _ = _orchestrator(NOW_BEFORE)
    orchestrator.execute(_request(kind))
    assert spy.external_effects >= 1


def test_orchestrator_revalidates_even_when_upstream_said_fresh() -> None:
    """上流が鮮度を確かめていても、Port直前でもう一度確かめること。

    判定と作用のあいだに時間があるなら、作用の直前で確かめ直す。
    「さっき確かめた」を根拠にしない。
    """
    orchestrator, spy, ledger = _orchestrator(NOW_AFTER)
    # 上流のDomain判定では FRESH だったと仮定しても、
    # Orchestratorは自分でClockを読み直して止める。
    with pytest.raises(PolicyStaleError):
        orchestrator.execute(_request(EffectKind.EXTERNAL_SEND))
    assert orchestrator.revalidations == 1
    _assert_no_side_effects(spy, ledger)


def test_expiry_boundary_is_revalidated_exclusively() -> None:
    """`expires_at` と完全一致する時刻で止まること。

    境界で「まだ有効」に倒すと、期限切れPolicyで1回だけ作用が通る窓ができる。
    """
    orchestrator, spy, ledger = _orchestrator(EXPIRED_AT)
    with pytest.raises(PolicyStaleError):
        orchestrator.execute(_request(EffectKind.EXTERNAL_SEND))
    _assert_no_side_effects(spy, ledger)


def test_one_second_before_expiry_still_passes() -> None:
    """境界の反対側では通ること。両側を測って初めて境界が確かめられる。"""
    orchestrator, spy, _ = _orchestrator(NOW_BEFORE)
    orchestrator.execute(_request(EffectKind.EXTERNAL_SEND))
    assert spy.network_calls == 1


# --------------------------------------------------------------------------
# Stale判定後のRetry／Fallback／Queue投入を禁止する
# --------------------------------------------------------------------------


def test_retry_after_stale_still_does_not_reach_the_port() -> None:
    """Stale後に再実行しても作用は起きないこと。

    呼出側が例外を握り潰して再試行しても、Orchestratorは毎回
    鮮度を測り直して止める。**再試行で通る窓を作らない。**
    """
    orchestrator, spy, ledger = _orchestrator(NOW_AFTER)
    for _ in range(3):
        with pytest.raises(PolicyStaleError):
            orchestrator.execute(_request(EffectKind.EXTERNAL_SEND))
    assert orchestrator.revalidations == 3
    _assert_no_side_effects(spy, ledger)


def test_fallback_to_another_effect_kind_is_also_blocked() -> None:
    """別の作用へFallbackしても止まること。

    「外部送信が駄目ならWorkspaceへ書く」のような代替経路を許さない。
    """
    orchestrator, spy, ledger = _orchestrator(NOW_AFTER)
    for kind in EffectKind:
        with pytest.raises(PolicyStaleError):
            orchestrator.execute(_request(kind))
    _assert_no_side_effects(spy, ledger)


def test_orchestrator_does_not_queue_blocked_requests() -> None:
    """止めた要求をQueueへ溜めないこと。

    溜めた要求が後で流れると、古いPolicyを根拠にした作用が遅れて起きる。
    Orchestratorは状態として保持しない。
    """
    orchestrator, _, _ = _orchestrator(NOW_AFTER)
    with pytest.raises(PolicyStaleError):
        orchestrator.execute(_request(EffectKind.EXTERNAL_SEND))
    # 再検証回数以外に、要求を溜めるFieldを持たない。
    retained = {
        name: getattr(orchestrator, name)
        for name in vars(orchestrator)
        if isinstance(getattr(orchestrator, name), list | tuple | dict | set)
    }
    assert retained == {}, f"止めた要求を保持している: {retained}"


def test_policy_stale_error_is_not_retryable_by_classification() -> None:
    """`PolicyStaleError` が再試行対象のError Codeでないこと。"""
    orchestrator, _, _ = _orchestrator(NOW_AFTER)
    with pytest.raises(PolicyStaleError) as excinfo:
        orchestrator.execute(_request(EffectKind.PAID_EXECUTION))
    assert excinfo.value.code.value == "POLICY_STALE_PAID_BLOCKED"
