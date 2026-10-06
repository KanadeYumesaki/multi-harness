"""Schema Migration。Migration専用接続だけが実行する（§1.14）。

Ledgerの追記専用性は多層で強制する。本Moduleが担うのは次の2層。

1. `event_ledger`へUPDATE／DELETE拒否Triggerを置く
2. Migration接続とRuntime接続の分離（`ConnectionRole`）

SQLite authorizerによるRuntime接続のUPDATE／DELETE拒否はTASK-MVP0A-003で追加する。
Triggerは`RAISE(ABORT, ...)`で停止するため、Application層のバグでも実体を守れる。

`sqlite3.Connection.executescript()`は保留中Transactionを暗黙COMMITするため使わない。
Migration全体を1つの`BEGIN IMMEDIATE`へ収めるには文を個別実行する必要がある。
"""

from __future__ import annotations

import sqlite3
from typing import Final

from harness.domain.errors import ErrorCode, HarnessError
from harness.infrastructure.sqlite.connection_factory import (
    ConnectionFactory,
    ConnectionRole,
)

__all__ = ["SCHEMA_VERSION", "migrate"]

SCHEMA_VERSION: Final[int] = 14

# Trigger本体が`;`を含むため、単純なsplitではなく文ごとに保持する。
_MIGRATION_0001: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS schema_migration (
        version      INTEGER PRIMARY KEY,
        applied_at   TEXT    NOT NULL
    ) STRICT
    """,
    # Event Ledger。追記専用。UPDATE／DELETEはTriggerで拒否する（不変条件#1）。
    # event_hash は "sha256:" + 64 hex = 71文字。
    """
    CREATE TABLE IF NOT EXISTS event_ledger (
        stream_id            TEXT    NOT NULL,
        sequence_number      INTEGER NOT NULL,
        event_type           TEXT    NOT NULL,
        payload_hash         TEXT    NOT NULL,
        recorded_at          TEXT    NOT NULL,
        previous_event_hash  TEXT,
        event_hash           TEXT    NOT NULL,
        PRIMARY KEY (stream_id, sequence_number),
        CHECK (sequence_number >= 1),
        CHECK (length(event_hash) = 71),
        CHECK (previous_event_hash IS NULL OR length(previous_event_hash) = 71)
    ) STRICT
    """,
    # 同一Hashの二重登録を拒否し、Chain断裂の検出を補強する。
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_event_ledger_event_hash
        ON event_ledger (event_hash)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_event_ledger_no_update
    BEFORE UPDATE ON event_ledger
    BEGIN
        SELECT RAISE(ABORT, 'event_ledger is append-only: UPDATE denied');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_event_ledger_no_delete
    BEFORE DELETE ON event_ledger
    BEGIN
        SELECT RAISE(ABORT, 'event_ledger is append-only: DELETE denied');
    END
    """,
)


# §1.14 Artifact Store。Bytes は DB外のCASにあり、ここは Manifest だけを持つ。
# GCは payload を消して metadata を残す（AT-GC-001/RAW_RETENTION）ため、
# payload_deleted で表現する。Manifest自体はLedgerではないのでUPDATE可。
_MIGRATION_0002: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS artifact_manifest (
        artifact_id          TEXT    NOT NULL PRIMARY KEY,
        content_hash         TEXT    NOT NULL UNIQUE,
        media_type           TEXT    NOT NULL,
        size_bytes           INTEGER NOT NULL,
        data_classification  TEXT    NOT NULL,
        trust_level          TEXT    NOT NULL,
        stored_at            TEXT    NOT NULL,
        storage_path         TEXT    NOT NULL,
        payload_deleted      INTEGER NOT NULL DEFAULT 0,
        store_version        INTEGER NOT NULL DEFAULT 1,
        CHECK (size_bytes >= 0),
        CHECK (length(content_hash) = 71),
        CHECK (payload_deleted IN (0, 1))
    ) STRICT
    """,
)

# Core Schema #22 `MaskingReceipt`（ADR-007 §8）。
#
# **原文もマスク後本文も保存しない。** 保存するのはHashと構造化された判断だけ。
# 本文を持つとReceipt自体が漏洩経路になり、Receiptを消さないと消去要求へ
# 応えられなくなる。`MaskingReport.detail` も保存しない（自由文であり、
# 将来の変更で値が混ざり得る）。
#
# Receiptは証跡なので追記専用にする。Event Ledgerと同じ理由である。
_MIGRATION_0003: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS masking_receipt (
        masking_receipt_id                  TEXT    NOT NULL PRIMARY KEY,
        record_id                           TEXT    NOT NULL UNIQUE,
        run_id                              TEXT    NOT NULL,
        created_at                          TEXT    NOT NULL,
        producer                            TEXT    NOT NULL,
        content_hash                        TEXT    NOT NULL,
        source_content_hash                 TEXT    NOT NULL,
        source_normalized_hash              TEXT,
        masked_content_hash                 TEXT,
        normalization_profile               TEXT    NOT NULL,
        normalization_profile_artifact_hash TEXT    NOT NULL,
        masking_policy_version              INTEGER NOT NULL,
        policy_snapshot_hash                TEXT    NOT NULL,
        masking_result                      TEXT    NOT NULL,
        scan1_decision                      TEXT    NOT NULL,
        scan1_findings                      TEXT    NOT NULL,
        span_validation_result              TEXT,
        scan2_result                        TEXT,
        spans                               TEXT    NOT NULL,
        rejected_categories                 TEXT    NOT NULL,
        error_code                          TEXT,
        masker_provider                     TEXT,
        masker_model                        TEXT,
        masker_model_digest                 TEXT,
        masker_instruction_hash             TEXT,
        masker_invocation_count             INTEGER NOT NULL,
        rewriter_version                    TEXT    NOT NULL,
        store_version                       INTEGER NOT NULL,
        CHECK (masking_result IN ('CLEAN', 'MASKED', 'REJECTED')),
        CHECK (scan1_decision IN ('CLEAN', 'MASKABLE', 'REJECT', 'UNKNOWN')),
        CHECK (scan2_result IS NULL OR scan2_result IN ('ACCEPTED', 'REJECTED')),
        CHECK (span_validation_result IS NULL
               OR span_validation_result IN ('ACCEPTED', 'REJECTED')),
        CHECK (masker_invocation_count >= 0),
        CHECK (store_version >= 1)
    ) STRICT
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_masking_receipt_run
        ON masking_receipt (run_id, created_at)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_masking_receipt_no_update
    BEFORE UPDATE ON masking_receipt
    BEGIN
        SELECT RAISE(ABORT, 'masking_receipt is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_masking_receipt_no_delete
    BEFORE DELETE ON masking_receipt
    BEGIN
        SELECT RAISE(ABORT, 'masking_receipt is append-only');
    END
    """,
)

# MVP0-Aの承認・Lease・Effect・ActionAttempt。Ledger以外の現在状態はCASで更新するが、
# Effect Journalは履歴表へ毎遷移を追記して監査可能にする。
_MIGRATION_0004: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS approval_grant (
        grant_id                        TEXT    NOT NULL PRIMARY KEY,
        nonce                           TEXT    NOT NULL UNIQUE,
        plan_content_hash               TEXT    NOT NULL,
        execution_plan_hash             TEXT    NOT NULL,
        action_scope                    TEXT    NOT NULL,
        approver_subject_id             TEXT    NOT NULL,
        approver_tenant_id              TEXT    NOT NULL,
        authentication_context_class    TEXT    NOT NULL,
        mfa_performed                   INTEGER NOT NULL,
        authentication_time             TEXT    NOT NULL,
        issued_at                       TEXT    NOT NULL,
        not_before                      TEXT    NOT NULL,
        expires_at                      TEXT    NOT NULL,
        maximum_clock_skew_seconds      INTEGER NOT NULL,
        revocation_epoch                INTEGER NOT NULL,
        issuer_id                       TEXT    NOT NULL,
        issuer_key_id                   TEXT    NOT NULL,
        signature_algorithm             TEXT    NOT NULL,
        signature                       TEXT    NOT NULL,
        status                          TEXT    NOT NULL,
        store_version                   INTEGER NOT NULL,
        consumed_at                     TEXT,
        consumed_by_actor_id            TEXT,
        attempt_id                      TEXT,
        invalidated_at                  TEXT,
        invalidation_reason             TEXT,
        delegation_id                   TEXT,
        CHECK (mfa_performed IN (0, 1)),
        CHECK (status IN ('ISSUED', 'CONSUMED', 'EXPIRED', 'REVOKED', 'INVALIDATED')),
        CHECK (store_version >= 1),
        CHECK (maximum_clock_skew_seconds >= 0),
        CHECK (revocation_epoch >= 0),
        CHECK (length(plan_content_hash) = 71),
        CHECK (length(execution_plan_hash) = 71)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS lease (
        lease_id         TEXT    NOT NULL PRIMARY KEY,
        resource_key     TEXT    NOT NULL,
        holder_id        TEXT    NOT NULL,
        attempt_id       TEXT    NOT NULL,
        fencing_token    INTEGER NOT NULL,
        issued_at        TEXT    NOT NULL,
        expires_at       TEXT    NOT NULL,
        renewed_at       TEXT    NOT NULL,
        status           TEXT    NOT NULL,
        store_version    INTEGER NOT NULL,
        UNIQUE (resource_key, fencing_token),
        CHECK (fencing_token >= 1),
        CHECK (status IN ('ACTIVE', 'RELEASED', 'EXPIRED')),
        CHECK (store_version >= 1)
    ) STRICT
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_lease_current_resource
        ON lease (resource_key, fencing_token DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS action_attempt (
        attempt_id                   TEXT    NOT NULL PRIMARY KEY,
        action_id                    TEXT    NOT NULL,
        attempt_number               INTEGER NOT NULL,
        state                        TEXT    NOT NULL,
        plan_content_hash            TEXT,
        execution_plan_hash          TEXT,
        worker_id                    TEXT,
        claim_id                     TEXT,
        lease_id                     TEXT,
        fencing_token                INTEGER,
        runtime_attestation_hash     TEXT,
        operation_journal_id         TEXT,
        started_at                   TEXT,
        ended_at                     TEXT,
        receipt_ids                  TEXT    NOT NULL,
        error_classification         TEXT,
        store_version                INTEGER NOT NULL,
        UNIQUE (action_id, attempt_number),
        CHECK (attempt_number >= 1),
        CHECK ((plan_content_hash IS NULL) = (execution_plan_hash IS NULL)),
        CHECK (store_version >= 1)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS operation_journal (
        operation_journal_id         TEXT    NOT NULL PRIMARY KEY,
        operation_id                 TEXT    NOT NULL UNIQUE,
        effect_id                    TEXT    NOT NULL UNIQUE,
        run_id                       TEXT    NOT NULL,
        action_id                    TEXT    NOT NULL,
        attempt_id                   TEXT    NOT NULL,
        operation_type               TEXT    NOT NULL,
        target_resource_identity     TEXT    NOT NULL,
        fencing_token                INTEGER NOT NULL,
        before_hash                  TEXT    NOT NULL,
        expected_after_hash          TEXT    NOT NULL,
        prepared_event_id            TEXT    NOT NULL,
        prepared_at                  TEXT    NOT NULL,
        durability_level             TEXT    NOT NULL,
        state                        TEXT    NOT NULL,
        execution_attempted_event_id TEXT,
        execution_attempted_at       TEXT,
        observed_hash                TEXT,
        observation_method           TEXT,
        observed_at                  TEXT,
        receipt_id                   TEXT,
        error_classification         TEXT,
        store_version                INTEGER NOT NULL,
        CHECK (state IN ('PREPARED_DURABLE', 'EXECUTION_ATTEMPTED', 'EFFECT_OBSERVED',
                         'RECEIPT_DURABLE', 'EFFECT_UNKNOWN', 'EFFECT_CONFLICT')),
        CHECK (fencing_token >= 1),
        CHECK (store_version >= 1),
        CHECK (length(before_hash) = 71),
        CHECK (length(expected_after_hash) = 71)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS operation_journal_history (
        operation_journal_id  TEXT    NOT NULL,
        store_version         INTEGER NOT NULL,
        state                 TEXT    NOT NULL,
        journal_hash          TEXT    NOT NULL,
        recorded_at           TEXT    NOT NULL,
        PRIMARY KEY (operation_journal_id, store_version),
        FOREIGN KEY (operation_journal_id) REFERENCES operation_journal(operation_journal_id),
        CHECK (store_version >= 1),
        CHECK (length(journal_hash) = 71)
    ) STRICT
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_operation_journal_history_no_update
    BEFORE UPDATE ON operation_journal_history
    BEGIN
        SELECT RAISE(ABORT, 'operation_journal_history is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_operation_journal_history_no_delete
    BEFORE DELETE ON operation_journal_history
    BEGIN
        SELECT RAISE(ABORT, 'operation_journal_history is append-only');
    END
    """,
)

# Masking STARTED EventとLeaseの束縛。既存Receiptは歴史的証跡のため更新せず、
# 追加列はNULLを許容する。新規実行はApplication層とSchemaでstream_idを必須にする。
_MIGRATION_0005: Final[tuple[str, ...]] = (
    "ALTER TABLE masking_receipt ADD COLUMN stream_id TEXT",
    "ALTER TABLE masking_receipt ADD COLUMN attempt_id TEXT",
    "ALTER TABLE masking_receipt ADD COLUMN lease_id TEXT",
    """
    CREATE INDEX IF NOT EXISTS ix_masking_receipt_stream
        ON masking_receipt (stream_id, created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS masking_recovery_stream (
        stream_id   TEXT    NOT NULL PRIMARY KEY,
        run_id      TEXT    NOT NULL,
        attempt_id  TEXT,
        lease_id    TEXT,
        started_at  TEXT    NOT NULL,
        CHECK ((attempt_id IS NULL) = (lease_id IS NULL))
    ) STRICT
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_masking_recovery_stream_no_update
    BEFORE UPDATE ON masking_recovery_stream
    BEGIN
        SELECT RAISE(ABORT, 'masking_recovery_stream is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_masking_recovery_stream_no_delete
    BEFORE DELETE ON masking_recovery_stream
    BEGIN
        SELECT RAISE(ABORT, 'masking_recovery_stream is append-only');
    END
    """,
)

# Version → その版で適用する文。既適用分を再実行しないため版ごとに分ける。
# ---------------------------------------------------------------------------
# 6: 会話（Conversation@1.0.0 / ConversationMessage@2.0.0）
#
# 本文は列に持たない。Artifact CAS にあり、content_artifact_hash で参照する。
# conversation 側に件数列も message_ids 列も置かない。置けば Message 追加の
# たびに親を UPDATE することになり、Append-only と両立しない。
# ---------------------------------------------------------------------------
_MIGRATION_0006: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS conversation (
        conversation_id   TEXT    PRIMARY KEY,
        conversation_hash TEXT    NOT NULL,
        record_id         TEXT    NOT NULL,
        created_at        TEXT    NOT NULL,
        producer          TEXT    NOT NULL,
        content_hash      TEXT    NOT NULL,
        store_version     INTEGER NOT NULL,
        CHECK (store_version >= 1),
        CHECK (length(conversation_id) > 0)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS conversation_message (
        message_id            TEXT    PRIMARY KEY,
        conversation_id       TEXT    NOT NULL REFERENCES conversation(conversation_id),
        role                  TEXT    NOT NULL,
        sequence_number       INTEGER NOT NULL,
        content_artifact_hash TEXT    NOT NULL,
        record_id             TEXT    NOT NULL,
        created_at            TEXT    NOT NULL,
        producer              TEXT    NOT NULL,
        content_hash          TEXT    NOT NULL,
        store_version         INTEGER NOT NULL,
        UNIQUE (conversation_id, sequence_number),
        CHECK (store_version >= 1),
        CHECK (sequence_number >= 1),
        CHECK (role IN (
            'SYSTEM_CONTROL', 'DEVELOPER_CONTROL', 'USER_TASK', 'TOOL_DEFINITION',
            'VERIFIED_REFERENCE_DATA', 'UNTRUSTED_ARTIFACT_DATA', 'UNTRUSTED_PROVIDER_DATA'
        ))
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS conversation_message_by_sequence
        ON conversation_message (conversation_id, sequence_number);
    """,
)


# ---------------------------------------------------------------------------
# 7: 会話 Snapshot（ConversationSnapshot@1.0.0）
#
# 本文も要約も列に持たない。参照は message_set_hash だけである。
# 再生成は新しい行として足す。既存行を UPDATE しない。
# ---------------------------------------------------------------------------
_MIGRATION_0007: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS conversation_snapshot (
        snapshot_id      TEXT    PRIMARY KEY,
        conversation_id  TEXT    NOT NULL REFERENCES conversation(conversation_id),
        snapshot_hash    TEXT    NOT NULL,
        message_set_hash TEXT    NOT NULL,
        schema_set_hash  TEXT    NOT NULL,
        design_sha256    TEXT    NOT NULL,
        record_id        TEXT    NOT NULL,
        created_at       TEXT    NOT NULL,
        producer         TEXT    NOT NULL,
        content_hash     TEXT    NOT NULL,
        store_version    INTEGER NOT NULL,
        CHECK (store_version >= 1),
        CHECK (length(snapshot_id) > 0)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS conversation_snapshot_by_conversation
        ON conversation_snapshot (conversation_id, created_at);
    """,
)


_MIGRATION_0008: Final[tuple[str, ...]] = (
    """CREATE TABLE local_workflow_run (
        run_id TEXT PRIMARY KEY, state TEXT NOT NULL, version INTEGER NOT NULL,
        document_hash TEXT NOT NULL, CHECK(version >= 1), CHECK(length(document_hash) = 71)
    ) STRICT""",
)

_MIGRATION_0009: Final[tuple[str, ...]] = (
    (
        "CREATE TABLE approval_consume_group (grant_id TEXT PRIMARY KEY, concurrency_group "
        "TEXT NOT NULL UNIQUE) STRICT"
    ),
    (
        "CREATE TABLE approval_consume_ticket (ticket_id TEXT PRIMARY KEY, grant_id TEXT NOT "
        "NULL, attempt_id TEXT NOT NULL, concurrency_group TEXT NOT NULL, request_hash TEXT "
        "NOT NULL, state TEXT NOT NULL CHECK(state IN ('ADMITTED','CONSUMED','REJECTED')), "
        "UNIQUE(grant_id, attempt_id)) STRICT"
    ),
    (
        "CREATE UNIQUE INDEX approval_consume_one_winner ON "
        "approval_consume_ticket(concurrency_group) WHERE state = 'CONSUMED'"
    ),
    (
        "CREATE TABLE approval_consume_result (consume_result_id TEXT PRIMARY KEY, ticket_id "
        "TEXT NOT NULL UNIQUE, content_hash TEXT NOT NULL) STRICT"
    ),
    (
        "CREATE TRIGGER approval_consume_result_no_update BEFORE UPDATE ON "
        "approval_consume_result BEGIN SELECT RAISE(ABORT, 'approval consume result is append "
        "only'); END"
    ),
    (
        "CREATE TRIGGER approval_consume_result_no_delete BEFORE DELETE ON "
        "approval_consume_result BEGIN SELECT RAISE(ABORT, 'approval consume result is append "
        "only'); END"
    ),
)

# CLI Workbench Preview（Owner明示依頼の拡張）。**既存Core Schema名を名乗らない。**
#
# 送信側Journalを既存 `operation_journal` へ相乗りさせない。外部生成には送信前に
# 既知の `expected_after_hash` が無く、同じ表へ入れると実測と期待の意味が壊れる。
# `session_id` をUNIQUEにして、同じSessionから2つ目の起動が作られないようにする。
# `record_hash` は行の改ざん検知用であり、読み戻し時に必ず再計算して照合する。
_MIGRATION_0010: Final[tuple[str, ...]] = (
    """
    CREATE TABLE workbench_session (
        session_id     TEXT    NOT NULL PRIMARY KEY,
        state          TEXT    NOT NULL,
        version        INTEGER NOT NULL,
        document_hash  TEXT    NOT NULL,
        CHECK (version >= 1),
        CHECK (length(document_hash) = 71)
    ) STRICT
    """,
    """
    CREATE TABLE cli_invocation_journal (
        invocation_id       TEXT    NOT NULL PRIMARY KEY,
        session_id          TEXT    NOT NULL UNIQUE,
        execution_plan_hash TEXT    NOT NULL,
        request_hash        TEXT    NOT NULL,
        runtime_hash        TEXT    NOT NULL,
        state               TEXT    NOT NULL,
        response_hash       TEXT,
        store_version       INTEGER NOT NULL,
        record_hash         TEXT    NOT NULL,
        CHECK (state IN ('PREPARED_DURABLE','EXECUTION_ATTEMPTED',
                         'RESPONSE_CAPTURED','EFFECT_UNKNOWN')),
        CHECK (store_version >= 0),
        CHECK (length(execution_plan_hash) = 71),
        CHECK (length(request_hash) = 71),
        CHECK (length(runtime_hash) = 71),
        CHECK (length(record_hash) = 71),
        CHECK (response_hash IS NULL OR length(response_hash) = 71),
        CHECK ((state = 'RESPONSE_CAPTURED') = (response_hash IS NOT NULL))
    ) STRICT
    """,
)


# 内部運用状態も同じSQLite DBに置く。公開Core Schemaの版とは独立したMigration。
_MIGRATION_0011: Final[tuple[str, ...]] = (
    "CREATE TABLE operation_control (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "mode TEXT NOT NULL CHECK(mode IN ('OPEN','DRAINING','RESTORING')), "
    "version INTEGER NOT NULL CHECK(version>=1), "
    "refusals INTEGER NOT NULL CHECK(refusals>=0)) STRICT",
    "INSERT INTO operation_control VALUES (1,'OPEN',1,0)",
    "CREATE TABLE operation_admission (token TEXT PRIMARY KEY, "
    "kind TEXT NOT NULL CHECK(kind IN ('intake','effect'))) STRICT",
)

# Ownership records do not infer missing legacy owners during upgrade.
_MIGRATION_0012: Final[tuple[str, ...]] = (
    "CREATE TABLE operation_admission_owner (token TEXT PRIMARY KEY REFERENCES "
    "operation_admission(token) ON DELETE CASCADE, request_id TEXT NOT NULL, "
    "owner_scope TEXT NOT NULL) STRICT",
    "CREATE TABLE operation_restore (token TEXT PRIMARY KEY, owner_scope TEXT NOT NULL, "
    "purpose TEXT NOT NULL, source_hash TEXT NOT NULL, status TEXT NOT NULL "
    "CHECK(status IN ('ACTIVE','FAILED','COMPLETED','ABANDONED'))) STRICT",
    "CREATE UNIQUE INDEX one_pending_restore ON operation_restore ((1)) "
    "WHERE status IN ('ACTIVE','FAILED')",
)

# User form preferences are not design answers or execution approvals.
_MIGRATION_0013: Final[tuple[str, ...]] = (
    "CREATE TABLE workbench_preference (scope_hash TEXT PRIMARY KEY NOT NULL, "
    "provider_id TEXT NOT NULL, model_id TEXT NOT NULL, reasoning_effort TEXT, "
    "version INTEGER NOT NULL CHECK(version >= 1), content_hash TEXT NOT NULL, "
    "CHECK(length(scope_hash)=71), CHECK(length(content_hash)=71)) STRICT",
)

# Registration identity only. Never persist OAuth credentials.
_MIGRATION_0014: Final[tuple[str, ...]] = (
    "CREATE TABLE chatgpt_registration (name TEXT PRIMARY KEY NOT NULL "
    "CHECK(name IN ('host_id','client_id','subject_hash')), value TEXT NOT NULL "
    "CHECK(length(value) BETWEEN 1 AND 512)) STRICT",
)

_MIGRATIONS: Final[tuple[tuple[int, tuple[str, ...]], ...]] = (
    (1, _MIGRATION_0001),
    (2, _MIGRATION_0002),
    (3, _MIGRATION_0003),
    (4, _MIGRATION_0004),
    (5, _MIGRATION_0005),
    (6, _MIGRATION_0006),
    (7, _MIGRATION_0007),
    (8, _MIGRATION_0008),
    (9, _MIGRATION_0009),
    (10, _MIGRATION_0010),
    (11, _MIGRATION_0011),
    (12, _MIGRATION_0012),
    (13, _MIGRATION_0013),
    (14, _MIGRATION_0014),
)


def migrate(factory: ConnectionFactory, *, recorded_at: str) -> int:
    """未適用のMigrationだけを適用し、適用後のSchema Versionを返す。

    Migration専用接続を使う。Runtime接続からSchemaを変更しない。
    全体を単一Transactionで適用し、途中失敗を残さない。
    """
    connection = factory.connect(ConnectionRole.MIGRATION)
    try:
        with factory.begin_immediate(connection):
            current = _current_version(connection)
            if current > SCHEMA_VERSION:
                raise HarnessError(
                    ErrorCode.MIGRATION_FAILED,
                    f"database schema version {current} is newer than "
                    f"supported {SCHEMA_VERSION}; refusing to downgrade",
                )
            if current == SCHEMA_VERSION:
                return current
            for version, statements in _MIGRATIONS:
                if version <= current:
                    continue
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migration (version, applied_at) VALUES (?, ?)",
                    (version, recorded_at),
                )
        return SCHEMA_VERSION
    finally:
        connection.close()


def _current_version(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migration'"
    ).fetchone()
    if row is None:
        return 0
    version_row = connection.execute(
        "SELECT MAX(version) AS version FROM schema_migration"
    ).fetchone()
    version = version_row["version"] if version_row is not None else None
    return int(version) if version is not None else 0
