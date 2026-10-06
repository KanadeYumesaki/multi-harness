"""Event Ledger Hash Chain（Domain純粋関数）の試験。

Gate 14「Ledger Hash Chain改ざん検出」の基盤にあたる。
Storage実装から独立してChainの性質だけを検証する。
"""

from __future__ import annotations

import pytest

from harness.domain.hashing import ContentHash, hash_bytes
from harness.domain.ledger import (
    GENESIS_PREVIOUS_HASH,
    LedgerEntry,
    compute_event_hash,
    verify_chain,
)

pytestmark = pytest.mark.unit

RECORDED_AT = "2026-08-06T00:00:00Z"


def _payload(text: str) -> ContentHash:
    return hash_bytes(text.encode("utf-8"))


def _entry(
    sequence: int, previous: ContentHash | None, *, event_type: str = "RUN_CREATED"
) -> LedgerEntry:
    payload = _payload(f"payload-{sequence}")
    event_hash = compute_event_hash(
        stream_id="run-1",
        sequence_number=sequence,
        event_type=event_type,
        payload_hash=payload,
        recorded_at=RECORDED_AT,
        previous_event_hash=previous,
    )
    return LedgerEntry(
        stream_id="run-1",
        sequence_number=sequence,
        event_type=event_type,
        payload_hash=payload,
        recorded_at=RECORDED_AT,
        previous_event_hash=previous,
        event_hash=event_hash,
    )


def _chain(length: int) -> list[LedgerEntry]:
    entries: list[LedgerEntry] = []
    previous: ContentHash | None = GENESIS_PREVIOUS_HASH
    for sequence in range(1, length + 1):
        entry = _entry(sequence, previous)
        entries.append(entry)
        previous = entry.event_hash
    return entries


# --------------------------------------------------------------------------
# 正常系
# --------------------------------------------------------------------------


def test_empty_stream_is_valid() -> None:
    result = verify_chain([])
    assert result.valid
    assert result.entry_count == 0


def test_well_formed_chain_verifies() -> None:
    result = verify_chain(_chain(5))
    assert result.valid, result.reason
    assert result.entry_count == 5


def test_hash_is_deterministic() -> None:
    a = _entry(1, GENESIS_PREVIOUS_HASH)
    b = _entry(1, GENESIS_PREVIOUS_HASH)
    assert a.event_hash == b.event_hash


def test_genesis_uses_null_previous_hash() -> None:
    """Stream先頭はnullとして符号化され、後続と区別できる。"""
    first = _entry(1, GENESIS_PREVIOUS_HASH)
    assert first.previous_event_hash is None


# --------------------------------------------------------------------------
# 改ざん検出（Gate 14）
# --------------------------------------------------------------------------


def test_payload_tamper_is_detected() -> None:
    """Payloadを差し替えるとevent_hashの再計算値が合わなくなる。"""
    entries = _chain(3)
    tampered = entries[1]
    entries[1] = LedgerEntry(
        stream_id=tampered.stream_id,
        sequence_number=tampered.sequence_number,
        event_type=tampered.event_type,
        payload_hash=_payload("tampered"),
        recorded_at=tampered.recorded_at,
        previous_event_hash=tampered.previous_event_hash,
        event_hash=tampered.event_hash,  # Hashは元のまま残す
    )
    result = verify_chain(entries)
    assert not result.valid
    assert result.first_invalid_sequence == 2
    assert "does not match recomputation" in (result.reason or "")


def test_event_type_tamper_is_detected() -> None:
    entries = _chain(3)
    original = entries[2]
    entries[2] = LedgerEntry(
        stream_id=original.stream_id,
        sequence_number=original.sequence_number,
        event_type="ACTION_COMMITTED",
        payload_hash=original.payload_hash,
        recorded_at=original.recorded_at,
        previous_event_hash=original.previous_event_hash,
        event_hash=original.event_hash,
    )
    result = verify_chain(entries)
    assert not result.valid
    assert result.first_invalid_sequence == 3


def test_removed_middle_entry_is_detected() -> None:
    """中間Eventの削除は連番の欠落として検出される。"""
    entries = _chain(4)
    del entries[1]
    result = verify_chain(entries)
    assert not result.valid
    assert "not contiguous" in (result.reason or "")


def test_reordered_entries_are_detected() -> None:
    entries = _chain(3)
    entries[0], entries[1] = entries[1], entries[0]
    result = verify_chain(entries)
    assert not result.valid


def test_broken_previous_hash_is_detected() -> None:
    """Chainを付け替えると前Hash不一致で検出される。"""
    entries = _chain(3)
    original = entries[2]
    forged_previous = _payload("forged")
    entries[2] = LedgerEntry(
        stream_id=original.stream_id,
        sequence_number=original.sequence_number,
        event_type=original.event_type,
        payload_hash=original.payload_hash,
        recorded_at=original.recorded_at,
        previous_event_hash=forged_previous,
        event_hash=compute_event_hash(
            stream_id=original.stream_id,
            sequence_number=original.sequence_number,
            event_type=original.event_type,
            payload_hash=original.payload_hash,
            recorded_at=original.recorded_at,
            previous_event_hash=forged_previous,
        ),
    )
    result = verify_chain(entries)
    assert not result.valid
    assert "does not chain" in (result.reason or "")
    assert result.first_invalid_sequence == 3


def test_truncated_tail_still_verifies() -> None:
    """末尾切詰めはChainだけでは検出できない。

    Chainは順序と内容の整合しか保証しない。「あるべきEventが無い」ことの検出は
    Projection／Store照合の責務であり、この限界を試験として明示しておく。
    """
    entries = _chain(5)
    assert verify_chain(entries[:3]).valid


# --------------------------------------------------------------------------
# 入力検証
# --------------------------------------------------------------------------


def test_sequence_must_start_at_one() -> None:
    with pytest.raises(ValueError, match="sequence_number"):
        compute_event_hash(
            stream_id="run-1",
            sequence_number=0,
            event_type="RUN_CREATED",
            payload_hash=_payload("x"),
            recorded_at=RECORDED_AT,
            previous_event_hash=None,
        )


def test_entry_rejects_empty_stream_id() -> None:
    with pytest.raises(ValueError, match="stream_id"):
        LedgerEntry(
            stream_id="",
            sequence_number=1,
            event_type="RUN_CREATED",
            payload_hash=_payload("x"),
            recorded_at=RECORDED_AT,
            previous_event_hash=None,
            event_hash=_payload("y"),
        )


def test_different_streams_produce_different_hashes() -> None:
    payload = _payload("same")
    a = compute_event_hash(
        stream_id="run-1",
        sequence_number=1,
        event_type="RUN_CREATED",
        payload_hash=payload,
        recorded_at=RECORDED_AT,
        previous_event_hash=None,
    )
    b = compute_event_hash(
        stream_id="run-2",
        sequence_number=1,
        event_type="RUN_CREATED",
        payload_hash=payload,
        recorded_at=RECORDED_AT,
        previous_event_hash=None,
    )
    assert a != b
