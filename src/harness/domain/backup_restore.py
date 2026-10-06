"""§27 Migration と Backup / Restore の判定。

## Fresh Install

新規DBのMigrationは、次を**全て**満たしたときだけ成功である。

* 到達した schema version が期待値と一致する
* `integrity_check` が `ok`
* Migration Head が記録されている

**部分Migrationを成功扱いしない。** 途中まで適用されたDBは、
「どのVersionなのか」が確定しない。確定しないものをACCEPTEDにしない。

## Backup / Restore

Restore後に確かめるのは2つである。

| 観点 | 判定 |
|---|---|
| Event Ledger | `restored_chain_head == original_chain_head` かつ Chainが連続 |
| Artifact | Manifest件数と参照集合が一致 |

Chain Headが一致しても**途中が繋がっていなければ**不合格である。
Head だけを比べるのは「最後の1件が同じ」しか言っておらず、
間の改変を見逃す。Chainを端から辿る。

## Restore中にEffectを起こさない

Restoreは過去の記録を再構成する操作であり、新しい作用ではない。
Restore中にEffectが1件でも走ったら、それはRestoreではない。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from harness.domain._registry_generated import ErrorCode
from harness.domain.hashing import ContentHash

__all__ = [
    "BackupRestoreResult",
    "LedgerChainSnapshot",
    "MigrationOutcome",
    "evaluate_backup_restore",
    "evaluate_fresh_install",
]

_ACCEPTED: Final[str] = "ACCEPTED"
_REJECTED: Final[str] = "REJECTED"


@dataclass(frozen=True, slots=True)
class MigrationOutcome:
    """Fresh Install Migration の結果。"""

    migration_result_id: str
    state: str
    error_code: ErrorCode | None
    schema_version: str | None
    expected_schema_version: str
    integrity_check: str
    migration_head: str | None
    problems: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.state == _ACCEPTED


def evaluate_fresh_install(
    *,
    migration_result_id: str,
    schema_version: str | None,
    expected_schema_version: str,
    integrity_check: str,
    migration_head: str | None,
) -> MigrationOutcome:
    """新規DBのMigration結果を判定する。**部分適用を成功にしない。**"""
    problems: list[str] = []
    if schema_version != expected_schema_version:
        problems.append("schema_version != expected_schema_version")
    if integrity_check != "ok":
        problems.append("integrity_check != ok")
    if not migration_head:
        # Headが無いDBは「どこまで適用したか」を示せない。
        problems.append("migration_head missing")

    state = _ACCEPTED if not problems else _REJECTED
    return MigrationOutcome(
        migration_result_id=migration_result_id,
        state=state,
        error_code=None if not problems else ErrorCode.MIGRATION_FAILED,
        schema_version=schema_version,
        expected_schema_version=expected_schema_version,
        integrity_check=integrity_check,
        migration_head=migration_head,
        problems=tuple(problems),
    )


@dataclass(frozen=True, slots=True)
class LedgerChainSnapshot:
    """Event Ledger の Hash Chain。Restore前後を比べる。"""

    #: 各Eventの `(sequence_number, event_hash, previous_hash)`。
    entries: tuple[tuple[int, ContentHash, ContentHash | None], ...]

    @property
    def head(self) -> ContentHash | None:
        return self.entries[-1][1] if self.entries else None

    def broken_links(self) -> tuple[int, ...]:
        """前Eventへ繋がっていない位置。**端から辿る。**

        Headだけを比べると「最後の1件が同じ」しか言えず、
        途中の改変を見逃す。
        """
        broken: list[int] = []
        previous: ContentHash | None = None
        expected_sequence = 0
        for sequence, event_hash, link in self.entries:
            expected_sequence += 1
            if sequence != expected_sequence or link != previous:
                broken.append(sequence)
            previous = event_hash
        return tuple(broken)


@dataclass(frozen=True, slots=True)
class BackupRestoreResult:
    """Restore の判定結果。"""

    backup_restore_id: str
    state: str
    error_code: ErrorCode | None
    original_chain_head: ContentHash | None
    restored_chain_head: ContentHash | None
    artifact_manifest_count_equal: bool
    effects_during_restore: int
    problems: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.state == _ACCEPTED


def evaluate_backup_restore(
    *,
    backup_restore_id: str,
    original: LedgerChainSnapshot,
    restored: LedgerChainSnapshot,
    original_manifest_ids: Sequence[str],
    restored_manifest_ids: Sequence[str],
    effects_during_restore: int,
) -> BackupRestoreResult:
    """Restore結果を判定する。

    Chain Headの一致だけでなく、**Chainが端から繋がっていること**と
    Artifact Manifestの**参照集合**が一致することを確かめる。
    """
    problems: list[str] = []

    if original.head != restored.head:
        problems.append("restored_chain_head != original_chain_head")
    if len(original.entries) != len(restored.entries):
        problems.append("chain length differs")

    broken = restored.broken_links()
    if broken:
        # Headが同じでも途中が切れていればRestoreは成立していない。
        problems.append(f"restored chain broken at {list(broken)}")

    original_ids = sorted(original_manifest_ids)
    restored_ids = sorted(restored_manifest_ids)
    count_equal = len(original_ids) == len(restored_ids)
    if not count_equal:
        problems.append("artifact_manifest_count differs")
    elif original_ids != restored_ids:
        # 件数が同じでも中身が違えば別物である。
        problems.append("artifact_manifest ids differ")

    if effects_during_restore:
        # Restoreは過去の再構成であり、新しい作用ではない。
        problems.append("effects executed during restore")

    # Chainが切れている場合は改ざん検知のCodeを使う。単なる失敗と区別する。
    state = _ACCEPTED if not problems else _REJECTED
    error_code: ErrorCode | None = None
    if problems:
        chain_damaged = any("chain" in p for p in problems)
        error_code = (
            ErrorCode.LEDGER_CHAIN_TAMPERED if chain_damaged else ErrorCode.BACKUP_RESTORE_FAILED
        )
    return BackupRestoreResult(
        backup_restore_id=backup_restore_id,
        state=state,
        error_code=error_code,
        original_chain_head=original.head,
        restored_chain_head=restored.head,
        artifact_manifest_count_equal=count_equal,
        effects_during_restore=effects_during_restore,
        problems=tuple(problems),
    )
