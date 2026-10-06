"""Historical inputs remain reproducible while current audits continue to observe drift.

The synthetic `canon_repo` tests below prove the answer-time resolution contract.
Running the saved decision builders against the current canon needs their private
upstream answer packages; that check is kept on the private side
(`tests/private_history/test_private_recorded_canon.py`).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import recorded_canon
from chat_canon_binding import CanonResolutionError, design_filename

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.spec_lint


def _sha(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


@pytest.fixture
def canon_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def create(*, ambiguous: bool = False, bad_hash: bool = False):
        root = tmp_path / "repo"
        root.mkdir()
        design = root / design_filename("7.0")
        design.write_text("fixture design\n")
        snapshot = {
            "design_version": "7.0",
            "design_sha256": _sha(design.read_bytes()),
            "registry_snapshot_hash": _sha(b"registry-original"),
            "schema_catalog_hash": _sha(b"schema-original"),
        }
        (root / "registry-snapshot.json").write_text(json.dumps(snapshot))
        record = root / "docs/decision/record.json"
        record.parent.mkdir(parents=True)
        package = {key: value for key, value in snapshot.items() if key != "schema_catalog_hash"}
        if bad_hash:
            package["design_sha256"] = _sha(b"wrong-design")
        record.write_text(json.dumps(package))
        if ambiguous:
            (root / "duplicate").mkdir()
            (root / "duplicate" / design.name).write_bytes(design.read_bytes())
        for args in (
            ["init", "-q"],
            ["add", "--", "."],
            [
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "fixture",
            ],
        ):
            subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, timeout=30)  # noqa: S603, S607 - fixed fixture argv
        commit = subprocess.run(  # noqa: S603 - fixed Git argv
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - Git from test environment
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        monkeypatch.setattr(recorded_canon, "RECORD_BASELINE", commit)
        return root, record, design, snapshot

    return create


def test_current_drift_does_not_change_recorded_inputs(canon_repo) -> None:
    root, record, design, snapshot = canon_repo()
    original = record.read_bytes()
    assert recorded_canon.recorded_snapshot(root, record) == snapshot
    old_design = recorded_canon.recorded_design_text(root, record)
    design.write_text("changed current design\n")
    current = dict(
        snapshot,
        design_sha256=_sha(design.read_bytes()),
        registry_snapshot_hash=_sha(b"new-registry"),
    )
    (root / "registry-snapshot.json").write_text(json.dumps(current))
    assert recorded_canon.recorded_snapshot(root, record, current=True) == current
    assert recorded_canon.recorded_design_text(root, record, current=True) == design.read_text()
    assert recorded_canon.recorded_snapshot(root, record) == snapshot
    assert recorded_canon.recorded_design_text(root, record) == old_design
    assert record.read_bytes() == original
    assert "schema_catalog_hash" not in json.loads(original)


def test_record_byte_change_is_rejected(canon_repo) -> None:
    root, record, _, _ = canon_repo()
    record.write_bytes(record.read_bytes() + b" ")
    with pytest.raises(ValueError, match="RECORDED_CANON_RECORD_CHANGED"):
        recorded_canon.recorded_snapshot(root, record)


def test_missing_git_input_never_falls_back(canon_repo, monkeypatch) -> None:
    root, record, _, _ = canon_repo()
    monkeypatch.setattr(recorded_canon, "RECORD_BASELINE", "absent-reference")
    with pytest.raises(CanonResolutionError, match="GIT_HISTORY_UNAVAILABLE"):
        recorded_canon.recorded_snapshot(root, record)


@pytest.mark.parametrize(
    "option,code",
    [
        ("ambiguous", "GIT_HISTORY_DESIGN_PATH_AMBIGUOUS"),
        ("bad_hash", "GIT_HISTORY_DESIGN_HASH_MISMATCH"),
    ],
)
def test_invalid_history_fails_closed(canon_repo, option: str, code: str) -> None:
    root, record, _, _ = canon_repo(**{option: True})
    with pytest.raises(CanonResolutionError, match=code):
        recorded_canon.recorded_snapshot(root, record)


@pytest.mark.parametrize("current", [False, True])
@pytest.mark.parametrize("kind", ["direct", "symlink", "hardlink", "new-path"])
def test_saved_directory_and_aliases_are_protected(tmp_path, monkeypatch, current, kind) -> None:
    root = tmp_path / "repo"
    source = root / "docs/audit/another-report.md"
    source.parent.mkdir(parents=True)
    source.write_text("preserved")
    monkeypatch.setattr(recorded_canon, "__file__", str(root / "tools/recorded_canon.py"))
    output = source
    if kind == "symlink":
        output = tmp_path / "alias.md"
        output.symlink_to(source)
    elif kind == "hardlink":
        output = tmp_path / "alias.md"
        output.hardlink_to(source)
    elif kind == "new-path":
        output = root / "docs/decision/new.json"
    with pytest.raises(ValueError, match="SAVED_RECORD_OUTPUT_DENIED"):
        recorded_canon.require_separate_current_output(current, (output,), ())
    assert source.read_text() == "preserved"
    recorded_canon.require_separate_current_output(current, (tmp_path / "fresh.json",), ())
