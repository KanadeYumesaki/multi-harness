"""§3.10／§27 運用操作のPort契約。Drain観測とBackup/Restoreの入出力。

## なぜ数え上げをPortにするか

`domain.operations_safety.evaluate_deployment()` は `active_run_count` と
`pending_approval_count` を**受け取るだけ**である。誰がその数を数えるのかを
決めていないと、判定器は正しいのに「実際のDBを一度も見ていない」まま
Drain 合格を主張できてしまう。

Portにしておけば、Application層は抽象へ依存したまま、Adapterが**実Store**を
数えたことを試験で確かめられる。独立Probeが返す 0 と、実DBを数えた 0 を
区別するのはこの境界である。

## Restore中の作用遮断をPortにする理由

Restore は過去の再構成であり新しい作用ではない（`domain.backup_restore`）。
「起こさなかった」を主張するには、**起こそうとした回数を数えられる形**で
作用経路を通す必要がある。変数へ 0 を代入するのでは観測になっていない。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "BackupArtifact",
    "DrainInspectionPort",
    "DrainSnapshot",
    "EffectAttempt",
    "RestoreTargetPort",
]


@dataclass(frozen=True, slots=True)
class DrainSnapshot:
    """Drain判定のために**実Storeから数えた**現在値。

    `observed_at` と `source` を持たせるのは、どの時点のどのStoreを見たのかを
    Evidence へ残すためである。数字だけでは「見て数えた」ことを示せない。
    """

    active_run_count: int
    pending_approval_count: int
    #: 進行中と数えた内訳。`operation_journal.state` はそのまま、
    #: `cli_invocation_journal` は `cli_invocation_journal:<state>`、受付予約は `ADMITTED`。
    active_states: tuple[tuple[str, int], ...]
    #: 承認待ちと数えた `approval_grant.status` の内訳。
    pending_statuses: tuple[tuple[str, int], ...]
    observed_at: str
    #: 数えた対象の識別（DB Path など）。**推測で埋めない。**
    source: str

    @property
    def quiesced(self) -> bool:
        """新規受付を止めたうえで、進行中も承認待ちも無い状態か。"""
        return self.active_run_count == 0 and self.pending_approval_count == 0


@dataclass(frozen=True, slots=True)
class EffectAttempt:
    """Restore区間で作用を起こそうとした記録。**遮断しても記録は残す。**"""

    port: str
    operation: str
    detail: str


@dataclass(frozen=True, slots=True)
class BackupArtifact:
    """作成したBackupの識別と束縛。"""

    backup_id: str
    database_path: str
    cas_root: str
    database_sha256: str
    created_at: str
    #: Backup時点の Ledger Chain Head。Restore後の比較基準。
    source_chain_heads: tuple[tuple[str, str], ...]
    artifact_ids: tuple[str, ...]


class DrainInspectionPort(Protocol):
    """実Storeを数えてDrain状態を返す。"""

    def inspect(self) -> DrainSnapshot:
        """現在の進行中処理と承認待ちを数える。"""
        ...

    def stop_intake(self) -> None:
        """新規受付を止める。止めた後に受け付けたら例外にする。"""
        ...

    def intake_stopped(self) -> bool:
        """新規受付が止まっているか。"""
        ...


class RestoreTargetPort(Protocol):
    """Restore先。復元と、復元結果の読み直しを担う。"""

    def restore(
        self, backup: BackupArtifact, *, checkpoint: Callable[[], None] | None = None
    ) -> None:
        """Backupから復元する。**この区間で作用を起こしてはならない。**"""
        ...

    def chain_heads(self) -> tuple[tuple[str, str], ...]:
        """復元後の Ledger Chain Head を読み直す。"""
        ...

    def artifact_ids(self) -> tuple[str, ...]:
        """復元後の Artifact Manifest の参照集合を読み直す。"""
        ...
