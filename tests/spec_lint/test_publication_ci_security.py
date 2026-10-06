"""Public PR inputs must remain data and CI credentials must be least-privilege."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = [ROOT / ".github/workflows/ci.yml", ROOT / "ci/github-actions-ci.yml"]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.parent.name)
def test_all_actions_are_pinned_and_checkout_does_not_persist_credentials(path: Path) -> None:
    document = yaml.safe_load(path.read_text())
    assert document["permissions"] == {"contents": "read"}
    for job in document["jobs"].values():
        assert job.get("permissions", document["permissions"]) == {"contents": "read"}
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", step["uses"])
                if step["uses"].startswith("actions/checkout@"):
                    assert step["with"]["persist-credentials"] is False
            assert "${{" not in step.get("run", ""), "Event expressions must enter via env"
    assert (ROOT / document["env"]["DESIGN"]).is_file()


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.parent.name)
def test_scope_step_treats_hostile_branch_and_labels_as_literal_arguments(
    path: Path, tmp_path: Path
) -> None:
    document = yaml.safe_load(path.read_text())
    step = next(s for s in document["jobs"]["plan"]["steps"] if s.get("name") == "decide-scope")
    sentry = tmp_path / "must-not-exist"
    captured = tmp_path / "arguments.json"
    # The fake python records argv; the workflow's actual Bash text is executed unchanged.
    executable = tmp_path / "python"
    executable.write_text(
        f"#!{sys.executable}\nimport json,os,sys\n"
        "open(os.environ['ARGUMENTS_FILE'],'w').write(json.dumps(sys.argv[1:]))\n"
    )
    executable.chmod(0o700)
    hostile = f"x'; touch {sentry}; # $(touch {sentry}) `touch {sentry}`"
    values = {
        "${{ github.event_name }}": "pull_request",
        "${{ github.event.pull_request.draft }}": "false",
        '${{ join(github.event.pull_request.labels.*.name, ",") }}': hostile,
        "${{ github.ref }}": "refs/pull/1/merge",
        "${{ github.head_ref }}": hostile,
    }
    environment = dict(
        os.environ,
        PATH=str(tmp_path) + os.pathsep + os.environ["PATH"],
        ARGUMENTS_FILE=str(captured),
        GITHUB_OUTPUT=str(tmp_path / "github-output"),
    )
    environment.update({key: values[expression] for key, expression in step["env"].items()})
    result = subprocess.run(  # noqa: S603 - fixed workflow script; hostile values stay in env
        ["/bin/bash", "-eu", "-c", step["run"]],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert not sentry.exists()
    args = json.loads(captured.read_text())
    assert args[args.index("--labels") + 1] == hostile
    assert args[args.index("--head-ref") + 1] == hostile


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.parent.name)
def test_sbom_and_audit_use_the_lock_bound_production_entry(path: Path, tmp_path: Path) -> None:
    """Workflowの実Shellを実行し、完全監査入口と証跡Directoryを固定する。

    元のSBOM入力文字列確認より強い条件: 個別SBOMだけで合格にせず、Lockの
    License・脆弱性結果・SBOMをすべて検査する入口を必ず呼ぶ。
    """
    document = yaml.safe_load(path.read_text())
    steps = document["jobs"]["supply-chain"]["steps"]
    step = next(s for s in steps if s.get("name") == "dependency-audit (pip-audit / cyclonedx-py)")
    executable = tmp_path / "python"
    captured = tmp_path / "audit-argv.json"
    executable.write_text(
        f"#!{sys.executable}\nimport json,os,sys\n"
        "open(os.environ['CAPTURE_FILE'],'w').write(json.dumps(sys.argv[1:]))\n"
    )
    executable.chmod(0o700)
    environment = dict(
        os.environ,
        PATH=str(tmp_path) + os.pathsep + os.environ["PATH"],
        CAPTURE_FILE=str(captured),
        RUNNER_TEMP=str(tmp_path / "runner temp"),
    )
    result = subprocess.run(  # noqa: S603 — Workflowの固定Script、値はenv。
        ["/bin/bash", "-eu", "-c", step["run"]],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert json.loads(captured.read_text()) == [
        "tools/run_public_dependency_audit.py",
        "--repo",
        ".",
        "--out",
        str(tmp_path / "runner temp/public-dependency-audit"),
    ]
    artifact = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@"))
    assert artifact["with"]["if-no-files-found"] == "error"
    assert "public-dependency-audit/*.json" in artifact["with"]["path"]
    assert "public-dependency-audit/*.log" in artifact["with"]["path"]
