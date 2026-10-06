"""マスキング実行に関わるPort群（CLAUDE.md §2）。

Application層はここだけを見る。`infrastructure/` の具象を直接importしない。

## `Transactional` を置く理由

Receipt保存とEvent追記は同一Transactionで行う必要がある。別々のDB接続に
またがると同一Transactionへ入らず、片方だけがCommitされる形が成立する。

しかしApplication層は`sqlite3.Connection`を知ってはならない。そこで
「同じTransaction文脈に属するか」だけを**不透明な識別子の一致**で
判定できるようにする。Application層は識別子の中身を解釈しない。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from harness.domain.masking_result import MaskingReport

__all__ = [
    "MaskingPipelinePort",
    "MaskingPolicyDenial",
    "MaskingPolicyGatePort",
    "MaskingReceiptPort",
    "MaskingReceiptRecord",
    "MaskingRecoveryPort",
    "MaskingRecoveryStream",
    "Transactional",
]

_STORE_VERSION = 1


@dataclass(frozen=True, slots=True)
class MaskingReceiptRecord:
    """保存するReceipt 1件。

    Fieldは`schemas/core/MaskingReceipt/1.0.0.schema.json`に対応する。
    `ports/event_ledger.py` の `NewEvent` と同じく、Port操作のDTOとして
    ここに置く。

    **本文を持たない。** 原文もマスク後本文も`MaskingReport.detail`も
    入れない。Receiptが本文を持つとReceipt自体が漏洩経路になり、
    消去要求へ応えるにはReceiptごと消すしかなくなる。証跡は消せては
    ならないので、そもそも入れない。
    """

    masking_receipt_id: str
    record_id: str
    run_id: str
    stream_id: str
    attempt_id: str | None
    lease_id: str | None
    created_at: str
    producer: str
    content_hash: str
    source_content_hash: str
    source_normalized_hash: str | None
    masked_content_hash: str | None
    normalization_profile: str
    normalization_profile_artifact_hash: str
    masking_policy_version: int
    policy_snapshot_hash: str
    masking_result: str
    scan1_decision: str
    scan1_findings: tuple[dict[str, object], ...]
    span_validation_result: str | None
    scan2_result: str | None
    spans: tuple[dict[str, object], ...]
    rejected_categories: tuple[str, ...]
    error_code: str | None
    masker_provider: str | None
    masker_model: str | None
    masker_model_digest: str | None
    masker_instruction_hash: str | None
    masker_invocation_count: int
    rewriter_version: str
    store_version: int = _STORE_VERSION


class Transactional(Protocol):
    def transaction_identity(self) -> object:
        """同一Transaction文脈を表す不透明な識別子。

        比較にだけ使う。Application層は中身を解釈しない。
        """
        ...


class MaskingPipelinePort(Protocol):
    def run(self, text: str) -> MaskingReport:
        """マスキング判定を行う。**DB副作用を持たない。**

        例外を外へ出さない。想定外の失敗もREJECTとして返す
        （`never_pass_unmasked`）。
        """
        ...


class MaskingReceiptPort(Transactional, Protocol):
    def save(self, receipt: MaskingReceiptRecord) -> None:
        """Receiptを1件保存する。Transactionの内側で呼ばれる前提。

        重複IDは`STORAGE_WRITE_FAILED`。`INSERT OR REPLACE`はしない。
        同じIDに別の内容を書けると、どちらが本物か決められなくなる。
        """
        ...

    def exists(self, masking_receipt_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class MaskingRecoveryStream:
    """STARTEDがDurableだが終端Eventを持たないMasking Streamの束縛情報。"""

    stream_id: str
    run_id: str | None
    attempt_id: str | None
    lease_id: str | None


class MaskingRecoveryPort(Transactional, Protocol):
    def record_started_stream(
        self,
        *,
        stream_id: str,
        run_id: str,
        attempt_id: str | None,
        lease_id: str | None,
        started_at: str,
    ) -> None:
        """STARTED Eventと同一TransactionでStream→Lease束縛を追記する。"""
        ...

    def find_incomplete_streams(self) -> tuple[MaskingRecoveryStream, ...]:
        """開始Eventはあるが終端Eventが無いStreamとLease束縛を返す。

        これは未終端の検出であり、期限切れ判断はApplication層がLease Portを
        通じて行う。束縛のない旧Streamは自動Recoveryを許可しない。
        """
        ...


@dataclass(frozen=True, slots=True)
class MaskingPolicyDenial:
    """Masking Policy が「渡さない」と決めた記録。

    **Error Code を持たない。** Policy 境界による拒否は仕様どおりの動作であって
    失敗ではない（設計書 v1.17 §1.16.3.2 / Owner Decision MASK-5-C）。
    ここへ Code を付けると、「仕様どおり止まった」と「検証が失敗した」が
    同じ形で記録され、運用で区別できなくなる。

    **Bytes も原文も持たない。** 何が見つかったかは分類名だけで表す。
    """

    rejected_categories: tuple[str, ...]
    masker_invocation_count: int


class MaskingPolicyGatePort(Protocol):
    """分類済みの入力を Masking Policy へ通す関門（§1.16.2.3）。

    Masking Pipeline は判定を返すだけで Ledger を知らない。その判定を
    Input Read の判定へ引き取るのは Orchestration の仕事である。
    """

    def evaluate(
        self, payload: bytes, classification: str | None
    ) -> MaskingPolicyDenial | None: ...
