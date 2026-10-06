"""§27 Backup/Restore と §3.10 Drain の本番経路。

## 判定器を持たない

Drain 可否は `domain.operations_safety.evaluate_deployment()`、Restore 可否は
`domain.backup_restore.evaluate_backup_restore()` が決める。本Serviceは
**実Storeを数え、作用経路を遮断し、観測値をそれらへ渡す**ことだけを行う。
判定を写し取って別の場所で再実装しない。

## 0 を代入しない

本番Facadeが注入する同一DBの受付制御は、復元区間の通常の受付・作用を拒否する。
`effects_during_restore` はその拒否カウンタの差分と、合成ProbeのGuard呼出しを数える。
拒否された試行数であり、実行できた作用数とは区別する。

## 期限超過は安全側で止める

Drain 待機が期限を超えたら、`DEPLOY_DRAIN_REQUIRED` のまま止める。
待ちきれなかったことを「落ち着いた」と読み替えない（不変条件#6）。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field, replace

from harness.domain.backup_restore import (
    BackupRestoreResult,
    LedgerChainSnapshot,
    evaluate_backup_restore,
)
from harness.domain.hashing import ContentHash
from harness.domain.operations_safety import DeploymentVerdict, evaluate_deployment
from harness.ports.effect_execution import ClockPort
from harness.ports.operation_control import OperationAdmissionPort
from harness.ports.operations import (
    BackupArtifact,
    DrainInspectionPort,
    DrainSnapshot,
    EffectAttempt,
    RestoreTargetPort,
)

__all__ = [
    "BackupRestoreService",
    "DrainOutcome",
    "RestoreEffectBlocked",
    "RestoreGuard",
    "RestoreOutcome",
]


class RestoreEffectBlocked(RuntimeError):
    """Restore 区間で作用を起こそうとした。**遮断した事実を例外でも示す。**"""


class _RestoreVerificationRejected(RuntimeError):
    """検証不合格を復元Contextへ伝え、停止状態を解除させない内部通知。"""


@dataclass(slots=True)
class RestoreGuard:
    """Restore 区間の作用遮断。呼ばれた回数を**実測**する。

    `ExternalSendPort` / `ProcessLaunchPort` / `WorkspaceWritePort` /
    `PaidExecutionPort` の形をそのまま満たすので、Production の呼出し側を
    書き換えずに差し込める。どれも**拒否したうえで記録**する。
    """

    attempts: list[EffectAttempt] = field(default_factory=list)

    @property
    def effect_count(self) -> int:
        return len(self.attempts)

    def _refuse(self, port: str, operation: str, detail: str) -> None:
        self.attempts.append(EffectAttempt(port=port, operation=operation, detail=detail))
        raise RestoreEffectBlocked(
            f"EFFECT_DURING_RESTORE_BLOCKED: {port}.{operation} は Restore 区間では実行しない"
        )

    # --- ExternalSendPort -------------------------------------------------
    def send(self, endpoint: str, payload: bytes) -> int:
        self._refuse("ExternalSendPort", "send", f"endpoint={endpoint} bytes={len(payload)}")
        raise AssertionError("unreachable")

    # --- ProcessLaunchPort ------------------------------------------------
    def launch(self, argv: list[str]) -> int:
        self._refuse("ProcessLaunchPort", "launch", f"argv0={argv[0] if argv else ''}")
        raise AssertionError("unreachable")

    # --- WorkspaceWritePort -----------------------------------------------
    def commit(self, relative_path: str, payload: bytes) -> str:
        self._refuse("WorkspaceWritePort", "commit", f"path={relative_path} bytes={len(payload)}")
        raise AssertionError("unreachable")

    # --- PaidExecutionPort ------------------------------------------------
    def reserve_budget(self, tokens: int) -> str:
        self._refuse("PaidExecutionPort", "reserve_budget", f"tokens={tokens}")
        raise AssertionError("unreachable")

    def invoke_provider(self, reservation_id: str, prompt: str) -> str:
        # prompt の中身は記録しない。Secret が混ざる経路を Evidence へ出さない（不変条件#7）。
        self._refuse(
            "PaidExecutionPort",
            "invoke_provider",
            f"reservation={reservation_id} prompt_len={len(prompt)}",
        )
        raise AssertionError("unreachable")


@dataclass(frozen=True, slots=True)
class DrainOutcome:
    """Drain 判定の結果と、判定に使った観測値。"""

    verdict: DeploymentVerdict
    snapshot: DrainSnapshot
    intake_stopped: bool
    #: 期限超過で打ち切ったか。超過したなら合格にしない。
    deadline_exceeded: bool
    waited_polls: int
    #: 受付入口が実際に拒否する状態か。**Service内のBool値ではなく入口側の状態。**
    intake_refuses_new_requests: bool = False
    intake_state_source: str = ""


@dataclass(frozen=True, slots=True)
class RestoreOutcome:
    """Restore 判定の結果と、遮断した作用の記録。"""

    result: BackupRestoreResult
    guard_attempts: tuple[EffectAttempt, ...]
    restored_chain_heads: tuple[tuple[str, str], ...]
    restored_artifact_ids: tuple[str, ...]

    @property
    def effects_during_restore(self) -> int:
        return len(self.guard_attempts)


def _snapshot(heads: tuple[tuple[str, str], ...]) -> LedgerChainSnapshot:
    """Chain Head 一覧を Domain の比較対象へ写す。

    Domain は `(sequence, event_hash, previous_hash)` を端から辿る。ここでは
    Stream ごとの Head を安定順で並べ、連結の検査は Adapter 側の
    `verify_chain` が担う。**Head だけを比べて済ませない。**
    """
    entries: list[tuple[int, ContentHash, ContentHash | None]] = []
    previous: ContentHash | None = None
    for index, (_stream, head) in enumerate(sorted(heads), start=1):
        current = ContentHash.parse(head)
        entries.append((index, current, previous))
        previous = current
    return LedgerChainSnapshot(entries=tuple(entries))


class BackupRestoreService:
    """Operator 入口から呼ばれる本番 Service。"""

    def __init__(
        self,
        *,
        drain: DrainInspectionPort,
        target: RestoreTargetPort,
        clock: ClockPort,
        intake: OperationAdmissionPort | None = None,
        drain_max_polls: int = 5,
    ) -> None:
        self._drain = drain
        self._target = target
        self._clock = clock
        self._intake = intake
        self._drain_max_polls = drain_max_polls

    # ----------------------------------------------------------------- Drain
    def drain_check(self, *, deployment_result_id: str, stop_intake: bool = True) -> DrainOutcome:
        """新規受付を止め、進行中が捌けるまで待ち、Deploy 可否を判定する。

        **待ちきれなかったら合格にしない。** 期限超過は
        `DEPLOY_DRAIN_REQUIRED` のまま返す。
        """
        if stop_intake:
            self._drain.stop_intake()
            if self._intake is not None:
                # **受付入口から見える場所へ停止を書く。** Service内のBool値だけだと
                # 入口はそれを見ずに受け付け続ける。
                self._intake.stop()

        polls = 0
        snapshot = self._drain.inspect()
        while not snapshot.quiesced and polls < self._drain_max_polls:
            polls += 1
            snapshot = self._drain.inspect()

        if self._intake is not None:
            admitted = self._intake.active_count()
            snapshot = replace(
                snapshot,
                active_run_count=snapshot.active_run_count + admitted,
                active_states=(*snapshot.active_states, ("ADMITTED", admitted))
                if admitted
                else snapshot.active_states,
            )
        deadline_exceeded = not snapshot.quiesced
        verdict = evaluate_deployment(
            deployment_result_id=deployment_result_id,
            active_run_count=snapshot.active_run_count,
            pending_approval_count=snapshot.pending_approval_count,
        )
        return DrainOutcome(
            verdict=verdict,
            snapshot=snapshot,
            intake_stopped=self._drain.intake_stopped(),
            deadline_exceeded=deadline_exceeded,
            waited_polls=polls,
            intake_refuses_new_requests=self._intake is not None and stop_intake,
            intake_state_source=snapshot.source + "#operation_control",
        )

    # --------------------------------------------------------------- Restore
    def restore_and_verify(
        self,
        *,
        backup: BackupArtifact,
        backup_restore_id: str,
        during_restore: Callable[[RestoreGuard], None] | None = None,
    ) -> RestoreOutcome:
        """Backup から復元し、Chain と Manifest を読み直して判定する。

        本番Facadeは同じDBのOperationAdmissionPortを必ず注入する。
        RESTORING状態は通常の受付・Workbench生成/適用・Effect入口を拒否する。
        during_restoreはDBコピー後、CASコピー前の同期点であり、保護を作らない。
        RestoreGuardを呼ぶ従来のProbeは部品試験として残す。
        任意Nativeコードや基盤外の直接I/Oを封じるOS sandboxとは主張しない。
        """
        guard = RestoreGuard()
        window = (
            self._intake.restoration(purpose="VERIFY_COPY")
            if self._intake is not None
            else nullcontext()
        )

        def checkpoint() -> None:
            if during_restore is not None:
                try:
                    during_restore(guard)
                except RestoreEffectBlocked:
                    pass

        try:
            with window:
                # 保護はDB/CAS復元より前に有効。Checkpointも復元処理の内側で呼ぶ。
                before_refusals = self._intake.refusals() if self._intake is not None else 0
                self._target.restore(backup, checkpoint=checkpoint)
                restored_heads = self._target.chain_heads()
                restored_ids = self._target.artifact_ids()
                refusals = (
                    self._intake.refusals() - before_refusals if self._intake is not None else 0
                )
                for _ in range(refusals):
                    guard.attempts.append(
                        EffectAttempt(
                            "OperationAdmissionPort", "admission", "refused during restore"
                        )
                    )
                result = evaluate_backup_restore(
                    backup_restore_id=backup_restore_id,
                    original=_snapshot(backup.source_chain_heads),
                    restored=_snapshot(restored_heads),
                    original_manifest_ids=list(backup.artifact_ids),
                    restored_manifest_ids=list(restored_ids),
                    effects_during_restore=guard.effect_count,
                )
                if not result.accepted:
                    raise _RestoreVerificationRejected()
        except _RestoreVerificationRejected:
            # 判定値は返すが、復元ContextはRESTORINGを保持する。
            pass
        return RestoreOutcome(
            result=result,
            guard_attempts=tuple(guard.attempts),
            restored_chain_heads=restored_heads,
            restored_artifact_ids=restored_ids,
        )
