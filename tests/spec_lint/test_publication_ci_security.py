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
        "${{ join(github.event.pull_request.labels.*.name, ',') }}": hostile,
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


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.parent.name)
def test_expression_string_literals_use_github_actions_quoting(path: Path) -> None:
    # GitHub rejects double-quoted expression strings before it creates any jobs.
    # Strip valid single-quoted literals; quotes within those literals are legal.
    expressions = re.findall(r"\$\{\{(.*?)\}\}", path.read_text(), flags=re.DOTALL)
    assert expressions
    for expression in expressions:
        nonliteral = re.sub(r"'(?:[^']|'')*'", "", expression)
        assert '"' not in nonliteral, f"Invalid expression string quoting: {expression}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.parent.name)
def test_source_archive_preserves_all_committed_bytes_without_private_release_report(
    path: Path, tmp_path: Path
) -> None:
    document = yaml.safe_load(path.read_text())
    step = next(
        s
        for s in document["jobs"]["spec"]["steps"]
        if s.get("name") == "source-preview-archive-smoke"
    )
    assert "python -m pytest tests/test_package_release_zip.py -q" in step["run"]
    # Execute the actual archive/verification script in a real synthetic repository.
    # Formal packager tests run separately; their existing rejection cases stay intact.
    script = "\n".join(step["run"].splitlines()[1:])
    repo = tmp_path / "source"
    repo.mkdir()
    expected = {"hello.txt": b"synthetic source\n", "日本語.md": "合成の文書\n".encode()}
    for name, data in expected.items():
        (repo / name).write_bytes(data)
    git = [
        "/usr/bin/git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.name=Synthetic",
        "-c",
        "user.email=synthetic@example.invalid",
    ]
    for args in [["init", "-b", "codex/fixture"], ["add", "."], ["commit", "-m", "fixture"]]:
        subprocess.run(  # noqa: S603 — fixed executable; synthetic fixture arguments.
            [*git, *args], cwd=repo, check=True, capture_output=True, timeout=10
        )
    cache = repo / ".pytest_cache"
    cache.mkdir()
    (cache / "private-cache.txt").write_text("untracked synthetic cache")
    runner = tmp_path / "runner"
    runner.mkdir()
    environment = dict(os.environ, RUNNER_TEMP=str(runner))
    result = subprocess.run(  # noqa: S603 — workflow text is the tested fixed script.
        ["/bin/bash", "-eu", "-c", script],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["verified_files"] == len(expected)
    assert receipt["scope"] == "SOURCE_PREVIEW_NOT_RELEASE"
    assert not (repo / "MVP0-A_実装・検証報告.md").exists()
    # Matching names are insufficient: changed archive bytes must be rejected.
    import zipfile

    with zipfile.ZipFile(runner / "source-preview.zip", "w") as archive:
        for name, data in expected.items():
            archive.writestr("source-preview/" + name, data + b"corrupt")
    verify = script[script.index("python - <<'PY'") :]
    rejected = subprocess.run(  # noqa: S603 — same workflow verification script.
        ["/bin/bash", "-eu", "-c", verify],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert rejected.returncode != 0
    assert "SOURCE_ARCHIVE_BYTES_MISMATCH" in rejected.stderr
