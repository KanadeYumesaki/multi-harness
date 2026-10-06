"""`MaskingReceipt`（Core Schema #22）のSQLite Repository。

不変条件#15に従い`commit()`しない。呼出側のUnit of Workが開いた
`BEGIN IMMEDIATE` Transactionの内側で実行される前提とする。

## 本文を持たない

保存するのはHashと構造化された判断だけである。原文もマスク後本文も、
`MaskingReport.detail` も持たない。Receiptが本文を持つと、
Receipt自体が漏洩経路になり、消去要求へ応えるにはReceiptごと消すしかなくなる。
証跡は消せてはならないので、そもそも入れない。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

from harness.domain.errors import ErrorCode, HarnessError
from harness.infrastructure.schema.registry import CoreSchemaRegistry
from harness.infrastructure.sqlite.transaction_guard import require_transaction
from harness.ports.masking import MaskingReceiptRecord

__all__ = ["MaskingReceiptRecord", "SqliteMaskingReceiptRepository"]


class SqliteMaskingReceiptRepository:
    def __init__(
        self, connection: sqlite3.Connection, schema_registry: CoreSchemaRegistry | None = None
    ) -> None:
        self._connection = connection
        self._schema_registry = schema_registry or CoreSchemaRegistry.bundled()

    def transaction_identity(self) -> object:
        """`Transactional`。同一Transaction文脈の識別子。

        Application層は`sqlite3.Connection`を知ってはならないため、
        比較にだけ使える不透明な値として返す。
        """
        return self._connection

    def save(self, receipt: MaskingReceiptRecord) -> None:
        """1件保存する。重複IDは`STORAGE_WRITE_FAILED`。

        `INSERT OR REPLACE` を使わない。同じIDに別の内容を書けると、
        どちらが本物か決められなくなる。
        """
        require_transaction(self._connection, "masking receipt save")
        self._schema_registry.validate_or_raise(
            "MaskingReceipt", "1.0.0", _schema_document(receipt)
        )
        try:
            self._connection.execute(
                """
                INSERT INTO masking_receipt (
                    masking_receipt_id, record_id, run_id, stream_id, attempt_id, lease_id,
                    created_at, producer, content_hash, source_content_hash, source_normalized_hash,
                    masked_content_hash, normalization_profile,
                    normalization_profile_artifact_hash, masking_policy_version,
                    policy_snapshot_hash, masking_result, scan1_decision,
                    scan1_findings, span_validation_result, scan2_result, spans,
                    rejected_categories, error_code, masker_provider, masker_model,
                    masker_model_digest, masker_instruction_hash,
                    masker_invocation_count, rewriter_version, store_version
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    receipt.masking_receipt_id,
                    receipt.record_id,
                    receipt.run_id,
                    receipt.stream_id,
                    receipt.attempt_id,
                    receipt.lease_id,
                    receipt.created_at,
                    receipt.producer,
                    receipt.content_hash,
                    receipt.source_content_hash,
                    receipt.source_normalized_hash,
                    receipt.masked_content_hash,
                    receipt.normalization_profile,
                    receipt.normalization_profile_artifact_hash,
                    receipt.masking_policy_version,
                    receipt.policy_snapshot_hash,
                    receipt.masking_result,
                    receipt.scan1_decision,
                    _dump(receipt.scan1_findings),
                    receipt.span_validation_result,
                    receipt.scan2_result,
                    _dump(receipt.spans),
                    _dump(receipt.rejected_categories),
                    receipt.error_code,
                    receipt.masker_provider,
                    receipt.masker_model,
                    receipt.masker_model_digest,
                    receipt.masker_instruction_hash,
                    receipt.masker_invocation_count,
                    receipt.rewriter_version,
                    receipt.store_version,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HarnessError(
                ErrorCode.STORAGE_WRITE_FAILED,
                f"masking receipt already exists or violates a constraint: "
                f"{receipt.masking_receipt_id}",
            ) from exc

    def exists(self, masking_receipt_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM masking_receipt WHERE masking_receipt_id = ?",
            (masking_receipt_id,),
        ).fetchone()
        return row is not None


def _schema_document(receipt: MaskingReceiptRecord) -> dict[str, object]:
    """DTOをSchema検証用JSON互換Documentへ射影する。"""
    document = asdict(receipt)
    document["schema_name"] = "MaskingReceipt"
    document["schema_version"] = "1.0.0"
    for key in ("scan1_findings", "spans", "rejected_categories"):
        document[key] = list(document[key])
    return {key: value for key, value in document.items() if value is not None}


def _dump(value: object) -> str:
    """Canonicalに近い決定的なJSONにする。

    Key順を固定し、空白を詰める。Receiptの`content_hash`は別途
    Canonical JSONで算出するが、保存文字列自体も比較可能にしておく。
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
