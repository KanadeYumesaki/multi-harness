"""§3.10／§27 運用安全：Drain・GC・Emergency Recovery。

3つとも「止めるべきときに止まる」ための規則であり、共通する形がある。
**まだ確かめていないものを、確かめたことにして進めない。**

| 機能 | 止める理由 |
|---|---|
| Drain | 実行中Runがあるまま入れ替えると、途中の作用が宙に浮く |
| GC | 参照が無いことと「消してよい」ことは別である |
| Emergency Recovery | Policyを迂回する経路だからこそ、できることを狭く固定する |
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from harness.domain._registry_generated import ErrorCode, EventType

__all__ = [
    "ALLOWED_EMERGENCY_OPERATIONS",
    "DENIED_EMERGENCY_OPERATIONS",
    "ArtifactRecord",
    "DeploymentVerdict",
    "EmergencyRecoveryVerdict",
    "GarbageCollectionResult",
    "evaluate_deployment",
    "evaluate_emergency_recovery",
    "plan_garbage_collection",
]

_ACCEPTED: Final[str] = "ACCEPTED"
_REJECTED: Final[str] = "REJECTED"


# ==========================================================================
# Drain
# ==========================================================================


@dataclass(frozen=True, slots=True)
class DeploymentVerdict:
    """Deploy 前検査の結果。"""

    deployment_result_id: str
    state: str
    error_code: ErrorCode | None
    events: tuple[EventType, ...]
    active_run_count: int
    pending_approval_count: int
    #: Migrationを開始したか。拒否したなら必ず False。
    migration_started: bool
    #: Drain中に新しく起こした作用。常に0でなければならない。
    new_effects: int = 0

    @property
    def accepted(self) -> bool:
        return self.state == _ACCEPTED


def evaluate_deployment(
    *,
    deployment_result_id: str,
    active_run_count: int,
    pending_approval_count: int,
) -> DeploymentVerdict:
    """Deployしてよいかを判定する。

    **実行中Runが1件でもあれば拒否する。** 入れ替えてしまうと、途中まで
    進んだ作用がどのVersionの規則で動いていたのか確定しなくなる。

    承認待ちが残っている場合も拒否する。承認待ちは「人がまだ決めていない」
    状態であり、その決定を新しいVersionが引き継げる保証が無い。

    拒否したら **Migrationを開始しない。** 開始してから気付くと、
    DBだけ新Versionで実行中Runは旧Versionという状態になる。
    """
    if active_run_count < 0 or pending_approval_count < 0:
        raise ValueError("counts must not be negative")

    if active_run_count > 0 or pending_approval_count > 0:
        return DeploymentVerdict(
            deployment_result_id=deployment_result_id,
            state=_REJECTED,
            error_code=ErrorCode.DEPLOY_DRAIN_REQUIRED,
            events=(EventType.ACTION_BLOCKED,),
            active_run_count=active_run_count,
            pending_approval_count=pending_approval_count,
            # 拒否したのだからMigrationへ進まない。
            migration_started=False,
        )

    return DeploymentVerdict(
        deployment_result_id=deployment_result_id,
        state=_ACCEPTED,
        error_code=None,
        events=(),
        active_run_count=0,
        pending_approval_count=0,
        migration_started=True,
    )


# ==========================================================================
# GC
# ==========================================================================


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """GC判定の対象。"""

    artifact_id: str
    #: 参照しているManifest／Recordの数。
    reference_count: int
    age_seconds: int
    #: 生Payloadを保持しているか。
    payload_present: bool


@dataclass(frozen=True, slots=True)
class GarbageCollectionResult:
    """GC の結果。**Dry Runでは1件も消さない。**"""

    gc_result_id: str
    state: str
    orphan_candidate_ids: tuple[str, ...]
    deleted_ids: tuple[str, ...]
    referenced_kept_ids: tuple[str, ...]

    @property
    def orphan_candidate_count(self) -> int:
        return len(self.orphan_candidate_ids)

    @property
    def deleted_count(self) -> int:
        return len(self.deleted_ids)

    @property
    def referenced_deleted_count(self) -> int:
        """参照中なのに消したものの数。**常に0でなければならない。**"""
        return len(set(self.deleted_ids) & set(self.referenced_kept_ids))


def plan_garbage_collection(
    artifacts: Sequence[ArtifactRecord],
    *,
    gc_result_id: str,
    minimum_age_seconds: int,
    dry_run: bool = True,
) -> GarbageCollectionResult:
    """削除**候補**を挙げる。Dry Runでは削除しない。

    ## 孤立していることと、消してよいことは別である

    参照が0でも、それは「今この瞬間、参照が見つからない」でしかない。
    書込み途中かもしれないし、参照する側がまだ書かれていないかもしれない。

    P1 の `SQLITE_ENOSPC` はまさにその状態を作る。Artifactは CAS へ収まったが
    Journal のCommitが失敗したので誰も参照していない。**あれは正しい状態**であり、
    即削除する対象ではない。だから最小経過時間を要求し、既定はDry Runにする。

    候補に挙げることと消すことを分けておけば、
    「孤立しているから消した」で取り返しがつかなくなる経路が無い。
    """
    if minimum_age_seconds < 0:
        raise ValueError("minimum_age_seconds must not be negative")

    candidates: list[str] = []
    referenced: list[str] = []
    for artifact in artifacts:
        if artifact.reference_count > 0:
            referenced.append(artifact.artifact_id)
            continue
        if artifact.age_seconds >= minimum_age_seconds:
            candidates.append(artifact.artifact_id)
        # 若い孤立Artifactは候補にもしない。書込み途中かもしれない。

    # Dry Run では削除しない。実削除は別のOperator操作である。
    deleted: tuple[str, ...] = () if dry_run else tuple(candidates)

    return GarbageCollectionResult(
        gc_result_id=gc_result_id,
        state=_ACCEPTED,
        orphan_candidate_ids=tuple(sorted(candidates)),
        deleted_ids=deleted,
        referenced_kept_ids=tuple(sorted(referenced)),
    )


# ==========================================================================
# Emergency Recovery
# ==========================================================================

# §3.10.1 の許可操作。9件。
ALLOWED_EMERGENCY_OPERATIONS: Final[tuple[str, ...]] = (
    "STORE_READ",
    "LEDGER_CHAIN_VERIFY",
    "ARTIFACT_HASH_VERIFY",
    "PROJECTION_REBUILD",
    "RECOVERY_EVENT_APPEND",
    "FILESYSTEM_READONLY_COMPARE",
    "SAFE_CANCEL",
    "APPROVED_COMPENSATING_EVENT",
    "EVIDENCE_EXPORT",
)

# §3.10.1 の禁止操作。9件。
#
# 設計本文は「新Action、新Effect準備／実行、Provider呼出、Budget予約、
# Outbox Dispatch、Workspace Write、Policy／Flag変更、
# Ledger／Journal／Receipt UPDATE／DELETE」と読点で8区切りに書かれている。
# Case は `denied_operation_count == 9` を要求するため、どこかが2操作である。
#
# 「新Effect準備／実行」を2つに割った。本Systemでは Journal の
# `PREPARED_DURABLE` 確定（準備）と Effect 実行が**別の境界**であり、
# 不変条件#2 がその境界そのものを規定している。他の候補
# （Policy変更／Flag変更）は同一の設定変更操作であり、分ける理由が弱い。
DENIED_EMERGENCY_OPERATIONS: Final[tuple[str, ...]] = (
    "NEW_ACTION",
    "NEW_EFFECT_PREPARE",
    "NEW_EFFECT_EXECUTE",
    "PROVIDER_CALL",
    "BUDGET_RESERVATION",
    "OUTBOX_DISPATCH",
    "WORKSPACE_WRITE",
    "POLICY_OR_FLAG_CHANGE",
    "LEDGER_JOURNAL_RECEIPT_MUTATION",
)


@dataclass(frozen=True, slots=True)
class EmergencyRecoveryVerdict:
    """Emergency Recovery の判定結果。"""

    emergency_recovery_id: str
    state: str
    error_code: ErrorCode | None
    events: tuple[EventType, ...]
    allowed_operation_pass_count: int
    denied_operation_count: int
    recovery_started: bool
    new_effects: int
    workspace_writes: int
    provider_calls: int

    @property
    def accepted(self) -> bool:
        return self.state == _ACCEPTED


def evaluate_emergency_recovery(
    requested_operations: Sequence[str],
    *,
    emergency_recovery_id: str,
    signature_valid: bool,
    profile_version: int,
    minimum_accepted_profile_version: int,
) -> EmergencyRecoveryVerdict:
    """Emergency Recovery の要求を判定する。

    ## 署名を先に見る

    Profileの署名が不正なら、**要求の中身を見る前に**止める。
    中身を見てから判断すると、署名の無いProfileが「許可操作だけだったから
    通した」という経路を持つことになる。Recoveryは開始しない。

    ## Rollbackを拒む

    古いProfile Versionへ戻せるなら、制限の緩かった版を持ち出せる。
    `minimum_accepted_profile_version` より古いものを受け付けない。

    ## 許可操作でも新しい作用は起こさない

    許可9操作はいずれも読取り・検証・追記であり、外部への作用を含まない。
    `new_effects` / `workspace_writes` / `provider_calls` は常に0である。
    """
    if not signature_valid or profile_version < minimum_accepted_profile_version:
        return EmergencyRecoveryVerdict(
            emergency_recovery_id=emergency_recovery_id,
            state=_REJECTED,
            error_code=ErrorCode.EMERGENCY_PROFILE_SIGNATURE_INVALID,
            events=(),
            allowed_operation_pass_count=0,
            denied_operation_count=0,
            # 署名を確かめられていないので、Recoveryを始めない。
            recovery_started=False,
            new_effects=0,
            workspace_writes=0,
            provider_calls=0,
        )

    requested = list(requested_operations)
    denied = [op for op in requested if op not in ALLOWED_EMERGENCY_OPERATIONS]

    if denied:
        return EmergencyRecoveryVerdict(
            emergency_recovery_id=emergency_recovery_id,
            state=_REJECTED,
            error_code=ErrorCode.EMERGENCY_OPERATION_NOT_ALLOWED,
            # Policyを迂回した事実だけは記録する（§3.10.1）。
            events=(EventType.POLICY_STALE_RECOVERY_ONLY,),
            allowed_operation_pass_count=0,
            denied_operation_count=len(denied),
            recovery_started=False,
            new_effects=0,
            workspace_writes=0,
            provider_calls=0,
        )

    return EmergencyRecoveryVerdict(
        emergency_recovery_id=emergency_recovery_id,
        state=_ACCEPTED,
        error_code=None,
        events=(
            EventType.POLICY_STALE_RECOVERY_ONLY,
            EventType.RECOVERY_STARTED,
            EventType.RECOVERY_DECIDED,
        ),
        allowed_operation_pass_count=len(requested),
        denied_operation_count=0,
        recovery_started=True,
        # 許可操作は読取り・検証・追記だけ。外部作用を含まない。
        new_effects=0,
        workspace_writes=0,
        provider_calls=0,
    )
