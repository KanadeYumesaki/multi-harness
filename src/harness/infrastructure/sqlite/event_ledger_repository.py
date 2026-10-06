"""EventLedgerPortのSQLite実装。

不変条件#15に従い`commit()`しない。呼出側のUnit of Workが開いた
`BEGIN IMMEDIATE` Transactionの内側で実行される前提とする。

Appendは`expected_stream_sequence`によるCASとする。Stream末尾と一致しない場合は
`EVENT_ORDER_VIOLATION`で停止し、推測で書かない（不変条件#9）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.events import EventType
from harness.domain.hashing import ContentHash
from harness.domain.ledger import (
    ChainVerificationResult,
    LedgerEntry,
    compute_event_hash,
    verify_chain,
)
from harness.domain.transitions import validate_event_order
from harness.infrastructure.sqlite.transaction_guard import require_transaction
from harness.ports.event_ledger import AppendResult, NewEvent

__all__ = ["SqliteEventLedgerRepository"]


class SqliteEventLedgerRepository:
    """`event_ledger`テーブルへの追記専用Repository。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def stream_head(self, stream_id: str) -> int:
        """Streamの末尾Sequence。未使用Streamは0。"""
        sequence, _previous = self._head(stream_id)
        return sequence

    def transaction_identity(self) -> object:
        """`Transactional`。同一Transaction文脈の識別子。

        Receipt保存と同一Transactionに入ることをApplication層が
        確認するために使う。比較専用で、中身は解釈させない。
        """
        return self._connection

    # ------------------------------------------------------------------
    # append
    # ------------------------------------------------------------------

    def append(self, events: Sequence[NewEvent], *, expected_stream_sequence: int) -> AppendResult:
        require_transaction(self._connection, "event ledger append")
        if not events:
            raise HarnessError(
                ErrorCode.EVENT_ORDER_VIOLATION, "append requires at least one event"
            )
        stream_ids = {event.stream_id for event in events}
        if len(stream_ids) != 1:
            raise HarnessError(
                ErrorCode.EVENT_ORDER_VIOLATION,
                "a single append must target exactly one stream",
            )
        stream_id = events[0].stream_id

        current_sequence, previous_hash = self._head(stream_id)
        if current_sequence != expected_stream_sequence:
            raise HarnessError(
                ErrorCode.EVENT_ORDER_VIOLATION,
                f"expected_stream_sequence {expected_stream_sequence} does not match "
                f"stream head {current_sequence} for stream {stream_id}",
            )

        # §1.4.1 の順序検証はStream内の既存Eventを見て行う。
        # 同一Append内の先行Eventも順序の根拠になるため、逐次積み上げる。
        seen: list[EventType] = [
            EventType(entry.event_type) for entry in self.load_stream(stream_id)
        ]

        sequence = current_sequence
        for event in events:
            self._reject_unknown_event_type(event.event_type)
            event_type = EventType(event.event_type)
            validate_event_order(event_type, seen)
            seen.append(event_type)
            sequence += 1
            event_hash = compute_event_hash(
                stream_id=event.stream_id,
                sequence_number=sequence,
                event_type=event.event_type,
                payload_hash=event.payload_hash,
                recorded_at=event.recorded_at,
                previous_event_hash=previous_hash,
            )
            self._insert(event, sequence, previous_hash, event_hash)
            previous_hash = event_hash

        # events が非空であることは冒頭で検証済みのため previous_hash は確定している。
        # 型の上でも保証するため明示的に確認する（推測で通さない・不変条件#9）。
        if previous_hash is None:
            raise HarnessError(
                ErrorCode.EVENT_ORDER_VIOLATION, "append produced no head event hash"
            )
        return AppendResult(
            appended_count=len(events),
            first_sequence_number=current_sequence + 1,
            last_sequence_number=sequence,
            head_event_hash=previous_hash,
        )

    def _insert(
        self,
        event: NewEvent,
        sequence: int,
        previous_hash: ContentHash | None,
        event_hash: ContentHash,
    ) -> None:
        try:
            self._connection.execute(
                "INSERT INTO event_ledger ("
                " stream_id, sequence_number, event_type, payload_hash,"
                " recorded_at, previous_event_hash, event_hash"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event.stream_id,
                    sequence,
                    event.event_type,
                    str(event.payload_hash),
                    event.recorded_at,
                    None if previous_hash is None else str(previous_hash),
                    str(event_hash),
                ),
            )
        except sqlite3.IntegrityError as exc:
            # 並行Appendや重複Hashは推測で解決せず停止する。
            raise HarnessError(
                ErrorCode.EVENT_ORDER_VIOLATION,
                f"ledger insert rejected at sequence {sequence}: {exc}",
            ) from exc

    @staticmethod
    def _reject_unknown_event_type(event_type: str) -> None:
        """§1.8 の正本Event Typeだけを受け付ける。"""
        try:
            EventType(event_type)
        except ValueError as exc:
            raise HarnessError(
                ErrorCode.EVENT_ORDER_VIOLATION, f"unknown event type: {event_type}"
            ) from exc

    def _head(self, stream_id: str) -> tuple[int, ContentHash | None]:
        row = self._connection.execute(
            "SELECT sequence_number, event_hash FROM event_ledger"
            " WHERE stream_id = ? ORDER BY sequence_number DESC LIMIT 1",
            (stream_id,),
        ).fetchone()
        if row is None:
            return 0, None
        return int(row["sequence_number"]), ContentHash.parse(row["event_hash"])

    # ------------------------------------------------------------------
    # read
    # ------------------------------------------------------------------

    def load_stream(self, stream_id: str, *, after_sequence: int = 0) -> list[LedgerEntry]:
        rows = self._connection.execute(
            "SELECT stream_id, sequence_number, event_type, payload_hash,"
            " recorded_at, previous_event_hash, event_hash"
            " FROM event_ledger WHERE stream_id = ? AND sequence_number > ?"
            " ORDER BY sequence_number ASC",
            (stream_id, after_sequence),
        ).fetchall()
        return [self._to_entry(row) for row in rows]

    def verify_chain(self, stream_id: str) -> ChainVerificationResult:
        return verify_chain(self.load_stream(stream_id))

    @staticmethod
    def _to_entry(row: sqlite3.Row) -> LedgerEntry:
        previous = row["previous_event_hash"]
        return LedgerEntry(
            stream_id=row["stream_id"],
            sequence_number=int(row["sequence_number"]),
            event_type=row["event_type"],
            payload_hash=ContentHash.parse(row["payload_hash"]),
            recorded_at=row["recorded_at"],
            previous_event_hash=None if previous is None else ContentHash.parse(previous),
            event_hash=ContentHash.parse(row["event_hash"]),
        )
