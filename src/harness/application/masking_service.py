"""マスキング実行のApplication Service（ADR-007 §8、Core Schema #22）。

Pipelineは純粋な判定であり、DB副作用を持たない。永続化とEvent発行は
すべてここで行う。

## Transaction境界を2つに分ける

    Transaction 1: INPUT_MASKING_STARTED を追記してCommit
    （Transaction外）: Pipeline実行。Maskerとの往復を含む
    Transaction 2: 中間Event＋Receipt保存＋終端Event を BEGIN IMMEDIATE で一括

### なぜSTARTEDを先にCommitするか

Maskerの呼出しは外部Processとの往復であり、そこでProcessが落ちると
「Raw PIIを渡したのに記録が無い」状態になる。渡していないのか記録が
残らなかったのかを後から区別できない。先にDurable化しておけば、
STARTEDだけが残っている実行を「Masker呼出し中に停止した」と判定できる
（`find_incomplete_streams()`）。

### なぜMasker呼出し中にTransactionを開かないか

`BEGIN IMMEDIATE` は書込みロックを取る。外部呼出しの応答時間には上限が
無い（Timeoutを付けても最悪値はTimeout値になる）。その間ずっとDBを掴むと、
他の実行が進めなくなる。Transactionは「自分で完結できる処理」だけで閉じる。

### なぜReceiptと終端Eventを同一Transactionにするか

片方だけが残る状態を作らないため。Receiptがあって終端Eventが無ければ
Ledgerから見て実行が終わっていないのにReceiptがある。逆ならReceiptの無い
完了記録になる。どちらも後から真偽を決められない。
"""

from __future__ import annotations

from dataclasses import dataclass

# §1.11 のCanonical Bytes。Receipt／Event PayloadのHash入力はこれを通す。
from harness.domain.canonical import canonicalize
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_bytes
from harness.domain.masking_result import MaskingReport
from harness.domain.recovery import MaskingRecoveryDecision, classify_masking_recovery_stream
from harness.ports.event_ledger import EventLedgerPort, NewEvent
from harness.ports.lease import LeasePort
from harness.ports.masking import (
    MaskingPipelinePort,
    MaskingReceiptPort,
    MaskingReceiptRecord,
    MaskingRecoveryPort,
)
from harness.ports.unit_of_work import UnitOfWorkPort

__all__ = ["MaskingExecution", "MaskingExecutionRequest", "MaskingService"]

_STARTED = "INPUT_MASKING_STARTED"
_SCAN1_READY = "INPUT_MASKING_SCAN1_CANDIDATES_READY"
_SPANS_PROPOSED = "INPUT_MASKING_SPANS_PROPOSED"
_COMPLETED = "INPUT_MASKING_COMPLETED"
_REJECTED = "INPUT_MASKING_REJECTED"

_TERMINAL_EVENTS = frozenset({_COMPLETED, _REJECTED})


@dataclass(frozen=True, slots=True)
class MaskingExecutionRequest:
    """1回の実行要求。

    `source_content_hash` は §1.16.2 の `ReadEvidence` から引き継ぐ。
    Serviceが本文から計算し直さないのは、**読取ったものとマスクしたものが
    同じであること**を呼出側の証跡へ束縛するためである。ここで計算すると
    「Serviceが受け取った本文のHash」にしかならず、読取り経路とは繋がらない。
    """

    masking_receipt_id: str
    run_id: str
    stream_id: str
    text: str
    source_content_hash: str
    recorded_at: str
    producer: str
    attempt_id: str | None = None
    lease_id: str | None = None


@dataclass(frozen=True, slots=True)
class MaskingExecution:
    """実行結果。`report.masked_text` はメモリ上のみで、DBには無い。"""

    report: MaskingReport
    receipt_id: str
    terminal_event: str


class MaskingService:
    def __init__(
        self,
        *,
        pipeline: MaskingPipelinePort,
        ledger: EventLedgerPort,
        receipts: MaskingReceiptPort,
        recovery: MaskingRecoveryPort,
        unit_of_work: UnitOfWorkPort,
        policy_snapshot_hash: str,
    ) -> None:
        # **同一Transaction文脈であることを組立て時に確認する。**
        # 別々のDB接続にまたがると同一Transactionへ入らず、片方だけが
        # Commitされる。実行してから気付いても、そのときには片側が確定済み。
        #
        # Application層は `sqlite3.Connection` を知らない（CLAUDE.md §2）。
        # 不透明な識別子の一致だけで判定する。
        ledger_identity = getattr(ledger, "transaction_identity", None)
        if ledger_identity is None:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "event ledger repository does not expose a transaction identity",
            )
        if ledger_identity() is not receipts.transaction_identity():
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "ledger and receipt repositories must share one transaction context; "
                "separate connections cannot participate in one transaction",
            )
        self._pipeline = pipeline
        self._ledger = ledger
        self._receipts = receipts
        self._recovery = recovery
        if not policy_snapshot_hash.startswith("sha256:") or len(policy_snapshot_hash) != 71:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH, "invalid masking policy snapshot hash"
            )
        self._unit_of_work = unit_of_work
        self._policy_snapshot_hash = policy_snapshot_hash

    # ------------------------------------------------------------------

    def execute(self, request: MaskingExecutionRequest) -> MaskingExecution:
        if (request.attempt_id is None) != (request.lease_id is None):
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "attempt_id and lease_id must be both set or both absent",
            )
        head = self._ledger.stream_head(request.stream_id)

        # --- Transaction 1: STARTED をDurable化 ---------------------------
        # ここが失敗したらMaskerを呼ばない。呼んでしまうと、Raw PIIが
        # 渡ったのに記録が無い状態になる。
        with self._unit_of_work.begin_immediate():
            self._ledger.append(
                [
                    self._event(
                        request, _STARTED, {"masking_receipt_id": request.masking_receipt_id}
                    )
                ],
                expected_stream_sequence=head,
            )
            self._recovery.record_started_stream(
                stream_id=request.stream_id,
                run_id=request.run_id,
                attempt_id=request.attempt_id,
                lease_id=request.lease_id,
                started_at=request.recorded_at,
            )
        head += 1

        # --- Transaction 外: Pipeline実行 ---------------------------------
        # Transactionを開いたまま外部呼出しへ入っていないことを確認する。
        # `BEGIN IMMEDIATE` は書込みロックを取るため、Maskerの応答を
        # 待つ間ずっとDBを掴むことになる。
        if self._unit_of_work.in_transaction():
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "a transaction is open before the masker call; "
                "external calls must not hold the write lock",
            )
        report = self._pipeline.run(request.text)

        # --- Transaction 2: 中間Event＋Receipt＋終端Event ------------------
        events = self._intermediate_events(request, report)
        terminal = _REJECTED if report.rejected else _COMPLETED
        events.append(self._event(request, terminal, self._terminal_payload(report)))
        receipt = self._build_receipt(request, report)

        with self._unit_of_work.begin_immediate():
            self._receipts.save(receipt)
            self._ledger.append(events, expected_stream_sequence=head)

        return MaskingExecution(
            report=report, receipt_id=receipt.masking_receipt_id, terminal_event=terminal
        )

    # ------------------------------------------------------------------

    def classify_incomplete_streams(
        self, *, leases: LeasePort, now: str
    ) -> tuple[MaskingRecoveryDecision, ...]:
        """未終端Masking StreamをLease期限で分類する。

        ACTIVE Leaseは実行中とみなし、期限切れ・束縛欠落・束縛不一致を
        明示的に分離する。終端Eventを持たないことだけで停止と断定しない。
        """
        return tuple(
            classify_masking_recovery_stream(
                stream_id=stream.stream_id,
                run_id=stream.run_id,
                attempt_id=stream.attempt_id,
                lease_id=stream.lease_id,
                lease=None if stream.lease_id is None else leases.get(stream.lease_id),
                now=now,
            )
            for stream in self._recovery.find_incomplete_streams()
        )

    def expired_recovery_candidates(
        self, *, leases: LeasePort, now: str
    ) -> tuple[MaskingRecoveryDecision, ...]:
        """実Recovery照合へ渡してよい期限切れStreamだけを返す。"""
        return tuple(
            decision
            for decision in self.classify_incomplete_streams(leases=leases, now=now)
            if decision.automatic_recovery_allowed
        )

    # ------------------------------------------------------------------

    def _event(
        self, request: MaskingExecutionRequest, event_type: str, payload: dict[str, object]
    ) -> NewEvent:
        """Event Payloadは**Hashだけ**をLedgerへ載せる。

        本文はもちろん、`MaskingReport.detail` も入れない。
        """
        return NewEvent(
            stream_id=request.stream_id,
            event_type=event_type,
            payload_hash=hash_bytes(canonicalize(payload)),
            recorded_at=request.recorded_at,
        )

    def _intermediate_events(
        self, request: MaskingExecutionRequest, report: MaskingReport
    ) -> list[NewEvent]:
        """到達した段階だけEventを出す。出していない段階は「起きていない」。

        `SCAN1_CANDIDATES_READY` は **候補が揃ってMaskerへ進める** ことを指す。
        Scan#1がREJECTした場合、候補は1件も作られずMaskerへも進まないため
        出さない。ここを「Scan#1が走った」の意味で出すと、Event列を読んだ
        側が「候補があったのに拒否された」と読み違える。
        """
        events: list[NewEvent] = []
        trace = report.trace
        if trace.scan1_decision in ("CLEAN", "MASKABLE"):
            events.append(
                self._event(
                    request,
                    _SCAN1_READY,
                    {
                        "scan1_decision": trace.scan1_decision,
                        "finding_count": len(report.findings),
                    },
                )
            )
        if report.masker_invocation_count > 0:
            events.append(
                self._event(
                    request,
                    _SPANS_PROPOSED,
                    {"span_count": len(trace.spans)},
                )
            )
        return events

    @staticmethod
    def _terminal_payload(report: MaskingReport) -> dict[str, object]:
        return {
            "masking_result": report.result,
            "rejected_categories": list(report.rejected_categories),
            "error_code": report.error_code.value if report.error_code else None,
            "span_count": report.span_count,
        }

    def _build_receipt(
        self, request: MaskingExecutionRequest, report: MaskingReport
    ) -> MaskingReceiptRecord:
        trace = report.trace
        # 型注釈を明示する。`dict[str, int | str]` と `dict[str, object]` は
        # 不変であり、推論に任せると代入できない。
        findings: tuple[dict[str, object], ...] = tuple(
            {
                "rule_id": finding.rule_id,
                "category": finding.category,
                "disposition": finding.disposition.value,
                "count": finding.count,
            }
            for finding in report.findings
        )
        spans: tuple[dict[str, object], ...] = tuple(
            {"start": span.start, "end": span.end, "category": span.category}
            for span in trace.spans
        )
        masker = trace.masker
        body: dict[str, object] = {
            "masking_receipt_id": request.masking_receipt_id,
            "run_id": request.run_id,
            "stream_id": request.stream_id,
            "attempt_id": request.attempt_id,
            "lease_id": request.lease_id,
            "source_content_hash": request.source_content_hash,
            "source_normalized_hash": report.source_normalized_hash,
            "masked_content_hash": report.masked_content_hash,
            "normalization_profile": trace.normalization_profile,
            "normalization_profile_artifact_hash": trace.normalization_profile_artifact_hash,
            "policy_snapshot_hash": self._policy_snapshot_hash,
            "masking_policy_version": trace.masking_policy_version,
            "masking_result": report.result,
            "scan1_decision": trace.scan1_decision,
            "scan1_findings": list(findings),
            "span_validation_result": trace.span_validation_result,
            "scan2_result": trace.scan2_result,
            "spans": list(spans),
            "rejected_categories": list(report.rejected_categories),
            "error_code": report.error_code.value if report.error_code else None,
            "rewriter_version": trace.rewriter_version,
        }
        # Receipt自身のContent Hash。`detail`も本文も入っていない値から作る。
        content_hash = str(hash_bytes(canonicalize(body)))
        return MaskingReceiptRecord(
            masking_receipt_id=request.masking_receipt_id,
            record_id=request.masking_receipt_id,
            run_id=request.run_id,
            stream_id=request.stream_id,
            attempt_id=request.attempt_id,
            lease_id=request.lease_id,
            created_at=request.recorded_at,
            producer=request.producer,
            content_hash=content_hash,
            source_content_hash=request.source_content_hash,
            source_normalized_hash=report.source_normalized_hash,
            masked_content_hash=report.masked_content_hash,
            normalization_profile=trace.normalization_profile,
            normalization_profile_artifact_hash=trace.normalization_profile_artifact_hash,
            masking_policy_version=trace.masking_policy_version,
            policy_snapshot_hash=self._policy_snapshot_hash,
            masking_result=report.result,
            scan1_decision=trace.scan1_decision,
            scan1_findings=findings,
            span_validation_result=trace.span_validation_result,
            scan2_result=trace.scan2_result,
            spans=spans,
            rejected_categories=report.rejected_categories,
            error_code=report.error_code.value if report.error_code else None,
            masker_provider=masker.provider if masker else None,
            masker_model=masker.model if masker else None,
            masker_model_digest=masker.model_digest if masker else None,
            masker_instruction_hash=masker.instruction_hash if masker else None,
            masker_invocation_count=report.masker_invocation_count,
            rewriter_version=trace.rewriter_version,
        )
