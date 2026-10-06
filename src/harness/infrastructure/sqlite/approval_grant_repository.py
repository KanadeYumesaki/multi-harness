"""SQLite Approval Grant Repository。"""

from __future__ import annotations

import json
import sqlite3

from harness.domain.approval import ApprovalGrant, ApprovalStatus
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash
from harness.domain.timestamps import canonical_timestamp
from harness.infrastructure.sqlite.transaction_guard import require_transaction
from harness.ports.approval import ApprovalConsumeRequest

__all__ = ["SqliteApprovalGrantRepository"]


class SqliteApprovalGrantRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def transaction_identity(self) -> object:
        return self._connection

    def issue(self, grant: ApprovalGrant) -> None:
        require_transaction(self._connection, "approval grant issue")
        if grant.status is not ApprovalStatus.ISSUED:
            raise HarnessError(
                ErrorCode.APPROVAL_INVALIDATED, "only ISSUED grants can be persisted"
            )
        try:
            self._connection.execute(
                """
                INSERT INTO approval_grant (
                    grant_id, nonce, plan_content_hash, execution_plan_hash, action_scope,
                    approver_subject_id, approver_tenant_id, authentication_context_class,
                    mfa_performed, authentication_time, issued_at, not_before, expires_at,
                    maximum_clock_skew_seconds, revocation_epoch, issuer_id, issuer_key_id,
                    signature_algorithm, signature, status, store_version, delegation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    grant.grant_id,
                    grant.nonce,
                    str(grant.plan_content_hash),
                    str(grant.execution_plan_hash),
                    _dump(list(grant.action_scope)),
                    grant.approver_subject_id,
                    grant.approver_tenant_id,
                    grant.authentication_context_class,
                    int(grant.mfa_performed),
                    _time(grant.authentication_time),
                    _time(grant.issued_at),
                    _time(grant.not_before),
                    _time(grant.expires_at),
                    grant.maximum_clock_skew_seconds,
                    grant.revocation_epoch,
                    grant.issuer_id,
                    grant.issuer_key_id,
                    grant.signature_algorithm,
                    grant.signature,
                    grant.status.value,
                    grant.store_version,
                    grant.delegation_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HarnessError(
                ErrorCode.APPROVAL_REPLAY, "grant_id or nonce already exists"
            ) from exc

    def get(self, grant_id: str) -> ApprovalGrant | None:
        row = self._connection.execute(
            "SELECT * FROM approval_grant WHERE grant_id = ?", (grant_id,)
        ).fetchone()
        return None if row is None else _grant(row)

    def consume(self, request: ApprovalConsumeRequest) -> ApprovalGrant:
        require_transaction(self._connection, "approval grant consume")
        grant = self.get(request.grant_id)
        if grant is None:
            raise HarnessError(ErrorCode.APPROVAL_REQUIRED, "approval grant was not found")
        consumed = grant.consume(
            now=request.now,
            expected_execution_plan_hash=request.expected_execution_plan_hash,
            current_revocation_epoch=request.current_revocation_epoch,
            actor_id=request.actor_id,
            attempt_id=request.attempt_id,
            signature_valid=request.signature_valid,
        )
        cursor = self._connection.execute(
            """
            UPDATE approval_grant
            SET status = ?, store_version = ?, consumed_at = ?,
                consumed_by_actor_id = ?, attempt_id = ?
            WHERE grant_id = ? AND status = 'ISSUED' AND store_version = ?
            """,
            (
                consumed.status.value,
                consumed.store_version,
                _time(consumed.consumed_at),
                consumed.consumed_by_actor_id,
                consumed.attempt_id,
                request.grant_id,
                request.expected_store_version,
            ),
        )
        if cursor.rowcount != 1:
            raise HarnessError(ErrorCode.APPROVAL_REPLAY, "approval grant CAS was not acquired")
        return consumed

    def invalidate(
        self, *, grant_id: str, expected_store_version: int, now: str, reason: str
    ) -> ApprovalGrant:
        require_transaction(self._connection, "approval grant invalidate")
        grant = self.get(grant_id)
        if grant is None:
            raise HarnessError(ErrorCode.APPROVAL_REQUIRED, "approval grant was not found")
        invalidated = grant.invalidate(now=now, reason=reason)
        cursor = self._connection.execute(
            """
            UPDATE approval_grant
            SET status = ?, store_version = ?, invalidated_at = ?, invalidation_reason = ?
            WHERE grant_id = ? AND status = 'ISSUED' AND store_version = ?
            """,
            (
                invalidated.status.value,
                invalidated.store_version,
                _time(invalidated.invalidated_at),
                invalidated.invalidation_reason,
                grant_id,
                expected_store_version,
            ),
        )
        if cursor.rowcount != 1:
            raise HarnessError(
                ErrorCode.APPROVAL_INVALIDATED, "approval grant CAS was not acquired"
            )
        return invalidated


def _grant(row: sqlite3.Row) -> ApprovalGrant:
    scope = json.loads(str(row["action_scope"]))
    if not isinstance(scope, list) or not all(isinstance(item, str) for item in scope):
        raise HarnessError(ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "stored action scope is invalid")
    status = ApprovalStatus(str(row["status"]))
    return ApprovalGrant(
        grant_id=str(row["grant_id"]),
        plan_content_hash=ContentHash.parse(str(row["plan_content_hash"])),
        execution_plan_hash=ContentHash.parse(str(row["execution_plan_hash"])),
        action_scope=tuple(scope),
        approver_subject_id=str(row["approver_subject_id"]),
        approver_tenant_id=str(row["approver_tenant_id"]),
        authentication_context_class=str(row["authentication_context_class"]),
        mfa_performed=bool(row["mfa_performed"]),
        authentication_time=_parse_time(str(row["authentication_time"])),
        issued_at=_parse_time(str(row["issued_at"])),
        not_before=_parse_time(str(row["not_before"])),
        expires_at=_parse_time(str(row["expires_at"])),
        maximum_clock_skew_seconds=int(row["maximum_clock_skew_seconds"]),
        nonce=str(row["nonce"]),
        revocation_epoch=int(row["revocation_epoch"]),
        issuer_id=str(row["issuer_id"]),
        issuer_key_id=str(row["issuer_key_id"]),
        signature_algorithm=str(row["signature_algorithm"]),
        signature=str(row["signature"]),
        status=status,
        store_version=int(row["store_version"]),
        consumed_at=_nullable_time(row["consumed_at"]),
        consumed_by_actor_id=_nullable_string(row["consumed_by_actor_id"]),
        attempt_id=_nullable_string(row["attempt_id"]),
        invalidated_at=_nullable_time(row["invalidated_at"]),
        invalidation_reason=_nullable_string(row["invalidation_reason"]),
        delegation_id=_nullable_string(row["delegation_id"]),
    )


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _time(value: str | None) -> str | None:
    return None if value is None else canonical_timestamp(value)


def _parse_time(value: str) -> str:
    return canonical_timestamp(value)


def _nullable_time(value: object) -> str | None:
    return None if value is None else _parse_time(str(value))


def _nullable_string(value: object) -> str | None:
    return None if value is None else str(value)
