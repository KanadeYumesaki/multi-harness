"""Static release evidence must fail when the measured tree or checker result changes."""

import shutil
import subprocess
from pathlib import Path

import pytest

from collect_release_baseline import measure_source_static
from emit_case_evidence import EvidenceEmissionError


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    for name in ("domain", "ports", "infrastructure", "application", "presentation"):
        folder = tmp_path / "src/harness" / name
        folder.mkdir(parents=True)
        (folder / "__init__.py").write_text("# synthetic source\n")
    git = shutil.which("git")
    assert git is not None
    for args in (
        ["init"],
        ["add", "."],
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "synthetic input",
        ],
    ):
        subprocess.run([git, *args], cwd=tmp_path, capture_output=True, check=True, timeout=30)  # noqa: S603 - fixed synthetic Git commands
    return tmp_path


def success(name: str, command: list[str]) -> dict:
    # Test-only runner: these results are never emitted as runtime evidence.
    return {"command": command, "exit_code": 0}


def test_static_hash_is_stable_for_same_measured_bytes(repo: Path):
    first = measure_source_static(repo, success)
    second = measure_source_static(repo, success)
    assert first["source_tree_hash"] == second["source_tree_hash"]
    assert first["checks"] and first["source_file_count"] > 0


def test_failure_cannot_be_reported_as_static_pass(repo: Path):
    with pytest.raises(EvidenceEmissionError, match="STATIC_CHECK_NOT_SUCCESSFUL"):
        measure_source_static(repo, lambda name, cmd: {"command": cmd, "exit_code": 1})


def test_source_mutation_during_checks_is_rejected(repo: Path):
    def mutate(name: str, command: list[str]) -> dict:
        (repo / "src/harness/domain/__init__.py").write_text("# changed\n")
        return success(name, command)

    with pytest.raises(EvidenceEmissionError, match="STATIC_SOURCE_CHANGED"):
        measure_source_static(repo, mutate)
