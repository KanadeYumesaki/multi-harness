"""決定論的なFault注入Adapter。**実Filesystemを壊さない。**

## なぜFakeで注入するか

ENOSPC や EIO を実環境で起こすには、Filesystemを本当に埋めるか
Deviceを壊す必要がある。それは再現しないうえ、試験環境そのものを
損なう。CLAUDE.md「Fault InjectionはPort経由で注入する」に従い、
Adapterの側で決定論的に失敗させる。

**同じ入力から必ず同じ失敗が出る。** 乱数もタイミング依存も使わない。
再現しない試験はEvidenceにならない。

## 順序を守ったうえで失敗させる

不変条件#13 の順序を実際に辿り、指定段階で例外を送出する。

    Temp write → File fsync → Atomic Rename → Directory fsync → Manifest登録

「Manifest登録の直前で落ちる」と「Temp write直後で落ちる」では
残るものが違う。段階を飛ばして失敗させると、その違いを試験できない。

## 書込み先はtmp_path配下に限る

本Adapterは試験専用であり、渡されたRoot配下にしか書かない。
Rootの外へ出るPathは拒否する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash, hash_bytes
from harness.domain.storage_fault import StorageFaultKind

__all__ = ["DeterministicStorageAdapter", "StorageCounters"]


@dataclass
class StorageCounters:
    """Case assertions が要求する観測値。"""

    write_attempts: int = 0
    effect_attempts: int = 0
    receipt_writes: int = 0
    workspace_commits: int = 0
    ledger_commits: int = 0
    external_effects: int = 0
    atomic_replace_count: int = 0
    partial_transaction_rows: int = 0
    manifest_created: bool = False
    journal_prepared: bool = False
    #: fsync前に消えずに残ったTemp File。
    orphan_temp_files: list[str] = field(default_factory=list)


@dataclass
class DeterministicStorageAdapter:
    """指定した段階で決定論的に失敗するStorage Adapter。

    `fault_kind` が `NONE` なら最後まで通す。通る経路が本当に通ることを
    確かめられないと、失敗経路の試験が何を測っているのか分からない。
    """

    root: Path
    fault_kind: StorageFaultKind = StorageFaultKind.NONE
    counters: StorageCounters = field(default_factory=StorageCounters)

    def _resolve(self, relative: str) -> Path:
        target = (self.root / relative).resolve()
        if self.root.resolve() not in target.parents:
            raise HarnessError(
                ErrorCode.PATH_OUTSIDE_CAPABILITY,
                f"{target} is outside the adapter root",
            )
        return target

    def write_artifact(self, relative: str, payload: bytes) -> ContentHash:
        """§13 の順序どおりに書き、指定段階で失敗する。

        失敗したときは**書きかけを残さない**。Temp Fileを削除してから送出する。
        """
        self.counters.write_attempts += 1
        target = self._resolve(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".tmp")

        # 1. Temp write
        if self.fault_kind is StorageFaultKind.ARTIFACT_WRITE_ENOSPC:
            # 書込みそのものが失敗する。Temp Fileは作られない。
            raise HarnessError(
                ErrorCode.STORAGE_WRITE_FAILED,
                "ENOSPC while writing artifact payload",
            )
        temp.write_bytes(payload)

        # 2. File fsync
        if self.fault_kind is StorageFaultKind.FILE_FSYNC_EIO:
            # fsyncが失敗した。Renameへ進まない。書きかけのTempを片付ける。
            temp.unlink(missing_ok=True)
            raise HarnessError(
                ErrorCode.STORAGE_WRITE_FAILED,
                "EIO while fsyncing artifact file",
            )

        # 3. Atomic Rename → 4. Directory fsync
        temp.replace(target)
        self.counters.atomic_replace_count += 1

        # 5. Manifest登録（最後）
        self.counters.manifest_created = True
        self.counters.workspace_commits += 1
        return hash_bytes(payload)

    def prepare_journal(self, effect_id: str) -> None:
        """Journalを `PREPARED_DURABLE` へ確定する（不変条件#2）。"""
        if self.fault_kind is StorageFaultKind.SQLITE_COMMIT_ENOSPC:
            # Commitが失敗した。Journalは確定していない。
            # 部分行を残さない＝Transactionが巻き戻っている。
            raise HarnessError(
                ErrorCode.STORAGE_WRITE_FAILED,
                f"ENOSPC while committing journal for {effect_id}",
            )
        self.counters.journal_prepared = True
        self.counters.ledger_commits += 1

    def store_receipt(self, receipt_id: str) -> None:
        if self.fault_kind is StorageFaultKind.SQLITE_COMMIT_ENOSPC:
            raise HarnessError(
                ErrorCode.STORAGE_WRITE_FAILED,
                f"ENOSPC while storing receipt {receipt_id}",
            )
        self.counters.receipt_writes += 1

    def surviving_temp_files(self) -> list[str]:
        """失敗後に残ったTemp File。空でなければ後始末に失敗している。"""
        if not self.root.exists():
            return []
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*.tmp"))
