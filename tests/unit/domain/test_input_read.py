"""§1.16.2 のうちFilesystemを見ずに判定できる部分の試験（Domain純粋関数）。"""

from __future__ import annotations

import pytest

from harness.domain.errors import ErrorCode
from harness.domain.input_read import (
    CapabilityScope,
    DenialReason,
    ReadDecision,
    ReadDenial,
    normalize_relative_segments,
    validate_relative_path,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# Path検証（Filesystemへ触れる前に落とす）
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/etc/passwd",
        "/",
        "../secret",
        "docs/../../etc/passwd",
        "docs/./a",
        "docs//a",
        "docs/",
        "a\0b",
        "a\nb",
        "C:/Windows/System32",
        "\\\\host\\share",
        "docs\\a",
    ],
)
def test_unsafe_paths_are_rejected(path: str) -> None:
    """§1.16.2「Workspace外Path、絶対Path、空Path、NULを含むPath」ほか。

    Windows系のDrive／UNC／Device Pathも MVP2-A 以前は拒否する。
    """
    assert validate_relative_path(path) is None


@pytest.mark.parametrize(
    ("path", "segments"),
    [
        ("a", ("a",)),
        ("docs/readme.md", ("docs", "readme.md")),
        ("a/b/c/d.txt", ("a", "b", "c", "d.txt")),
        ("日本語/ファイル.txt", ("日本語", "ファイル.txt")),
        ("dot.file.name", ("dot.file.name",)),
        ("..hidden", ("..hidden",)),
    ],
)
def test_safe_paths_are_accepted(path: str, segments: tuple[str, ...]) -> None:
    assert validate_relative_path(path) == segments


def test_normalize_raises_on_unsafe_path() -> None:
    with pytest.raises(ValueError, match="safe workspace-relative"):
        normalize_relative_segments("../x")


# --------------------------------------------------------------------------
# Capability Scope
# --------------------------------------------------------------------------


def test_scope_matches_on_segment_boundary() -> None:
    """文字列prefix比較だと `docs` が `docs-secret/` を許してしまう。"""
    scope = CapabilityScope(("docs",))
    assert scope.permits("docs/readme.md")
    assert scope.permits("docs")
    assert not scope.permits("docs-secret/leak.md")
    assert not scope.permits("docsx")


def test_scope_accepts_trailing_slash_prefix() -> None:
    scope = CapabilityScope(("docs/",))
    assert scope.permits("docs/readme.md")


def test_scope_rejects_paths_outside() -> None:
    scope = CapabilityScope(("docs", "src"))
    assert not scope.permits("etc/passwd")
    assert not scope.permits("../docs/readme.md")


def test_scope_rejects_unsafe_request_path() -> None:
    scope = CapabilityScope(("docs",))
    assert not scope.permits("docs/../../etc/passwd")


def test_empty_scope_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        CapabilityScope(())


@pytest.mark.parametrize("prefix", ["docs/*", "docs/?", "docs/[a-z]"])
def test_scope_rejects_glob(prefix: str) -> None:
    """ADR-006のPredicateと同じ方針。範囲が読んで分かる形だけを許す。"""
    with pytest.raises(ValueError, match="glob"):
        CapabilityScope((prefix,))


def test_scope_rejects_unsafe_prefix() -> None:
    with pytest.raises(ValueError, match="unsafe scope prefix"):
        CapabilityScope(("../etc",))


# --------------------------------------------------------------------------
# 拒否理由 → Error Code
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        (DenialReason.PATH_OUTSIDE_CAPABILITY, ErrorCode.PATH_OUTSIDE_CAPABILITY),
        (DenialReason.SYMLINK_DENIED, ErrorCode.SYMLINK_DENIED),
        (DenialReason.MOUNT_CROSSING_DENIED, ErrorCode.MOUNT_CROSSING_DENIED),
        (DenialReason.SPECIAL_FILE_DENIED, ErrorCode.SPECIAL_FILE_DENIED),
    ],
)
def test_denial_reason_maps_to_registered_error_code(reason: DenialReason, code: ErrorCode) -> None:
    assert reason.error_code is code


def test_denial_state_is_denied() -> None:
    """`INPUT_READ_DECISION` 名前空間の `DENIED`。"""
    denial = ReadDenial(
        reason=DenialReason.SYMLINK_DENIED,
        capability_id="cap-1",
        requested_path="docs/link",
    )
    assert denial.decision is ReadDecision.DENIED
    assert denial.error_code is ErrorCode.SYMLINK_DENIED


def test_denial_record_carries_no_bytes() -> None:
    """§1.16.2「拒否PathのBytesはArtifact Storeへ保存しない」。

    記録構造自体がBytesを持たないことを型で保証する。
    """
    denial = ReadDenial(
        reason=DenialReason.SPECIAL_FILE_DENIED,
        capability_id="cap-1",
        requested_path="docs/fifo",
    )
    assert not any(isinstance(getattr(denial, field), bytes) for field in denial.__slots__)
