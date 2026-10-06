"""Artifact の値オブジェクトとCAS配置規則（Domain純粋関数）の試験。"""

from __future__ import annotations

import pytest

from harness.domain.artifact import (
    ArtifactMetadata,
    ArtifactVerification,
    VerificationOutcome,
    cas_object_segments,
    cas_temp_segments,
)
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash, hash_bytes

pytestmark = pytest.mark.unit


def _hash(text: str) -> ContentHash:
    return hash_bytes(text.encode("utf-8"))


# --------------------------------------------------------------------------
# CAS配置
# --------------------------------------------------------------------------


def test_object_segments_are_content_addressed() -> None:
    """同一Hashは必ず同一Pathへ落ちる。"""
    a = cas_object_segments(_hash("payload"))
    b = cas_object_segments(_hash("payload"))
    assert a == b


def test_object_segments_shard_by_hash_prefix() -> None:
    content_hash = _hash("payload")
    digest = content_hash.hexdigest
    assert cas_object_segments(content_hash) == (
        "objects",
        digest[0:2],
        digest[2:4],
        digest,
    )


def test_different_content_lands_on_different_paths() -> None:
    assert cas_object_segments(_hash("a")) != cas_object_segments(_hash("b"))


def test_temp_segments_stay_inside_the_cas_root() -> None:
    """§1.14「同一FilesystemのTempへwrite」。

    `/tmp`等へ置くとRenameがFilesystemを跨ぎAtomicでなくなる。
    """
    segments = cas_temp_segments(_hash("x"), attempt_token="deadbeef")
    assert segments[0] == "incoming"
    assert not any(segment.startswith("/") for segment in segments)
    assert ".." not in segments


def test_temp_token_must_be_path_safe() -> None:
    for token in ("", "a/b", "a\0b"):
        with pytest.raises(ValueError, match="path-safe"):
            cas_temp_segments(_hash("x"), attempt_token=token)


def test_temp_token_does_not_change_object_path() -> None:
    """Temp名はContent Hashの入力ではない。"""
    content_hash = _hash("x")
    first = cas_temp_segments(content_hash, attempt_token="aaaa")
    second = cas_temp_segments(content_hash, attempt_token="bbbb")
    assert first != second
    assert cas_object_segments(content_hash) == cas_object_segments(content_hash)


# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------


def test_metadata_rejects_negative_size() -> None:
    with pytest.raises(ValueError, match="size_bytes"):
        ArtifactMetadata("text/plain", -1, "INTERNAL", "VERIFIED_INTERNAL")


@pytest.mark.parametrize("field", ["media_type", "data_classification", "trust_level"])
def test_metadata_rejects_empty_classification(field: str) -> None:
    values = {
        "media_type": "text/plain",
        "size_bytes": 1,
        "data_classification": "INTERNAL",
        "trust_level": "VERIFIED_INTERNAL",
    }
    values[field] = ""
    with pytest.raises(ValueError, match=field):
        ArtifactMetadata(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Verification（§1.14 Repair方針）
# --------------------------------------------------------------------------


def test_bytes_missing_raises_repair_required() -> None:
    """§1.14「DBにManifestがありBytesがない場合は`BLOCKED_REPAIR_REQUIRED`」。"""
    verification = ArtifactVerification(
        outcome=VerificationOutcome.BYTES_MISSING, content_hash=_hash("x")
    )
    assert not verification.ok
    with pytest.raises(HarnessError) as excinfo:
        verification.raise_if_repair_required()
    assert excinfo.value.code is ErrorCode.ARTIFACT_CONTENT_CONFLICT
    assert "BLOCKED_REPAIR_REQUIRED" in str(excinfo.value)


def test_content_mismatch_raises() -> None:
    verification = ArtifactVerification(
        outcome=VerificationOutcome.CONTENT_MISMATCH,
        content_hash=_hash("x"),
        observed_hash=_hash("y"),
    )
    with pytest.raises(HarnessError, match="do not hash to"):
        verification.raise_if_repair_required()


def test_orphan_bytes_do_not_raise() -> None:
    """§1.14「Bytesだけがある場合は孤児としてGC候補」。停止事由ではない。"""
    verification = ArtifactVerification(
        outcome=VerificationOutcome.ORPHAN_BYTES, content_hash=_hash("x")
    )
    assert not verification.ok
    verification.raise_if_repair_required()


def test_ok_is_ok() -> None:
    verification = ArtifactVerification(outcome=VerificationOutcome.OK, content_hash=_hash("x"))
    assert verification.ok
    verification.raise_if_repair_required()
