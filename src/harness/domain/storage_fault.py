"""§11 Storage I/O Fault の結果判定。失敗時に部分状態を残さない。

## 何を守る規則か

`AT-FAULT-IO-001` の3 Case はいずれも同じことを要求している。
**書込みが失敗したなら、書きかけの何も残さない。**

| Case | 失敗箇所 | 残ってはいけないもの |
|---|---|---|
| `ARTIFACT_ENOSPC` | Artifact書込み | Manifest、`PREPARED_DURABLE` Journal |
| `FSYNC_EIO` | File fsync | Atomic Rename の結果 |
| `SQLITE_ENOSPC` | SQLite commit | 部分Transaction行、Effect試行 |

いずれも `target_hash == base_hash` を要求する。**対象が実行前のままである**
ことが「何も起きなかった」の定義である。

## Manifest登録は最後である

不変条件#13 は順序を定めている。

    Temp write → File fsync → Atomic Rename → Directory fsync → Manifest登録

どの段階で落ちても Manifest が無ければ、その Artifact は「存在しないもの」
として扱える。Manifest を先に書くと、実体の無いArtifactを指す参照が残る。

## 失敗を成功として記録しない

`STORAGE_IO_RESULT` は `REJECTED` になり、`STORAGE_WRITE_FAILED` を返す。
書けなかったことを「書けた」と記録する経路を作らない。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

from harness.domain._registry_generated import ErrorCode
from harness.domain.hashing import ContentHash

__all__ = [
    "StorageFaultKind",
    "StorageIoOutcome",
    "StorageWriteAttempt",
    "evaluate_storage_fault",
]

_REJECTED: Final[str] = "REJECTED"
_ACCEPTED: Final[str] = "ACCEPTED"


class StorageFaultKind(Enum):
    """注入するStorage Fault。"""

    NONE = "NONE"
    ARTIFACT_WRITE_ENOSPC = "ARTIFACT_WRITE_ENOSPC"
    FILE_FSYNC_EIO = "FILE_FSYNC_EIO"
    SQLITE_COMMIT_ENOSPC = "SQLITE_COMMIT_ENOSPC"


@dataclass(frozen=True, slots=True)
class StorageWriteAttempt:
    """1回の書込み試行の観測値。"""

    storage_io_id: str
    fault_kind: StorageFaultKind
    base_hash: ContentHash
    #: 書込み後にこうなるはずのHash。
    intended_hash: ContentHash
    #: 実際の対象Hash。失敗したなら `base_hash` と等しいはずである。
    target_hash: ContentHash
    manifest_created: bool
    journal_prepared: bool
    atomic_replace_count: int
    partial_transaction_rows: int
    effect_attempts: int


@dataclass(frozen=True, slots=True)
class StorageIoOutcome:
    """Storage I/O の判定結果。"""

    storage_io_id: str
    state: str
    error_code: ErrorCode | None
    fault_kind: StorageFaultKind
    #: 部分状態が残っている場合の説明。残っていなければ空。
    residue: tuple[str, ...]

    @property
    def rejected(self) -> bool:
        return self.state == _REJECTED

    @property
    def clean(self) -> bool:
        """部分状態が1つも残っていないか。"""
        return not self.residue


def evaluate_storage_fault(attempt: StorageWriteAttempt) -> StorageIoOutcome:
    """書込み結果を判定し、**残ってはいけないものが残っていないか**を確かめる。

    Faultが注入されているのに部分状態が残っていれば、それ自体が違反である。
    `residue` へ列挙して返す。空でないまま `ACCEPTED` にはしない。
    """
    if attempt.fault_kind is StorageFaultKind.NONE:
        if attempt.target_hash != attempt.intended_hash:
            return StorageIoOutcome(
                storage_io_id=attempt.storage_io_id,
                state=_REJECTED,
                error_code=ErrorCode.STORAGE_WRITE_FAILED,
                fault_kind=attempt.fault_kind,
                residue=("target_hash did not reach intended_hash",),
            )
        return StorageIoOutcome(
            storage_io_id=attempt.storage_io_id,
            state=_ACCEPTED,
            error_code=None,
            fault_kind=attempt.fault_kind,
            residue=(),
        )

    # **残ってはいけないものはFaultの種類ごとに違う。**
    #
    # 一律に「何も残っていないこと」を課すと、正しい状態まで違反と呼ぶ。
    # 例：SQLite commit が失敗しても、CASへ収まったArtifactは残ってよい。
    # 内容アドレスであり、参照されないままなら後段のGCが回収できる。
    # 一方 Artifact書込みが失敗したなら、その Artifact は存在してはならない。
    #
    # 判定はRegistryの assertions（`tests.yaml`）と一致させる。
    residue: list[str] = []
    if attempt.fault_kind is StorageFaultKind.ARTIFACT_WRITE_ENOSPC:
        # assertions: manifest_created == false / journal_prepared == false
        #             / target_hash == base_hash
        if attempt.target_hash != attempt.base_hash:
            residue.append("target_hash != base_hash")
        if attempt.manifest_created:
            residue.append("manifest_created")
        if attempt.journal_prepared:
            residue.append("journal_prepared")
    elif attempt.fault_kind is StorageFaultKind.FILE_FSYNC_EIO:
        # assertions: atomic_replace_count == 0 / target_hash == base_hash
        if attempt.atomic_replace_count:
            residue.append("atomic_replace_count")
        if attempt.target_hash != attempt.base_hash:
            residue.append("target_hash != base_hash")
    elif attempt.fault_kind is StorageFaultKind.SQLITE_COMMIT_ENOSPC:
        # assertions: partial_transaction_rows == 0 / effect_attempts == 0
        if attempt.partial_transaction_rows:
            residue.append("partial_transaction_rows")
        if attempt.effect_attempts:
            residue.append("effect_attempts")
        # Journalが確定していたらCommitは成功している。Faultと矛盾する。
        if attempt.journal_prepared:
            residue.append("journal_prepared")

    return StorageIoOutcome(
        storage_io_id=attempt.storage_io_id,
        state=_REJECTED,
        error_code=ErrorCode.STORAGE_WRITE_FAILED,
        fault_kind=attempt.fault_kind,
        residue=tuple(residue),
    )
