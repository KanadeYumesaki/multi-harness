"""Case実行の終了値と観測を別記録に残す。PASSを推測しない。"""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from case_runner import CaseRunnerError, PytestCaseAdapter


def test_failed_process_records_actual_exit_and_observations(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(command, **kwargs):
        Path(kwargs["env"]["CASE_OBSERVATION_SINK"]).write_text('{"case_id":"AT-X/CASE"}\n')
        return subprocess.CompletedProcess(command, 7, "synthetic stdout", "synthetic stderr")

    monkeypatch.setattr(subprocess, "run", run)
    adapter = PytestCaseAdapter(
        "AT-X/CASE",
        ("tests/test_x.py::test_x",),
        repo,
        tmp_path / "fixtures",
        execution_records_dir=tmp_path / "records",
    )
    with pytest.raises(CaseRunnerError, match="exit=7"):
        adapter._collect()
    folder = tmp_path / "records/AT-X/CASE"
    body = json.loads((folder / "execution.json").read_text())
    assert body["exit_code"] == 7
    assert (
        body["raw_observations_hash"]
        == "sha256:" + hashlib.sha256((folder / "observations.jsonl").read_bytes()).hexdigest()
    )
    assert "synthetic stdout" not in (folder / "execution.json").read_text()
    with pytest.raises(FileExistsError):
        adapter._collect()


def test_records_cannot_dirty_repository(tmp_path):
    adapter = PytestCaseAdapter(
        "AT-X/CASE",
        ("test",),
        tmp_path,
        tmp_path / "fixture",
        execution_records_dir=tmp_path / "records",
    )
    with pytest.raises(CaseRunnerError, match="IN_REPOSITORY"):
        adapter._collect()
