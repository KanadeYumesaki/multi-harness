"""Real hardlinks must not bypass capability paths, in either read implementation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness.domain.errors import ErrorCode
from harness.domain.input_read import CapabilityScope, ReadDenial
from harness.infrastructure.filesystem import safe_reader as reader_module
from harness.infrastructure.filesystem.safe_reader import CapabilityBroker, SafeInputReader
from harness.infrastructure.filesystem.workspace_boundary import FilesystemPolicy

REPO_ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("origin", ["outside", "outside_scope", "inside_scope"])
def test_hardlink_alias_is_rejected_before_any_read(tmp_path: Path, monkeypatch, fallback, origin):
    workspace = tmp_path / "workspace"
    docs = workspace / "docs"
    docs.mkdir(parents=True)
    source = {
        "outside": tmp_path / "original.txt",
        "outside_scope": workspace / "original.txt",
        "inside_scope": docs / "original.txt",
    }[origin]
    source.write_bytes(b"synthetic content\n")
    alias = docs / "alias.txt"
    os.link(source, alias)
    assert source.stat().st_ino == alias.stat().st_ino
    if fallback:
        monkeypatch.setattr(reader_module, "is_available", lambda: False)

    def forbidden_read(fd, **kwargs):
        pytest.fail("hardlink bytes reached the read syscall boundary")

    monkeypatch.setattr(SafeInputReader, "_read_all", staticmethod(forbidden_read))
    with CapabilityBroker(FilesystemPolicy.load(REPO_ROOT), allow_test_filesystems=True) as broker:
        broker.issue("hardlink-test", workspace, CapabilityScope(("docs",)))
        result = SafeInputReader(broker).open_read("hardlink-test", "docs/alias.txt")
    assert isinstance(result, ReadDenial)
    assert result.error_code is ErrorCode.PATH_OUTSIDE_CAPABILITY
    assert "hardlink" in result.detail


def test_hardlink_discards_the_entire_candidate_list(tmp_path: Path):
    workspace = tmp_path / "workspace"
    docs = workspace / "docs"
    docs.mkdir(parents=True)
    (docs / "a-normal.txt").write_bytes(b"normal\n")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"synthetic outside\n")
    os.link(outside, docs / "z-linked.txt")
    with CapabilityBroker(FilesystemPolicy.load(REPO_ROOT), allow_test_filesystems=True) as broker:
        broker.issue("hardlink-test", workspace, CapabilityScope(("docs",)))
        result = SafeInputReader(broker).enumerate("hardlink-test", "docs")
    assert isinstance(result, ReadDenial)
    assert result.error_code is ErrorCode.PATH_OUTSIDE_CAPABILITY


def test_alias_created_during_read_invalidates_returned_bytes(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    docs = workspace / "docs"
    docs.mkdir(parents=True)
    source = docs / "normal.txt"
    source.write_bytes(b"synthetic content\n")
    original = SafeInputReader._read_all

    def create_alias(fd, **kwargs):
        result = original(fd, **kwargs)
        os.link(source, tmp_path / "outside.txt")
        return result

    monkeypatch.setattr(SafeInputReader, "_read_all", staticmethod(create_alias))
    with CapabilityBroker(FilesystemPolicy.load(REPO_ROOT), allow_test_filesystems=True) as broker:
        broker.issue("hardlink-test", workspace, CapabilityScope(("docs",)))
        result = SafeInputReader(broker).open_read("hardlink-test", "docs/normal.txt")
    assert isinstance(result, ReadDenial)
    assert result.error_code is ErrorCode.PATH_OUTSIDE_CAPABILITY


def test_single_link_still_reads_after_unverified_alias_is_removed(tmp_path: Path):
    workspace = tmp_path / "workspace"
    docs = workspace / "docs"
    docs.mkdir(parents=True)
    source = docs / "normal.txt"
    source.write_bytes(b"synthetic content\n")
    outside = tmp_path / "outside.txt"
    os.link(source, outside)
    with CapabilityBroker(FilesystemPolicy.load(REPO_ROOT), allow_test_filesystems=True) as broker:
        broker.issue("hardlink-test", workspace, CapabilityScope(("docs",)))
        reader = SafeInputReader(broker)
        assert isinstance(reader.open_read("hardlink-test", "docs/normal.txt"), ReadDenial)
        outside.unlink()
        result = reader.open_read("hardlink-test", "docs/normal.txt")
        assert not isinstance(result, ReadDenial)
        assert result[0] == source.read_bytes()
