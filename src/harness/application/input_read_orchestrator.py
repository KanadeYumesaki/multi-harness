"""§1.16.2.1 Input Read Orchestrator。Ledger責務はここだけが持つ。

## なぜOrchestratorが要るか

`SafeInputReader`は判定を返すだけで、Ledgerへ何も残さない。判定した事実が
どこにも残らないと、後から「なぜ読めなかったか」を示せない。判定する層と
記録する層を分け、記録の有無で判定が変わらないようにする。

Readerへ`EventLedgerPort`を渡さない。渡すとInfrastructure層がTransaction境界を
持つことになり、不変条件#15「Transaction所有者はApplication層のUnit of Work」と
整合しなくなる。

## 単一Transactionにする理由

`masking_service`は2 Transactionへ分け、外部呼出しの前に`in_transaction()`を
確認する。Maskerは外部Processであり応答時間に上限が無いためで、かつ
**Raw PIIを渡した事実を渡す前にDurable化する**必要があるからである。

Input Readは事情が違う。

* 読取は外部Effectを伴わない（§1.16.2.1）。「Effectは起きたが記録が無い」が
  起こらないので、先にDurable化する理由が無い
* `O_NONBLOCK`とSize／Depth／Read Time上限があり、応答時間に上限がある
* STARTEDだけをCommitしてReaderが落ちると、終端Eventの無い**Partial Event**が
  Ledgerへ残る。単一Transactionならその状態を作らない

したがって Append 2箇所を単一Transactionへ入れる。Readerが落ちればRollbackし、
Ledgerには何も残らない。「観測されたものだけを残す」（§19.1.1）と一致する。

## 重複Append防止

`expected_stream_sequence`のCASで防ぐ。同じRequestを2度実行すると、2度目は
Headが進んでいるためCASが外れて`EVENT_ORDER_VIOLATION`になる。Rollback後の
再実行はHeadが動いていないので通る。
"""

from __future__ import annotations

from typing import Protocol

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.events import EventType
from harness.domain.hashing import ContentHash, hash_canonical
from harness.domain.input_read import ReadDecision, ReadDenial
from harness.ports.effect_execution import ClockPort
from harness.ports.event_ledger import EventLedgerPort, NewEvent
from harness.ports.masking import MaskingPolicyDenial, MaskingPolicyGatePort
from harness.ports.safe_input_reader import CandidateEnumerationPort, FileReadPort, ReadEvidence
from harness.ports.task_intake import InputReadOutcome, InputReadRequest
from harness.ports.unit_of_work import UnitOfWorkPort

__all__ = [
    "InputArtifactClassifierPort",
    "InputReadOrchestrator",
    "InputReadOutcome",
    "InputReadRequest",
    "MaskingPolicyDenial",
    "MaskingPolicyGatePort",
]

_EVENT_ARTIFACT_TYPE = "input-read-event"
_EVENT_SCHEMA_MAJOR = 1
_SUBJECT_ARTIFACT_TYPE = "input-read-subject"


class InputArtifactClassifierPort(Protocol):
    """読取れたBytesの分類。分類できたときだけ`INPUT_ARTIFACT_CLASSIFIED`を出す。"""

    def classify(self, payload: bytes, evidence: ReadEvidence) -> str: ...


class InputReadOrchestrator:
    """読取判定とLedger記録を同一Transactionで束ねる。"""

    def __init__(
        self,
        *,
        reader: FileReadPort,
        enumerator: CandidateEnumerationPort | None = None,
        ledger: EventLedgerPort,
        unit_of_work: UnitOfWorkPort,
        clock: ClockPort,
        classifier: InputArtifactClassifierPort | None = None,
        masking_gate: MaskingPolicyGatePort | None = None,
    ) -> None:
        self._reader = reader
        self._enumerator = enumerator
        self._ledger = ledger
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._classifier = classifier
        self._masking_gate = masking_gate

    def enumerate(self, request: InputReadRequest) -> list[str] | ReadDenial:
        if self._enumerator is None:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH, "candidate enumerator is unavailable"
            )
        with self._unit_of_work.begin_immediate():
            head = self._ledger.stream_head(request.stream_id)
            path_hash = self._path_hash(request)
            self._append(request, EventType.INPUT_READ_STARTED, head, path_hash)
            result = self._enumerator.enumerate(request.capability_id, request.relative_path)
            if isinstance(result, ReadDenial):
                self._append(request, EventType.INPUT_READ_DENIED, head + 1, path_hash)
        return result

    def read(self, request: InputReadRequest) -> InputReadOutcome:
        head_before = self._ledger.stream_head(request.stream_id)
        path_hash = self._path_hash(request)
        masking_denial: MaskingPolicyDenial | None = None

        with self._unit_of_work.begin_immediate():
            # 1. 読取を試みた事実を先に残す。Readerの判定より前である（§1.16.2.1）。
            self._append(request, EventType.INPUT_READ_STARTED, head_before, path_hash)
            head = head_before + 1

            # 2. Readerを呼ぶ。Readerは判定を返すだけでLedgerへ触らない。
            result = self._call_reader(request)

            events: tuple[EventType, ...]
            if isinstance(result, ReadDenial):
                self._append(request, EventType.INPUT_READ_DENIED, head, path_hash)
                events = (EventType.INPUT_READ_STARTED, EventType.INPUT_READ_DENIED)
                outcome_state = result.decision.value
                error_code: ErrorCode | None = result.error_code
                classification: str | None = None
                payload: bytes | None = None
                evidence: ReadEvidence | None = None
            else:
                payload, evidence = result
                classification = self._classify(payload, evidence)
                events = (EventType.INPUT_READ_STARTED,)
                if classification is not None:
                    self._append(request, EventType.INPUT_ARTIFACT_CLASSIFIED, head, path_hash)
                    events = (
                        EventType.INPUT_READ_STARTED,
                        EventType.INPUT_ARTIFACT_CLASSIFIED,
                    )
                outcome_state = ReadDecision.ALLOWED.value
                error_code = None

                # 分類まで進んだ入力を Masking Policy へ通す。拒否なら
                # Input Read の判定として DENIED を残す（§1.16.2.3 / MASK-2-C）。
                # **Masking の REJECTED をここ以外で DENIED へ読み替えない。**
                denial = self._evaluate_masking_policy(payload, classification)
                if denial is not None:
                    self._append(
                        request,
                        EventType.INPUT_READ_DENIED,
                        head + (1 if classification is not None else 0),
                        path_hash,
                    )
                    events = (*events, EventType.INPUT_READ_DENIED)
                    outcome_state = ReadDecision.DENIED.value
                    # Policy 拒否は Error Code を持たない（§1.16.3.2）。
                    error_code = None
                    masking_denial = denial
                    # A denied input must not expose reusable bytes or read evidence.
                    # Keep classification/denial metadata for the recorded decision.
                    payload = None
                    evidence = None

        # Commit後にLedgerを読み直す。Append要求の控えをHeadとして使わない。
        head_after = self._ledger.stream_head(request.stream_id)
        return InputReadOutcome(
            read_decision_id=request.read_decision_id,
            state=outcome_state,
            error_code=error_code,
            events=events,
            ledger_head_before=head_before,
            ledger_head_after=head_after,
            capability_path_hash=path_hash,
            classification=classification,
            payload=payload,
            read_evidence=evidence,
            masking_denial=masking_denial,
        )

    # ------------------------------------------------------------------

    def _call_reader(self, request: InputReadRequest) -> tuple[bytes, ReadEvidence] | ReadDenial:
        """Readerを呼ぶ。判定不能は`EFFECT_UNKNOWN`で止める（§1.16.2.1）。"""
        try:
            return self._reader.open_read(request.capability_id, request.relative_path)
        except HarnessError:
            # Harnessが分類済みのErrorはそのまま上げる。握り潰さない（不変条件#9）。
            raise
        except Exception as error:
            # 拒否だったのか成功だったのか分からない。`INPUT_READ_DENIED`を
            # 推測でAppendしない。Transactionは巻き戻り、Ledgerには何も残らない。
            raise HarnessError(
                ErrorCode.EFFECT_UNKNOWN,
                f"input read outcome is undetermined: {type(error).__name__} escaped the reader",
            ) from error

    def _evaluate_masking_policy(
        self, payload: bytes, classification: str | None
    ) -> MaskingPolicyDenial | None:
        """Masking Policy の判定を仰ぐ。判定不能は `EFFECT_UNKNOWN` で止める。"""
        if self._masking_gate is None:
            return None
        try:
            return self._masking_gate.evaluate(payload, classification)
        except HarnessError:
            raise
        except Exception as error:
            # 通したのか止めたのか分からない。推測で DENIED を残さない。
            raise HarnessError(
                ErrorCode.EFFECT_UNKNOWN,
                f"masking policy outcome is undetermined: {type(error).__name__} escaped the gate",
            ) from error

    def _classify(self, payload: bytes, evidence: ReadEvidence) -> str | None:
        if self._classifier is None:
            return None
        return self._classifier.classify(payload, evidence)

    def _append(
        self,
        request: InputReadRequest,
        event: EventType,
        expected_head: int,
        path_hash: ContentHash,
    ) -> None:
        self._ledger.append(
            [
                NewEvent(
                    stream_id=request.stream_id,
                    event_type=event.value,
                    # 本文はLedgerへ置かない。PathとCapabilityのHashだけを持つ（§15.7）。
                    payload_hash=hash_canonical(
                        {
                            "event_type": event.value,
                            "read_decision_id": request.read_decision_id,
                            "capability_path_hash": str(path_hash),
                        },
                        artifact_type=_EVENT_ARTIFACT_TYPE,
                        schema_major=_EVENT_SCHEMA_MAJOR,
                    ),
                    recorded_at=self._clock.now(),
                )
            ],
            expected_stream_sequence=expected_head,
        )

    @staticmethod
    def _path_hash(request: InputReadRequest) -> ContentHash:
        """CapabilityとPathのHash。生Pathをそのまま外へ出さない。"""
        return hash_canonical(
            {
                "capability_id": request.capability_id,
                "relative_path": request.relative_path,
            },
            artifact_type=_SUBJECT_ARTIFACT_TYPE,
            schema_major=_EVENT_SCHEMA_MAJOR,
        )
