"""Masking Streamの未終端検出とLease束縛の追記専用SQLite Repository。"""

from __future__ import annotations

import sqlite3
from typing import Final

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.timestamps import canonical_timestamp
from harness.infrastructure.sqlite.transaction_guard import require_transaction
from harness.ports.masking import MaskingRecoveryStream

__all__ = ["SqliteMaskingRecoveryRepository"]

_STARTED: Final[str] = "INPUT_MASKING_STARTED"
_TERMINALS: Final[tuple[str, str]] = ("INPUT_MASKING_COMPLETED", "INPUT_MASKING_REJECTED")


class SqliteMaskingRecoveryRepository:
    """STARTED StreamをLeaseへ束縛し、未終端集合を読み出す。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def transaction_identity(self) -> object:
        return self._connection

    def record_started_stream(
        self,
        *,
        stream_id: str,
        run_id: str,
        attempt_id: str | None,
        lease_id: str | None,
        started_at: str,
    ) -> None:
        """STARTED Eventと同一TransactionでStream→Lease関係を確定する。"""
        require_transaction(self._connection, "masking recovery stream record")
        if not stream_id or not run_id:
            raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "stream_id and run_id are required")
        if (attempt_id is None) != (lease_id is None):
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "attempt_id and lease_id must be both set or both absent",
            )
        try:
            self._connection.execute(
                """
                INSERT INTO masking_recovery_stream (
                    stream_id, run_id, attempt_id, lease_id, started_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (stream_id, run_id, attempt_id, lease_id, canonical_timestamp(started_at)),
            )
        except sqlite3.IntegrityError as exc:
            raise HarnessError(
                ErrorCode.STORAGE_WRITE_FAILED,
                f"masking recovery stream already exists: {stream_id}",
            ) from exc

    def find_incomplete_streams(self) -> tuple[MaskingRecoveryStream, ...]:
        """開始Eventはあるが終端Eventが無いStreamと耐久束縛を返す。

        期限判定はここで行わない。Application層がLease Portと現在時刻を渡して
        ACTIVEを除外し、EXPIREDだけを自動Recovery候補にする。
        """
        rows = self._connection.execute(
            """
            SELECT started.stream_id, binding.run_id, binding.attempt_id, binding.lease_id
            FROM event_ledger AS started
            LEFT JOIN masking_recovery_stream AS binding
              ON binding.stream_id = started.stream_id
            WHERE started.event_type = ?
              AND NOT EXISTS (
                    SELECT 1 FROM event_ledger AS terminal
                    WHERE terminal.stream_id = started.stream_id
                      AND terminal.event_type IN (?, ?)
              )
            ORDER BY started.stream_id
            """,
            (_STARTED, *_TERMINALS),
        ).fetchall()
        return tuple(
            MaskingRecoveryStream(
                stream_id=str(row["stream_id"]),
                run_id=None if row["run_id"] is None else str(row["run_id"]),
                attempt_id=None if row["attempt_id"] is None else str(row["attempt_id"]),
                lease_id=None if row["lease_id"] is None else str(row["lease_id"]),
            )
            for row in rows
        )
