"""§1.18.1 GC Executor。削除の**実行**だけを持つ。

## Plannerの結果を実行結果へ写さない

`plan_garbage_collection` はDomain関数で`os`をimportしないので、そもそも削除
できない。Plannerの`GC_RESULT`が`ACCEPTED`であることは「候補を正しく挙げた」
証拠であって「Payloadが消えた」証拠ではない。

本Moduleは候補を**受け取るだけ**で、Stateは自分が観測した結果から決める。
Plannerの`state`をコピーしない。

## Filesystem削除とSQLite Commitを1つの原子操作にできない

Payload削除はFilesystem、Event AppendとManifest更新はSQLiteである。両者を
1つのTransactionへ入れられない。失敗の位置で残るものが変わるので、3つを
区別する。

| 失敗位置 | Payload | Manifest | Event | 扱い |
|---|---|---|---|---|
| 削除前 | 残る | 変わらない | 無い | 失敗。副作用なし |
| 削除後・Commit前 | **消えている** | 変わらない | 無い | **Reconciliation対象**。成功にしない |
| Commit後 | 消えている | `payload_deleted=1` | 有る | 成功 |

**削除を先**に行いCommitを後にする。逆順にすると「Eventは削除したと言うが
Bytesは残っている」状態を作る。Ledgerが実体より進んだ状態は後から直せない。

削除後Commit前に落ちた場合、Manifestは`payload_deleted=0`のままでBytesが無い。
この不一致は検出でき、Reconciliationで前進させられる。**成功Evidenceは
生成しない。**
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from harness.domain.artifact import VerificationOutcome
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.events import EventType
from harness.domain.hashing import ContentHash, hash_canonical
from harness.domain.operations_safety import GarbageCollectionResult
from harness.ports.artifact_gc import ArtifactManifestPort, ArtifactPayloadPort
from harness.ports.effect_execution import ClockPort
from harness.ports.event_ledger import EventLedgerPort, NewEvent
from harness.ports.unit_of_work import UnitOfWorkPort

__all__ = ["GcExecuteOutcome", "GcExecuteRequest", "GcExecutor"]

_EVENT_ARTIFACT_TYPE = "artifact-gc-event"
_EVENT_SCHEMA_MAJOR = 1


class _Verdict(Enum):
    """削除直前の再検証の結論。"""

    PROCEED = "PROCEED"
    #: Manifestは未削除と言うがBytesが無い。成功にせずReconciliationへ回す。
    RECONCILE = "RECONCILE"


@dataclass(frozen=True, slots=True)
class _ReadBack:
    """Filesystem・Manifest・Ledgerから読み戻した実測値。"""

    ok: bool
    payload_deleted: bool
    manifest_retained: bool
    deletion_event_count: int


@dataclass(frozen=True, slots=True)
class GcExecuteRequest:
    """承認済みGC計画の実行要求。"""

    stream_id: str
    gc_result_id: str
    plan: GarbageCollectionResult
    #: `artifact_id` → 削除対象のContent Hash。Pathを受け取らない。
    content_hashes: Mapping[str, ContentHash]


@dataclass(frozen=True, slots=True)
class GcExecuteOutcome:
    """`GC_RESULT`名前空間のSubject。**すべて実測値である。**"""

    gc_result_id: str
    state: str
    error_code: ErrorCode | None
    events: tuple[EventType, ...]
    ledger_head_before: int
    ledger_head_after: int
    payload_deleted: bool
    manifest_retained: bool
    deletion_event_count: int
    deleted_content_hashes: tuple[str, ...]
    reconciliation_required: bool


class GcExecutor:
    """承認済み候補のPayloadを消し、`ARTIFACT_PAYLOAD_DELETED`を残す。"""

    def __init__(
        self,
        *,
        cas: ArtifactPayloadPort,
        manifests: ArtifactManifestPort,
        ledger: EventLedgerPort,
        unit_of_work: UnitOfWorkPort,
        clock: ClockPort,
    ) -> None:
        self._cas = cas
        self._manifests = manifests
        self._ledger = ledger
        self._unit_of_work = unit_of_work
        self._clock = clock

    def execute(self, request: GcExecuteRequest) -> GcExecuteOutcome:
        head_before = self._ledger.stream_head(request.stream_id)
        targets = self._resolve_targets(request)

        deleted: list[ContentHash] = []
        for content_hash in targets:
            # --- 実行直前の再検証。Plannerの判断を信じ切らない ---------------
            if self._reverify(content_hash) is _Verdict.RECONCILE:
                return self._reconciliation(
                    request,
                    head_before,
                    ErrorCode.EFFECT_UNKNOWN,
                    f"manifest says the payload is present but the bytes are gone: {content_hash}",
                )

            # --- 1. Payloadを消す（Filesystem）------------------------------
            if not self._cas.delete_payload(content_hash):
                # 既に無い。Manifestが残っているならReconciliation対象である。
                return self._reconciliation(
                    request,
                    head_before,
                    ErrorCode.EFFECT_UNKNOWN,
                    f"payload already absent before deletion: {content_hash}",
                )
            deleted.append(content_hash)

            # --- 2. Manifest更新とEvent Appendを同一Transactionで ------------
            try:
                with self._unit_of_work.begin_immediate():
                    self._manifests.mark_payload_deleted(content_hash)
                    self._append(request.stream_id, content_hash, head_before + len(deleted) - 1)
            except Exception:
                # Bytesは消えたがEventが残らなかった。**成功にしない。**
                return self._reconciliation(
                    request,
                    head_before,
                    ErrorCode.EFFECT_UNKNOWN,
                    f"payload deleted but the ledger commit failed: {content_hash}",
                )

        # --- 3. 実体を読み戻す。申告値を観測値にしない ----------------------
        observed = self._read_back(request, deleted)
        head_after = self._ledger.stream_head(request.stream_id)
        return GcExecuteOutcome(
            gc_result_id=request.gc_result_id,
            state="ACCEPTED" if observed.ok else "REJECTED",
            error_code=None if observed.ok else ErrorCode.EFFECT_UNKNOWN,
            events=tuple(EventType.ARTIFACT_PAYLOAD_DELETED for _ in deleted),
            ledger_head_before=head_before,
            ledger_head_after=head_after,
            payload_deleted=observed.payload_deleted,
            manifest_retained=observed.manifest_retained,
            deletion_event_count=observed.deletion_event_count,
            deleted_content_hashes=tuple(str(h) for h in deleted),
            reconciliation_required=False,
        )

    # ------------------------------------------------------------------

    def _resolve_targets(self, request: GcExecuteRequest) -> list[ContentHash]:
        """Plannerが`deleted_ids`へ挙げた候補だけを対象にする。

        `referenced_kept_ids` と `orphan_candidate_ids` は対象にしない。
        参照中のPayloadを消さない。
        """
        kept = set(request.plan.referenced_kept_ids)
        targets: list[ContentHash] = []
        for artifact_id in request.plan.deleted_ids:
            if artifact_id in kept:
                # Plannerは deleted_ids と referenced_kept_ids を排他に作る。
                # 両方に居るのは呼出側が計画を組み替えた入力矛盾であり、
                # Domain の結果ではない。新しい Error Code を作らない。
                raise ValueError(
                    f"artifact {artifact_id} is both deleted and referenced; "
                    "refusing to delete a referenced payload"
                )
            content_hash = request.content_hashes.get(artifact_id)
            if content_hash is None:
                raise HarnessError(
                    ErrorCode.EFFECT_UNKNOWN,
                    f"no content hash was supplied for artifact {artifact_id}",
                )
            targets.append(content_hash)
        return targets

    def _reverify(self, content_hash: ContentHash) -> _Verdict:
        """削除直前にManifestとCASを突き合わせる。"""
        record = self._manifests.find_by_content_hash(content_hash)
        if record is None:
            raise HarnessError(
                ErrorCode.EFFECT_UNKNOWN,
                f"manifest is missing for {content_hash}; refusing to delete",
            )
        if record.payload_deleted:
            # 二重削除。Eventを2件残さない。
            raise HarnessError(
                ErrorCode.EFFECT_UNKNOWN,
                f"payload for {content_hash} is already marked deleted",
            )
        verification = self._cas.verify(content_hash, manifest_exists=True)
        if verification.outcome is VerificationOutcome.BYTES_MISSING:
            # Manifestは「未削除」と言うのにBytesが無い。これは
            # 「削除後・Commit前に落ちた」残骸と同じ形である（§1.14）。
            # 改ざんではないので別に扱い、Reconciliationへ回す。
            return _Verdict.RECONCILE
        if verification.outcome is not VerificationOutcome.OK:
            # 改ざんか破損。消して良いか判定できない。
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT,
                f"CAS verification failed before deletion: {verification.outcome.value}",
            )
        return _Verdict.PROCEED

    def _read_back(self, request: GcExecuteRequest, deleted: list[ContentHash]) -> _ReadBack:
        """Filesystem・Manifest・Ledgerの3つを読み戻す。"""
        # Application層はPathを知らない。`verify`の判定でBytesの有無を見る。
        payload_gone = all(
            self._cas.verify(h, manifest_exists=True).outcome is VerificationOutcome.BYTES_MISSING
            for h in deleted
        )
        records = [self._manifests.find_by_content_hash(h) for h in deleted]
        manifest_retained = bool(records) and all(r is not None for r in records)
        entries = self._ledger.load_stream(request.stream_id)
        deletion_events = [
            entry
            for entry in entries
            if entry.event_type == EventType.ARTIFACT_PAYLOAD_DELETED.value
        ]
        return _ReadBack(
            ok=payload_gone and manifest_retained and len(deletion_events) == len(deleted),
            payload_deleted=payload_gone and bool(deleted),
            manifest_retained=manifest_retained,
            deletion_event_count=len(deletion_events),
        )

    def _reconciliation(
        self,
        request: GcExecuteRequest,
        head_before: int,
        code: ErrorCode,
        detail: str,
    ) -> GcExecuteOutcome:
        """削除したがEventを残せなかった。**成功Evidenceを作らない。**"""
        return GcExecuteOutcome(
            gc_result_id=request.gc_result_id,
            state="REJECTED",
            error_code=code,
            events=(),
            ledger_head_before=head_before,
            ledger_head_after=self._ledger.stream_head(request.stream_id),
            payload_deleted=False,
            manifest_retained=True,
            deletion_event_count=0,
            deleted_content_hashes=(),
            reconciliation_required=True,
        )

    def _append(self, stream_id: str, content_hash: ContentHash, expected_head: int) -> None:
        self._ledger.append(
            [
                NewEvent(
                    stream_id=stream_id,
                    event_type=EventType.ARTIFACT_PAYLOAD_DELETED.value,
                    # Content Hashだけを持つ。Payload本文・Secret・PIIを入れない（§15.7）。
                    payload_hash=hash_canonical(
                        {
                            "event_type": EventType.ARTIFACT_PAYLOAD_DELETED.value,
                            "content_hash": str(content_hash),
                        },
                        artifact_type=_EVENT_ARTIFACT_TYPE,
                        schema_major=_EVENT_SCHEMA_MAJOR,
                    ),
                    recorded_at=self._clock.now(),
                )
            ],
            expected_stream_sequence=expected_head,
        )
