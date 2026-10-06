"""SQLite Lease Repository。"""

from __future__ import annotations

import sqlite3

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.lease import Lease, LeaseStatus
from harness.domain.timestamps import canonical_timestamp
from harness.infrastructure.sqlite.transaction_guard import require_transaction
from harness.ports.lease import LeaseAcquireRequest

__all__ = ["SqliteLeaseRepository"]


class SqliteLeaseRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def transaction_identity(self) -> object:
        return self._connection

    def acquire(self, request: LeaseAcquireRequest) -> Lease:
        require_transaction(self._connection, "lease acquire")
        active = self._connection.execute(
            """
            SELECT lease_id FROM lease
            WHERE resource_key = ? AND status = 'ACTIVE' AND expires_at > ?
            ORDER BY fencing_token DESC LIMIT 1
            """,
            (request.resource_key, _time(request.issued_at)),
        ).fetchone()
        if active is not None:
            raise HarnessError(
                ErrorCode.STALE_FENCING_TOKEN, "resource already has an active lease"
            )
        row = self._connection.execute(
            "SELECT COALESCE(MAX(fencing_token), 0) AS token FROM lease WHERE resource_key = ?",
            (request.resource_key,),
        ).fetchone()
        next_token = int(row["token"]) + 1 if row is not None else 1
        lease = Lease(
            lease_id=f"lease:{request.resource_key}:{next_token}",
            resource_key=request.resource_key,
            holder_id=request.holder_id,
            attempt_id=request.attempt_id,
            fencing_token=next_token,
            issued_at=request.issued_at,
            expires_at=request.expires_at,
            renewed_at=request.issued_at,
            status=LeaseStatus.ACTIVE,
            store_version=1,
        )
        self._connection.execute(
            """
            INSERT INTO lease (
                lease_id, resource_key, holder_id, attempt_id, fencing_token,
                issued_at, expires_at, renewed_at, status, store_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _lease_values(lease),
        )
        return lease

    def get(self, lease_id: str) -> Lease | None:
        row = self._connection.execute(
            "SELECT * FROM lease WHERE lease_id = ?", (lease_id,)
        ).fetchone()
        return None if row is None else _lease(row)

    def current_fencing_token(self, resource_key: str) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(fencing_token), 0) AS token FROM lease WHERE resource_key = ?",
            (resource_key,),
        ).fetchone()
        return int(row["token"]) if row is not None else 0

    def renew(self, lease: Lease, *, now: str, expires_at: str) -> Lease:
        require_transaction(self._connection, "lease renew")
        current = self.current_fencing_token(lease.resource_key)
        renewed = lease.renew(now=now, expires_at=expires_at, current_token=current)
        cursor = self._connection.execute(
            """
            UPDATE lease SET expires_at = ?, renewed_at = ?, store_version = ?
            WHERE lease_id = ? AND status = 'ACTIVE' AND store_version = ? AND fencing_token = ?
            """,
            (
                _time(renewed.expires_at),
                _time(renewed.renewed_at),
                renewed.store_version,
                lease.lease_id,
                lease.store_version,
                lease.fencing_token,
            ),
        )
        if cursor.rowcount != 1:
            raise HarnessError(ErrorCode.STALE_FENCING_TOKEN, "lease renewal CAS was not acquired")
        return renewed

    def release(self, lease: Lease, *, now: str) -> Lease:
        require_transaction(self._connection, "lease release")
        current = self.current_fencing_token(lease.resource_key)
        released = lease.release(now=now, current_token=current)
        cursor = self._connection.execute(
            """
            UPDATE lease SET status = ?, store_version = ?
            WHERE lease_id = ? AND status = 'ACTIVE' AND store_version = ? AND fencing_token = ?
            """,
            (
                released.status.value,
                released.store_version,
                lease.lease_id,
                lease.store_version,
                lease.fencing_token,
            ),
        )
        if cursor.rowcount != 1:
            raise HarnessError(ErrorCode.STALE_FENCING_TOKEN, "lease release CAS was not acquired")
        return released


def _lease(row: sqlite3.Row) -> Lease:
    return Lease(
        lease_id=str(row["lease_id"]),
        resource_key=str(row["resource_key"]),
        holder_id=str(row["holder_id"]),
        attempt_id=str(row["attempt_id"]),
        fencing_token=int(row["fencing_token"]),
        issued_at=_parse_time(str(row["issued_at"])),
        expires_at=_parse_time(str(row["expires_at"])),
        renewed_at=_parse_time(str(row["renewed_at"])),
        status=LeaseStatus(str(row["status"])),
        store_version=int(row["store_version"]),
    )


def _lease_values(lease: Lease) -> tuple[object, ...]:
    return (
        lease.lease_id,
        lease.resource_key,
        lease.holder_id,
        lease.attempt_id,
        lease.fencing_token,
        _time(lease.issued_at),
        _time(lease.expires_at),
        _time(lease.renewed_at),
        lease.status.value,
        lease.store_version,
    )


def _time(value: str) -> str:
    return canonical_timestamp(value)


def _parse_time(value: str) -> str:
    return canonical_timestamp(value)
