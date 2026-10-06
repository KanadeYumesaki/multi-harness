"""§1.16.2.2 Approval／Plan Orchestrator。Ledger責務はここだけが持つ。

## 委任経路のEvent列を流用しない

`harness.domain.delegation` も `APPROVAL_ISSUED` / `APPROVAL_CONSUMED` /
`ACTION_BLOCKED` を組む。しかしその列は先頭が `DELEGATION_MATCHED` または
`POLICY_DECIDED` であり、**委任が成立した事実**を含む。委任していない実行の
記録として使うと、監査上は委任の記録になる。

同じEvent名を含むことは同じ列であることを意味しない（§19.1.1）。本Moduleは
delegationを一切呼ばず、素のApproval経路の列を自分で組む。

## Subject境界

* `APPROVAL_GRANT` … Grantそのものの状態。消費が成立した事実を主張する
* `ACTION_ATTEMPT` … 実行Attemptの状態。**止まった事実**を主張する

Grantの状態をAttemptの状態として扱わない。Approvalの検証に失敗したときは、
Grantを消費していない事実（`grant_consumed == false`）とAttemptが
`BLOCKED_APPROVAL`で止まった事実を別々に持つ。

## Planner結果を実行結果にしない

Plan Content Hashの一致確認は行うが、Plannerの比較結果そのものを実行結果へ
変換しない。Plan比較だけを主張するCase（`AT-PLAN-DETERMINISM-001/SAME_INPUT`）は
Unit層であり本Moduleの対象ではない（Owner Decision 2-B）。

## Transaction境界

Application層がTransactionを所有する。RepositoryはCommitしない（不変条件#15）。
State変更・Event Append・Ledger Head更新を同一Transactionで扱う。検証に失敗した
場合はRollbackし、Partial Eventを残さない。

## 止まった経路のEvent列は経路ごとに違う

同じ`ACTION_BLOCKED`で終わっても、そこへ至る列は経路ごとに違う。列は
**どこまで進んだか**を表すからである。Planを解決してからPolicyで弾かれたのと、
Planがそもそも決定的に組めなかったのとでは、残すべき記録が違う。

| 経路 | 進捗Event | 止まるState |
|---|---|---|
| Plan整合検証 | `PLAN_RESOLVED`／`POLICY_DECIDED` | `BLOCKED_POLICY` |
| Plan決定性検証 | なし。Planが組めていない | `BLOCKED_CONFLICT` |
| Fencing再検証 | なし。Effect直前で弾く | `BLOCKED_CONFLICT` |
| Artifact受入検証 | `INPUT_ARTIFACT_CLASSIFIED`／`POLICY_DECIDED` | `BLOCKED_POLICY` |

止まるStateはError Codeの`Classification`からは決まらない。`RUNTIME_SPEC_MISMATCH`と
`PLAN_NONDETERMINISTIC`はどちらも`VALIDATION_ERROR`だが、前者は`BLOCKED_POLICY`、
後者は`BLOCKED_CONFLICT`で止まる。**経路の契約として持つ。** 正本はRegistryの
`expected_state`である。

拒否をどのEventで記録するかは`_REJECTION_EVENT`が持つ。Test側が渡すのではない。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from harness.domain.approval import ApprovalGrant
from harness.domain.attempt import ActionAttempt
from harness.domain.errors import ErrorCode, HarnessError, classification_of
from harness.domain.events import EventType
from harness.domain.hashing import ContentHash, hash_canonical
from harness.domain.lease import Lease
from harness.domain.plan import ExecutionPlan, PlanAuthority, PlanBuildInput, build_execution_plan
from harness.ports.approval import ApprovalConsumeRequest, ApprovalGrantPort
from harness.ports.effect_execution import ClockPort
from harness.ports.event_ledger import EventLedgerPort, NewEvent
from harness.ports.lease import LeasePort
from harness.ports.unit_of_work import UnitOfWorkPort

__all__ = [
    "ApprovalOutcome",
    "ApprovalPlanOrchestrator",
    "ArtifactAdmissionPort",
    "ArtifactAdmissionRequest",
    "AttemptGuardOutcome",
    "FencedEffectRequest",
    "PlanDeterminismRequest",
    "PlanIntegrityRequest",
    "PolicyGuardPort",
    "PolicyGuardRequest",
]

_EVENT_ARTIFACT_TYPE = "approval-plan-event"
_EVENT_SCHEMA_MAJOR = 1


class PolicyGuardPort(Protocol):
    """Effect実行前のPolicy検査。拒否は`HarnessError`で表す。

    通過した場合は何も返さない。**Guard自身はLedgerへ触らない。**
    """

    def check(self) -> None: ...


class EffectPort(Protocol):
    """Guardを通過したときだけ呼ばれる実Effect。"""

    def run(self) -> object: ...


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    """`APPROVAL_GRANT`名前空間のSubject。"""

    grant_id: str
    state: str
    error_code: ErrorCode | None
    events: tuple[EventType, ...]
    ledger_head_before: int
    ledger_head_after: int
    concurrency_group: str
    successful_consumes: int
    grant_consumed: bool
    attempt: ActionAttempt | None
    attempt_state: str | None


@dataclass(frozen=True, slots=True)
class AttemptGuardOutcome:
    """`ACTION_ATTEMPT`名前空間のSubject。"""

    attempt_id: str
    state: str
    error_code: ErrorCode | None
    events: tuple[EventType, ...]
    ledger_head_before: int
    ledger_head_after: int
    attempt: ActionAttempt
    effect_invocations: int

    @property
    def attempt_state(self) -> str:
        return self.attempt.state


@dataclass(frozen=True, slots=True)
class PolicyGuardRequest:
    """Effect実行前のGuardをまとめて走らせる要求。"""

    stream_id: str
    attempt: ActionAttempt
    guards: tuple[PolicyGuardPort, ...]
    ended_at: str


#: どの拒否をどのEventで記録するか。**Production側の決めごとである。**
#: 対応が無い拒否は、直前の判定Event（`POLICY_DECIDED`など）で足りるものである。
_REJECTION_EVENT: dict[ErrorCode, EventType] = {
    ErrorCode.APPROVAL_REPLAY: EventType.APPROVAL_REPLAY_DENIED,
    ErrorCode.STALE_FENCING_TOKEN: EventType.FENCING_REJECTED,
    ErrorCode.RUNTIME_SPEC_MISMATCH: EventType.RUNTIME_SPEC_MISMATCH,
    ErrorCode.PLAN_NONDETERMINISTIC: EventType.PLAN_NONDETERMINISTIC,
}


class ArtifactAdmissionPort(Protocol):
    """取り込もうとしているArtifactの受入検査。拒否は`HarnessError`で表す。

    分類そのものは呼出側が済ませてある。ここは**Control面の役割を騙っていないか**を
    見る（§3.6）。Port自身はLedgerへ触らない。
    """

    def require_admissible(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PlanIntegrityRequest:
    """保存済みPlanを解決し、Content／Authority Hashを照合する。"""

    stream_id: str
    attempt: ActionAttempt
    plan: ExecutionPlan
    ended_at: str


@dataclass(frozen=True, slots=True)
class PlanDeterminismRequest:
    """凍結済みInputから組み直し、非決定性を検出する。"""

    stream_id: str
    attempt: ActionAttempt
    build_input: PlanBuildInput
    authority: PlanAuthority
    ended_at: str


@dataclass(frozen=True, slots=True)
class FencedEffectRequest:
    """Effect直前でFencing Tokenを再検証する（不変条件#3）。

    再検証はLeaseのCASで行う。`ActionAttempt`側の`fencing_token is None`分岐は
    正規の遷移からは到達しない（`RUNNING`へ至る道が`with_lease`を通るため）。
    古いWorkerが止まる実体は、新しいWorkerがLeaseを取り直したことによる
    Fencing Tokenの単調増加である。
    """

    stream_id: str
    attempt: ActionAttempt
    leases: LeasePort
    lease: Lease
    now: str
    expires_at: str
    ended_at: str


@dataclass(frozen=True, slots=True)
class ArtifactAdmissionRequest:
    """Artifactを分類し、Control面の役割を騙っていないかを見る。"""

    stream_id: str
    attempt: ActionAttempt
    artifact: ArtifactAdmissionPort
    ended_at: str


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """Grantを発行してから消費するまでの1往復。"""

    stream_id: str
    grant: ApprovalGrant
    consume: ApprovalConsumeRequest
    concurrency_group: str
    attempt: ActionAttempt | None = None
    ended_at: str | None = None


class ApprovalPlanOrchestrator:
    """Approval消費とPolicy Guardを、Ledger記録と同一Transactionで束ねる。"""

    def __init__(
        self,
        *,
        grants: ApprovalGrantPort,
        ledger: EventLedgerPort,
        unit_of_work: UnitOfWorkPort,
        clock: ClockPort,
        effect: EffectPort | None = None,
    ) -> None:
        self._grants = grants
        self._ledger = ledger
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._effect = effect

    # ------------------------------------------------------------------
    # Approval
    # ------------------------------------------------------------------

    def issue_and_consume(self, request: ApprovalRequest) -> ApprovalOutcome:
        """Grantを発行し、続けて消費する。

        消費に失敗した場合、Grantは消費されていない。Attemptを渡してあれば
        `BLOCKED_APPROVAL`で止め、`ACTION_BLOCKED`を残す。
        """
        head_before = self._ledger.stream_head(request.stream_id)

        # --- 発行。ここまでは成立している ---------------------------------
        with self._unit_of_work.begin_immediate():
            self._grants.issue(request.grant)
            self._append(request.stream_id, EventType.APPROVAL_ISSUED, head_before)
        head = head_before + 1

        # --- 消費。失敗すればGrantは消費されない --------------------------
        try:
            with self._unit_of_work.begin_immediate():
                consumed = self._grants.consume(request.consume)
                self._append(request.stream_id, EventType.APPROVAL_CONSUMED, head)
        except HarnessError as error:
            return self._blocked_approval(request, error, head_before, head)

        return ApprovalOutcome(
            grant_id=consumed.grant_id,
            state=consumed.status.value,
            error_code=None,
            events=(EventType.APPROVAL_ISSUED, EventType.APPROVAL_CONSUMED),
            ledger_head_before=head_before,
            ledger_head_after=self._ledger.stream_head(request.stream_id),
            concurrency_group=request.concurrency_group,
            successful_consumes=1,
            grant_consumed=True,
            attempt=request.attempt,
            attempt_state=request.attempt.state if request.attempt else None,
        )

    def consume_only(self, request: ApprovalRequest) -> ApprovalOutcome:
        """既に発行済みのGrantを消費する。並行消費の敗者を測るために使う。

        Grantを再発行しない。`APPROVAL_ISSUED`もAppendしない。
        """
        head_before = self._ledger.stream_head(request.stream_id)
        try:
            with self._unit_of_work.begin_immediate():
                consumed = self._grants.consume(request.consume)
                self._append(request.stream_id, EventType.APPROVAL_CONSUMED, head_before)
        except HarnessError as error:
            return self._denied_consume(request, error, head_before)
        return ApprovalOutcome(
            grant_id=consumed.grant_id,
            state=consumed.status.value,
            error_code=None,
            events=(EventType.APPROVAL_CONSUMED,),
            ledger_head_before=head_before,
            ledger_head_after=self._ledger.stream_head(request.stream_id),
            concurrency_group=request.concurrency_group,
            successful_consumes=1,
            grant_consumed=True,
            attempt=request.attempt,
            attempt_state=request.attempt.state if request.attempt else None,
        )

    def _denied_consume(
        self,
        request: ApprovalRequest,
        error: HarnessError,
        head_before: int,
    ) -> ApprovalOutcome:
        """消費が拒否された。Grantは消費されていない。

        Attemptを渡していない場合は、Grantを取れなかっただけである。実行が
        止まったわけではないのでEventを残さない（並行消費の敗者がこれに当たる）。

        Attemptを渡した場合は**実行が止まった**。止まった事実がLedgerに残らな
        ければ、後から読んで何が起きたか分からない。拒否Eventと`ACTION_BLOCKED`を
        残し、Attemptを`BLOCKED_APPROVAL`へ落とす。
        """
        attempt = request.attempt
        if attempt is None:
            return ApprovalOutcome(
                grant_id=request.consume.grant_id,
                state="REJECTED",
                error_code=error.code,
                events=(),
                ledger_head_before=head_before,
                ledger_head_after=self._ledger.stream_head(request.stream_id),
                concurrency_group=request.concurrency_group,
                successful_consumes=0,
                grant_consumed=False,
                attempt=None,
                attempt_state=None,
            )

        rejection = _REJECTION_EVENT.get(error.code)
        events = (rejection, EventType.ACTION_BLOCKED) if rejection else (EventType.ACTION_BLOCKED,)
        head = head_before
        ended_at = request.ended_at or self._clock.now()
        with self._unit_of_work.begin_immediate():
            blocked = attempt.block(
                state="BLOCKED_APPROVAL",
                error_classification=classification_of(error.code).value,
                ended_at=ended_at,
            )
            for event in events:
                self._append(request.stream_id, event, head)
                head += 1
        return ApprovalOutcome(
            grant_id=request.consume.grant_id,
            state=blocked.state,
            error_code=error.code,
            events=events,
            ledger_head_before=head_before,
            ledger_head_after=self._ledger.stream_head(request.stream_id),
            concurrency_group=request.concurrency_group,
            successful_consumes=0,
            grant_consumed=False,
            attempt=blocked,
            attempt_state=blocked.state,
        )

    def _blocked_approval(
        self,
        request: ApprovalRequest,
        error: HarnessError,
        head_before: int,
        head: int,
    ) -> ApprovalOutcome:
        """Approval検証に失敗した。Grantは消費されていない。"""
        attempt = request.attempt
        events: tuple[EventType, ...] = (EventType.APPROVAL_ISSUED,)
        if attempt is not None:
            ended_at = request.ended_at or self._clock.now()
            with self._unit_of_work.begin_immediate():
                attempt = attempt.block(
                    state="BLOCKED_APPROVAL",
                    error_classification=classification_of(error.code).value,
                    ended_at=ended_at,
                )
                self._append(request.stream_id, EventType.ACTION_BLOCKED, head)
            events = (EventType.APPROVAL_ISSUED, EventType.ACTION_BLOCKED)
        return ApprovalOutcome(
            grant_id=request.consume.grant_id,
            state=attempt.state if attempt is not None else "REJECTED",
            error_code=error.code,
            events=events,
            ledger_head_before=head_before,
            ledger_head_after=self._ledger.stream_head(request.stream_id),
            concurrency_group=request.concurrency_group,
            successful_consumes=0,
            grant_consumed=False,
            attempt=attempt,
            attempt_state=attempt.state if attempt is not None else None,
        )

    # ------------------------------------------------------------------
    # Policy Guard
    # ------------------------------------------------------------------

    def run_guarded(self, request: PolicyGuardRequest) -> AttemptGuardOutcome:
        """Effect実行前のGuardを順に走らせる。

        最初に拒否したGuardでAttemptを`BLOCKED_POLICY`へ落とし、
        `ACTION_BLOCKED`をAppendする。**Effectは呼ばない。**
        Blocked AttemptをQueueやRetryへ流さない。
        """
        head_before = self._ledger.stream_head(request.stream_id)
        denial: HarnessError | None = None
        for guard in request.guards:
            try:
                guard.check()
            except HarnessError as error:
                denial = error
                break

        if denial is not None:
            with self._unit_of_work.begin_immediate():
                blocked = request.attempt.block(
                    state="BLOCKED_POLICY",
                    error_classification=classification_of(denial.code).value,
                    ended_at=request.ended_at,
                )
                self._append(request.stream_id, EventType.ACTION_BLOCKED, head_before)
            return AttemptGuardOutcome(
                attempt_id=blocked.attempt_id,
                state=blocked.state,
                error_code=denial.code,
                events=(EventType.ACTION_BLOCKED,),
                ledger_head_before=head_before,
                ledger_head_after=self._ledger.stream_head(request.stream_id),
                attempt=blocked,
                effect_invocations=0,
            )

        invocations = 0
        if self._effect is not None:
            # Guardを全部通ったときだけ実Effectを呼ぶ。
            self._effect.run()
            invocations = 1
        return AttemptGuardOutcome(
            attempt_id=request.attempt.attempt_id,
            state=request.attempt.state,
            error_code=None,
            events=(),
            ledger_head_before=head_before,
            ledger_head_after=self._ledger.stream_head(request.stream_id),
            attempt=request.attempt,
            effect_invocations=invocations,
        )

    # ------------------------------------------------------------------
    # Registry が宣言した停止経路
    # ------------------------------------------------------------------

    def resolve_and_verify_plan(self, request: PlanIntegrityRequest) -> AttemptGuardOutcome:
        """Planを解決し、Policyを判定してから整合を照合する。

        `PLAN_RESOLVED`と`POLICY_DECIDED`は**そこまで進んだ事実**である。
        照合に失敗した場合だけ、その拒否Eventと`ACTION_BLOCKED`が続く。
        """
        return self._guarded_transition(
            stream_id=request.stream_id,
            attempt=request.attempt,
            progress=(EventType.PLAN_RESOLVED, EventType.POLICY_DECIDED),
            blocked_state="BLOCKED_POLICY",
            ended_at=request.ended_at,
            check=request.plan.assert_integrity,
        )

    def verify_plan_determinism(self, request: PlanDeterminismRequest) -> AttemptGuardOutcome:
        """凍結済みInputから組み直し、同じPlanになることを確かめる。

        **進捗Eventを持たない。** 決定的に組めないPlanは解決していないので、
        `PLAN_RESOLVED`を残すと「解決した」という誤った記録になる。
        """

        def _check() -> None:
            build_execution_plan(request.build_input, request.authority)

        return self._guarded_transition(
            stream_id=request.stream_id,
            attempt=request.attempt,
            progress=(),
            blocked_state="BLOCKED_CONFLICT",
            ended_at=request.ended_at,
            check=_check,
        )

    def prepare_effect_with_fence(self, request: FencedEffectRequest) -> AttemptGuardOutcome:
        """Fencing Tokenを再検証してからEffectへ進む（不変条件#3）。

        Application層の検証だけで通さない。Leaseを更新することで、いま保持して
        いるTokenが現在Tokenであることを**Storeに確かめさせる**。取り直されて
        いれば`STALE_FENCING_TOKEN`でCASが落ちる。

        進捗Eventを持たない。Effect直前で弾くので、そこまでのEventは既に別の
        経路が残している。
        """

        def _check() -> None:
            with self._unit_of_work.begin_immediate():
                request.leases.renew(request.lease, now=request.now, expires_at=request.expires_at)

        return self._guarded_transition(
            stream_id=request.stream_id,
            attempt=request.attempt,
            progress=(),
            blocked_state="BLOCKED_CONFLICT",
            ended_at=request.ended_at,
            check=_check,
        )

    def classify_and_admit_artifact(self, request: ArtifactAdmissionRequest) -> AttemptGuardOutcome:
        """Artifactを分類し、Control面の役割を騙っていないかを見る。

        `CONTROL_DATA_ROLE_ESCALATION`に対応する拒否Eventは持たない。
        `POLICY_DECIDED`が「Policyが判定して弾いた」ことを表しており、
        そこへ別のEventを重ねると同じ事実を二度記録することになる。
        """
        return self._guarded_transition(
            stream_id=request.stream_id,
            attempt=request.attempt,
            progress=(EventType.INPUT_ARTIFACT_CLASSIFIED, EventType.POLICY_DECIDED),
            blocked_state="BLOCKED_POLICY",
            ended_at=request.ended_at,
            check=request.artifact.require_admissible,
        )

    def _guarded_transition(
        self,
        *,
        stream_id: str,
        attempt: ActionAttempt,
        progress: tuple[EventType, ...],
        blocked_state: str,
        ended_at: str,
        check: Callable[[], None],
    ) -> AttemptGuardOutcome:
        """進捗を残し、検出器を呼び、拒否されたらAttemptを止める。

        進捗Eventは**検出器を呼ぶ前に**Appendする。そこまで進んだのは事実であり、
        後段が失敗したからといって無かったことにはならない。

        検出器はDomainの実物である。呼出側から結果を受け取らない。
        例外は握り潰さない（不変条件#9）。
        """
        head_before = self._ledger.stream_head(stream_id)
        head = head_before
        with self._unit_of_work.begin_immediate():
            for event in progress:
                self._append(stream_id, event, head)
                head += 1

        try:
            check()
        except HarnessError as error:
            rejection = _REJECTION_EVENT.get(error.code)
            tail = (
                (rejection, EventType.ACTION_BLOCKED) if rejection else (EventType.ACTION_BLOCKED,)
            )
            with self._unit_of_work.begin_immediate():
                blocked = attempt.block(
                    state=blocked_state,
                    error_classification=classification_of(error.code).value,
                    ended_at=ended_at,
                )
                for event in tail:
                    self._append(stream_id, event, head)
                    head += 1
            return AttemptGuardOutcome(
                attempt_id=blocked.attempt_id,
                state=blocked.state,
                error_code=error.code,
                events=(*progress, *tail),
                ledger_head_before=head_before,
                ledger_head_after=self._ledger.stream_head(stream_id),
                attempt=blocked,
                effect_invocations=0,
            )

        return AttemptGuardOutcome(
            attempt_id=attempt.attempt_id,
            state=attempt.state,
            error_code=None,
            events=progress,
            ledger_head_before=head_before,
            ledger_head_after=self._ledger.stream_head(stream_id),
            attempt=attempt,
            effect_invocations=0,
        )

    # ------------------------------------------------------------------

    def _append(self, stream_id: str, event: EventType, expected_head: int) -> None:
        self._ledger.append(
            [
                NewEvent(
                    stream_id=stream_id,
                    event_type=event.value,
                    payload_hash=self._payload_hash(event),
                    recorded_at=self._clock.now(),
                )
            ],
            expected_stream_sequence=expected_head,
        )

    @staticmethod
    def _payload_hash(event: EventType) -> ContentHash:
        # 本文はLedgerへ置かない。Event種別だけをHashにする（§15.7）。
        return hash_canonical(
            {"event_type": event.value},
            artifact_type=_EVENT_ARTIFACT_TYPE,
            schema_major=_EVENT_SCHEMA_MAJOR,
        )
