"""Event LedgerのHash Chain。Domain層の純粋関数だけで構成する。

§1.14「LedgerのUPDATE／DELETE禁止は多層防御で強制する」の検出層にあたる。
Chainの計算と検証はI/Oを持たないため、Storage実装から独立して試験できる。

Chain入力へPayload本文を直接入れず`payload_hash`だけを含める。
§1.11「SnapshotはHashだけを含める」および不変条件#7（Secret値を外へ出さない）に従う。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from harness.domain.hashing import HASH_PROFILE_VERSION, ContentHash, hash_canonical

__all__ = [
    "GENESIS_PREVIOUS_HASH",
    "ChainVerificationResult",
    "LedgerEntry",
    "compute_event_hash",
    "verify_chain",
]

_ARTIFACT_TYPE: Final[str] = "ledger-event"
_SCHEMA_MAJOR: Final[int] = 1

# Stream先頭の`previous_event_hash`。Chainの起点を明示するためNoneを使う。
GENESIS_PREVIOUS_HASH: Final[None] = None


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """Chain計算に必要な最小の射影。

    EventEnvelopeの全項目ではなく、Chainへ束縛する項目だけを持つ。
    Payload本文は含めない。
    """

    stream_id: str
    sequence_number: int
    event_type: str
    payload_hash: ContentHash
    recorded_at: str
    previous_event_hash: ContentHash | None
    event_hash: ContentHash

    def __post_init__(self) -> None:
        if self.sequence_number < 1:
            raise ValueError("sequence_number must start at 1")
        if not self.stream_id:
            raise ValueError("stream_id must not be empty")


def compute_event_hash(
    *,
    stream_id: str,
    sequence_number: int,
    event_type: str,
    payload_hash: ContentHash,
    recorded_at: str,
    previous_event_hash: ContentHash | None,
) -> ContentHash:
    """1件分のChain Hashを計算する。

    `previous_event_hash`がNoneのときStream先頭を意味する。
    JCSはnullを`null`として符号化するため、先頭と「前Hashを省略した改ざん」は
    別のCanonical Bytesになる。
    """
    if sequence_number < 1:
        raise ValueError("sequence_number must start at 1")
    projection = {
        "previous_event_hash": (None if previous_event_hash is None else str(previous_event_hash)),
        "stream_id": stream_id,
        "sequence_number": sequence_number,
        "event_type": event_type,
        "payload_hash": str(payload_hash),
        "recorded_at": recorded_at,
        "hash_profile_version": HASH_PROFILE_VERSION,
    }
    return hash_canonical(projection, artifact_type=_ARTIFACT_TYPE, schema_major=_SCHEMA_MAJOR)


@dataclass(frozen=True, slots=True)
class ChainVerificationResult:
    """Chain検証の結果。失敗理由を握り潰さず構造化して返す（不変条件#9）。"""

    valid: bool
    entry_count: int
    first_invalid_sequence: int | None = None
    reason: str | None = None

    @classmethod
    def ok(cls, entry_count: int) -> ChainVerificationResult:
        return cls(valid=True, entry_count=entry_count)

    @classmethod
    def failed(cls, entry_count: int, sequence: int, reason: str) -> ChainVerificationResult:
        return cls(
            valid=False,
            entry_count=entry_count,
            first_invalid_sequence=sequence,
            reason=reason,
        )


def verify_chain(entries: Iterable[LedgerEntry]) -> ChainVerificationResult:
    """Stream内のChainを先頭から検証する。

    次を検出する。

    * `sequence_number`の欠番・重複・巻戻り
    * `previous_event_hash`の断裂（前EventのHashと不一致）
    * 記録済み`event_hash`と再計算値の不一致（Payload改変・項目改変）
    """
    ordered: Sequence[LedgerEntry] = list(entries)
    if not ordered:
        return ChainVerificationResult.ok(0)

    expected_sequence = 1
    previous_hash: ContentHash | None = GENESIS_PREVIOUS_HASH

    for entry in ordered:
        if entry.sequence_number != expected_sequence:
            return ChainVerificationResult.failed(
                len(ordered),
                entry.sequence_number,
                f"sequence_number is not contiguous; expected {expected_sequence}",
            )
        if entry.previous_event_hash != previous_hash:
            return ChainVerificationResult.failed(
                len(ordered), entry.sequence_number, "previous_event_hash does not chain"
            )
        recomputed = compute_event_hash(
            stream_id=entry.stream_id,
            sequence_number=entry.sequence_number,
            event_type=entry.event_type,
            payload_hash=entry.payload_hash,
            recorded_at=entry.recorded_at,
            previous_event_hash=entry.previous_event_hash,
        )
        if recomputed != entry.event_hash:
            return ChainVerificationResult.failed(
                len(ordered), entry.sequence_number, "event_hash does not match recomputation"
            )
        previous_hash = entry.event_hash
        expected_sequence += 1

    return ChainVerificationResult.ok(len(ordered))
