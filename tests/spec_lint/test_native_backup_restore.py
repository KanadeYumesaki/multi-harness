"""Native restoration rejects missing/corrupt objects and preserves existing output."""

from pathlib import Path

import pytest

from harness.domain.errors import HarnessError
from harness.domain.hashing import ContentHash
from harness.infrastructure.artifact.filesystem_cas import FilesystemArtifactCas
from measure_backup_restore import inspect_restored, measure_backup_restore

STAMP = "2026-09-11T00:00:00Z"


def test_native_restore_roundtrip_and_no_overwrite(tmp_path: Path):
    root = tmp_path / "probe"
    result = measure_backup_restore(root, STAMP)
    assert result["matched"] and result["source_unchanged"]
    assert result["observed"]["integrity_check"] == ["ok"]
    assert result["observed"]["streams"] and result["observed"]["artifacts"]
    assert result["release_area_status"] == "UNVERIFIED"
    original = (root / "source.sqlite3").read_bytes()
    with pytest.raises(FileExistsError):
        measure_backup_restore(root, STAMP)
    assert (root / "source.sqlite3").read_bytes() == original


@pytest.mark.parametrize("damage", ["missing", "tampered"])
def test_restored_cas_damage_is_rejected(tmp_path: Path, damage: str):
    root = tmp_path / "probe"
    result = measure_backup_restore(root, STAMP)
    artifact = result["observed"]["artifacts"][0]
    cas = FilesystemArtifactCas(root / "restored-cas")
    path = cas.object_path(ContentHash.parse(artifact["hash"]))
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"tampered")
    with pytest.raises((HarnessError, FileNotFoundError)):
        inspect_restored(root / "restored.sqlite3", cas.root)
