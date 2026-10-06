"""Operation JournalとEffect Receiptの永続化Port契約。"""

from __future__ import annotations

from typing import Protocol

from harness.domain.effect import OperationJournal

__all__ = ["EffectJournalPort"]


class EffectJournalPort(Protocol):
    def create_prepared(self, journal: OperationJournal) -> None:
        """PREPARED_DURABLE JournalをLedgerのACTION_PREPAREDと同一Txで保存する。"""
        ...

    def get_by_effect_id(self, effect_id: str) -> OperationJournal | None: ...

    def update(self, journal: OperationJournal, *, expected_store_version: int) -> None:
        """状態遷移をCASで保存する。古いToken／Versionは拒否する。"""
        ...
