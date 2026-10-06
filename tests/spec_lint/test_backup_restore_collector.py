"""`backup_restore` Collector の束縛・公開・子Process・述語の契約。

観測そのもの（本物の復元区間）は `tests/integration/p3/test_backup_restore_area_observation.py`
が確かめる。ここでは Collector が **誤ってPASSを公開しない** ことだけを、
一時Git Repositoryと差し替えた観測関数で速く確かめる。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import backup_restore_predicates as predicates
import collect_backup_restore_area as collector
from backup_restore_observation import ObservationHalted, RawLogs, run_bounded

pytestmark = pytest.mark.spec_lint

HASH = "sha256:" + "a" * 64
BINDING = {
    "design_file_sha256": HASH,
    "snapshot_design_sha256": HASH,
    "registry_snapshot_hash": "sha256:" + "b" * 64,
    "schema_set_hash": "sha256:" + "c" * 64,
    "migration_head": "11",
    "area_evidence_path": "migration/backup-restore-report.json",
    "area_required": True,
    "release_enabled": True,
}


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603 - fixed executable and argv list
        ["/usr/bin/git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": os.defpath,
            "HOME": str(cwd),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "collector-test",
            "GIT_AUTHOR_EMAIL": "collector@example.invalid",
            "GIT_COMMITTER_NAME": "collector-test",
            "GIT_COMMITTER_EMAIL": "collector@example.invalid",
        },
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "tracked.txt").write_text("measured source\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-q", "-m", "measured")
    return root


def _manifest(commit: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "release_scope": "MVP0-A",
        "implementation_commit_sha": commit,
        "source_tree_clean": True,
        "design_sha256": BINDING["design_file_sha256"],
        "registry_snapshot_hash": BINDING["registry_snapshot_hash"],
        "schema_set_hash": BINDING["schema_set_hash"],
        "migration_head": BINDING["migration_head"],
        "runtime_environment": {"environment_manifest_hash": "sha256:" + "d" * 64},
        "required_evidence_areas": [
            {
                "area": "backup_restore",
                "status": "UNVERIFIED",
                "evidence_path": None,
                "evidence_manifest_hash": None,
            },
            {"area": "environment", "status": "PASS", "evidence_path": "environment.json"},
        ],
    }
    body.update(overrides)
    return body


@pytest.fixture
def evidence(tmp_path: Path, repo: Path) -> tuple[Path, Path]:
    root = tmp_path / "evidence"
    root.mkdir()
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps(_manifest(_git(repo, "rev-parse", "HEAD"))), encoding="utf-8")
    return root, manifest


def _observe(*, repo: Path, workspace: Path, logs: RawLogs) -> dict[str, Any]:
    workspace.mkdir()
    logs.write("fake", "stdout", b"{}\n")
    return {
        "contract": "fake-observations",
        "operator_flow": {
            "resume": {"decision": {"state": "ACCEPTED"}, "execution": "NOT_PERFORMED"}
        },
    }


def _passing(observations: Any, *, raw_root: Path | None) -> dict[str, Any]:
    return {
        "contract": "fake",
        "status": "PASS",
        "items": {"x": "PASS"},
        "checks": [],
        "counts": {},
    }


def _build(repo: Path, evidence: tuple[Path, Path], tmp_path: Path, **kwargs: Any) -> Any:
    root, manifest = evidence
    options: dict[str, Any] = {
        "repo": repo,
        "evidence_root": root,
        "manifest_path": manifest,
        "workspace": tmp_path / "workspace",
        "collector_root": repo,
        "observe": _observe,
        "evaluate": _passing,
        "derive": lambda _repo, _scope: dict(BINDING),
        "environment_check": lambda _manifest, _root: None,
    }
    options.update(kwargs)
    return collector.build(**options)


def _trial_records(root: Path, name: str) -> list[Path]:
    return sorted((root / "trials" / "backup-restore").glob(f"*/{name}"))


# ---------------------------------------------------------------------------
# 公開
# ---------------------------------------------------------------------------


def test_publishes_new_evidence_and_bound_manifest(
    repo: Path, evidence: tuple[Path, Path], tmp_path: Path
) -> None:
    root, manifest = evidence
    result = _build(repo, evidence, tmp_path)
    out = root / BINDING["area_evidence_path"]
    attached = root / "manifest-with-backup-restore.json"
    assert result["status"] == "PASS"
    assert out.is_file() and attached.is_file()
    body = json.loads(out.read_text(encoding="utf-8"))
    assert body["implementation_commit_sha"] == _git(repo, "rev-parse", "HEAD")
    assert body["summary"]["resume"]["execution"] == "NOT_PERFORMED"
    row = next(
        r
        for r in json.loads(attached.read_text(encoding="utf-8"))["required_evidence_areas"]
        if r["area"] == "backup_restore"
    )
    assert row["evidence_manifest_hash"] == collector.verifier.sha256_file(out)
    assert row["status"] == "PASS"
    # 元の Manifest は書き換えない。
    assert (
        json.loads(manifest.read_text(encoding="utf-8"))["required_evidence_areas"][0][
            "evidence_manifest_hash"
        ]
        is None
    )
    assert _trial_records(root, "trial-published.json")
    assert not _trial_records(root, "trial-failed.json")


# ---------------------------------------------------------------------------
# OE-09: 束縛と出力先
# ---------------------------------------------------------------------------


def test_dirty_tree_is_refused_before_a_trial_starts(
    repo: Path, evidence: tuple[Path, Path], tmp_path: Path
) -> None:
    (repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(collector.EvidenceEmissionError, match="DIRTY_TREE"):
        _build(repo, evidence, tmp_path)
    assert not (evidence[0] / "trials").exists()


def test_manifest_commit_mismatch_is_refused(
    repo: Path, evidence: tuple[Path, Path], tmp_path: Path
) -> None:
    evidence[1].write_text(json.dumps(_manifest("0" * 40)), encoding="utf-8")
    with pytest.raises(collector.AreaCollectionError, match="COMMIT_MISMATCH"):
        _build(repo, evidence, tmp_path)
    assert not (evidence[0] / BINDING["area_evidence_path"]).exists()


@pytest.mark.parametrize(
    "field", ["design_sha256", "registry_snapshot_hash", "schema_set_hash", "migration_head"]
)
def test_manifest_binding_that_differs_from_the_repository_is_refused(
    repo: Path, evidence: tuple[Path, Path], tmp_path: Path, field: str
) -> None:
    commit = _git(repo, "rev-parse", "HEAD")
    evidence[1].write_text(json.dumps(_manifest(commit, **{field: "stale"})), encoding="utf-8")
    with pytest.raises(collector.AreaCollectionError, match=f"MANIFEST_{field.upper()}_MISMATCH"):
        _build(repo, evidence, tmp_path)


def test_manifest_area_already_bound_is_refused(
    repo: Path, evidence: tuple[Path, Path], tmp_path: Path
) -> None:
    commit = _git(repo, "rev-parse", "HEAD")
    manifest = _manifest(commit)
    manifest["required_evidence_areas"][0].update(status="PASS", evidence_manifest_hash=HASH)
    evidence[1].write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(collector.AreaCollectionError, match="MANIFEST_AREA_ALREADY_BOUND"):
        _build(repo, evidence, tmp_path)


@pytest.mark.parametrize("target", ["evidence", "attached", "workspace", "trial"])
def test_output_reuse_is_refused(
    repo: Path, evidence: tuple[Path, Path], tmp_path: Path, target: str
) -> None:
    root, _ = evidence
    expected = {
        "evidence": "EVIDENCE_REUSE",
        "attached": "MANIFEST_OUT_REUSE",
        "workspace": "WORKSPACE_REUSE",
        "trial": "TRIAL_REUSE",
    }[target]
    if target == "evidence":
        path = root / BINDING["area_evidence_path"]
        path.parent.mkdir(parents=True)
        path.write_text("{}\n", encoding="utf-8")
    elif target == "attached":
        (root / "manifest-with-backup-restore.json").write_text("{}\n", encoding="utf-8")
    elif target == "workspace":
        (tmp_path / "workspace").mkdir()
    else:
        (root / "trials" / "backup-restore" / "fixed").mkdir(parents=True)
    with pytest.raises(collector.AreaCollectionError, match=expected):
        _build(repo, evidence, tmp_path, trial_id="fixed")
    if target != "evidence":
        assert not (root / BINDING["area_evidence_path"]).exists()


def test_evidence_root_inside_the_repository_is_refused(
    repo: Path, evidence: tuple[Path, Path], tmp_path: Path
) -> None:
    inside = repo / "evidence"
    inside.mkdir()
    manifest = inside / "manifest.json"
    manifest.write_bytes(evidence[1].read_bytes())
    with pytest.raises(collector.AreaCollectionError, match="EVIDENCE_IN_REPOSITORY"):
        _build(repo, (inside, manifest), tmp_path)


def test_collector_must_run_from_the_measured_worktree(
    repo: Path, evidence: tuple[Path, Path], tmp_path: Path
) -> None:
    with pytest.raises(collector.AreaCollectionError, match="COLLECTOR_NOT_FROM_MEASURED_WORKTREE"):
        _build(repo, evidence, tmp_path, collector_root=tmp_path)


# ---------------------------------------------------------------------------
# OE-09/OE-10: 計測中の変更と失敗
# ---------------------------------------------------------------------------


def _changing(action: Any) -> Any:
    def observe(*, repo: Path, workspace: Path, logs: RawLogs) -> dict[str, Any]:
        result = _observe(repo=repo, workspace=workspace, logs=logs)
        action(repo)
        return result

    return observe


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("modify", "TREE_CHANGED_DURING_COLLECTION"),
        ("commit", "COMMIT_CHANGED_DURING_COLLECTION"),
    ],
)
def test_source_change_during_observation_is_not_published(
    repo: Path, evidence: tuple[Path, Path], tmp_path: Path, change: str, expected: str
) -> None:
    def act(root: Path) -> None:
        (root / "tracked.txt").write_text("changed\n", encoding="utf-8")
        if change == "commit":
            _git(root, "commit", "-q", "-am", "moved")

    root, _ = evidence
    with pytest.raises(collector.AreaCollectionError, match=expected):
        _build(repo, evidence, tmp_path, observe=_changing(act))
    assert not (root / BINDING["area_evidence_path"]).exists()
    assert not (root / "manifest-with-backup-restore.json").exists()
    failed = json.loads(_trial_records(root, "trial-failed.json")[0].read_text(encoding="utf-8"))
    assert failed["stage"] == "BIND_BEFORE_PUBLISH"
    assert failed["status"] == "FAILED_NOT_ADOPTABLE"


def test_manifest_change_during_observation_is_not_published(
    repo: Path, evidence: tuple[Path, Path], tmp_path: Path
) -> None:
    root, manifest = evidence
    observe = _changing(lambda _root: manifest.write_text("{}", encoding="utf-8"))
    with pytest.raises(collector.AreaCollectionError, match="MANIFEST_CHANGED_DURING_COLLECTION"):
        _build(repo, evidence, tmp_path, observe=observe)
    assert not (root / BINDING["area_evidence_path"]).exists()


@pytest.mark.parametrize("status", ["UNVERIFIED", "FAIL"])
def test_non_pass_evaluation_is_kept_only_in_the_trial(
    repo: Path, evidence: tuple[Path, Path], tmp_path: Path, status: str
) -> None:
    root, _ = evidence

    def evaluate(observations: Any, *, raw_root: Path | None) -> dict[str, Any]:
        return {**_passing(observations, raw_root=raw_root), "status": status}

    with pytest.raises(collector.AreaCollectionError, match=f"AREA_NOT_PASS: {status}"):
        _build(repo, evidence, tmp_path, evaluate=evaluate)
    assert not (root / BINDING["area_evidence_path"]).exists()
    assert not (root / "manifest-with-backup-restore.json").exists()
    assert _trial_records(root, "observations.json")
    assert _trial_records(root, "evaluation.json")
    assert not _trial_records(root, "candidate-backup-restore-report.json")


def test_failure_after_publication_withdraws_the_files(
    repo: Path, evidence: tuple[Path, Path], tmp_path: Path
) -> None:
    root, _ = evidence

    def dirty() -> None:
        (repo / "tracked.txt").write_text("changed after publish\n", encoding="utf-8")

    with pytest.raises(collector.AreaCollectionError, match="TREE_CHANGED_DURING_COLLECTION"):
        _build(repo, evidence, tmp_path, after_publish=dirty)
    assert not (root / BINDING["area_evidence_path"]).exists()
    assert not (root / "manifest-with-backup-restore.json").exists()
    failed = json.loads(_trial_records(root, "trial-failed.json")[0].read_text(encoding="utf-8"))
    assert failed["stage"] == "BIND_AFTER_PUBLISH"
    assert len(failed["published_then_withdrawn"]) == 2
    assert _trial_records(root, "withdrawn-backup-restore-report.json")
    assert not _trial_records(root, "trial-published.json")


def test_observation_error_leaves_a_failure_record(
    repo: Path, evidence: tuple[Path, Path], tmp_path: Path
) -> None:
    root, _ = evidence

    def broken(*, repo: Path, workspace: Path, logs: RawLogs) -> dict[str, Any]:
        raise RuntimeError("synthetic observer defect")

    with pytest.raises(RuntimeError, match="synthetic observer defect"):
        _build(repo, evidence, tmp_path, observe=broken)
    failed = json.loads(_trial_records(root, "trial-failed.json")[0].read_text(encoding="utf-8"))
    assert failed["stage"] == "OBSERVE"
    assert not (root / BINDING["area_evidence_path"]).exists()


# ---------------------------------------------------------------------------
# OE-08: 子Processと生ログ
# ---------------------------------------------------------------------------


def _logs(tmp_path: Path) -> RawLogs:
    raw = tmp_path / "raw"
    raw.mkdir()
    return RawLogs(raw, tmp_path)


def _python(code: str, tmp_path: Path, *, timeout: float = 30) -> dict[str, Any]:
    return run_bounded(
        _logs(tmp_path),
        "child",
        "synthetic child",
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        timeout=timeout,
    )


def test_child_timeout_halts_the_observation(tmp_path: Path) -> None:
    with pytest.raises(ObservationHalted, match="CHILD_TIMEOUT") as caught:
        _python("import time; time.sleep(60)", tmp_path, timeout=1)
    assert caught.value.partial is not None
    assert caught.value.partial["timed_out"] is True
    assert caught.value.partial["residual_process_group"] == "ABSENT"


def test_child_group_left_running_halts_the_observation(tmp_path: Path) -> None:
    # 孫Processは stdio を閉じて残る。子が終了Codeを返しても Group は空にならない。
    code = (
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
    )
    with pytest.raises(ObservationHalted, match="CHILD_PROCESS_GROUP_REMAINS"):
        _python(code, tmp_path)


def test_stderr_json_is_never_read_as_the_result(tmp_path: Path) -> None:
    record = _python('import sys; sys.stderr.write(\'{"state": "ACCEPTED"}\\n\')', tmp_path)
    assert record["returncode"] == 0
    assert record["stdout_json"] is None
    assert record["stdout_parse_error"] == "STDOUT_NOT_SINGLE_LINE"
    with pytest.raises(predicates.Violation, match="not a single JSON object"):
        predicates.cli_call(record, tmp_path, "stderr-only")


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (b"", "STDOUT_NOT_SINGLE_LINE"),
        (b'{"a": 1}', "STDOUT_NOT_SINGLE_LINE"),
        (b'{"a": 1}\n{"b": 2}\n', "STDOUT_NOT_SINGLE_LINE"),
        (b"[1, 2]\n", "STDOUT_NOT_OBJECT"),
        (b'{"a": NaN}\n', "STDOUT_NOT_JSON"),
        (b"\xff\n", "STDOUT_NOT_UTF8"),
    ],
)
def test_stdout_must_be_one_json_object_line(payload: bytes, error: str) -> None:
    assert predicates.parse_json_line(payload) == (None, error)


def _recorded(tmp_path: Path, stdout: bytes) -> dict[str, Any]:
    logs = _logs(tmp_path)
    parsed, error = predicates.parse_json_line(stdout)
    return {
        "argv": ["harness"],
        "returncode": 0,
        "timed_out": False,
        "residual_process_group": "ABSENT",
        "stdout": logs.write("call", "stdout", stdout),
        "stderr": logs.write("call", "stderr", b""),
        "stdout_json": parsed,
        "stdout_parse_error": error,
    }


def test_recorded_json_must_match_the_raw_bytes(tmp_path: Path) -> None:
    record = _recorded(tmp_path, b'{"state": "REJECTED"}\n')
    assert predicates.cli_call(record, tmp_path, "call") == {"state": "REJECTED"}
    record["stdout_json"] = {"state": "ACCEPTED"}
    with pytest.raises(predicates.Violation, match="contradicts the raw stdout"):
        predicates.cli_call(record, tmp_path, "call")


def test_raw_log_hash_mismatch_and_absence_are_not_pass(tmp_path: Path) -> None:
    record = _recorded(tmp_path, b'{"state": "ACCEPTED"}\n')
    (tmp_path / record["stdout"]["path"]).write_bytes(b'{"state": "REJECTED"}\n')
    with pytest.raises(predicates.Violation, match="hash mismatch"):
        predicates.cli_call(record, tmp_path, "call")
    (tmp_path / record["stdout"]["path"]).unlink()
    with pytest.raises(predicates.Missing, match="file missing"):
        predicates.cli_call(record, tmp_path, "call")
    with pytest.raises(predicates.Missing, match="raw log root"):
        predicates.cli_call(record, None, "call")


def test_timeout_record_is_not_pass(tmp_path: Path) -> None:
    record = _recorded(tmp_path, b'{"state": "ACCEPTED"}\n')
    record["timed_out"] = True
    with pytest.raises(predicates.Violation, match="timed out"):
        predicates.cli_call(record, tmp_path, "call")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"returned": True}, "completed instead of being refused"),
        ({"exception_type": "harness.domain.errors.HarnessError"}, "not by the operation gate"),
        ({"error_code": "APPROVAL_REQUIRED"}, "is not DEPLOY_DRAIN_REQUIRED"),
    ],
)
def test_refusal_must_come_from_the_operation_gate(change: dict[str, Any], message: str) -> None:
    attempt = {
        "returned": False,
        "exception_type": predicates.REFUSAL_EXCEPTION,
        "error_code": predicates.REFUSAL_CODE,
    }
    predicates.refused_attempt(attempt, "entry")
    with pytest.raises(predicates.Violation, match=message):
        predicates.refused_attempt({**attempt, **change}, "entry")


def test_area_status_is_never_pass_with_missing_observations() -> None:
    verdict = predicates.evaluate({}, raw_root=None)
    assert verdict["status"] == "UNVERIFIED"
    assert all(check["status"] != "PASS" for check in verdict["checks"])
    verdict = predicates.evaluate({"contract": predicates.CONTRACT}, raw_root=None)
    assert verdict["status"] != "PASS"


@pytest.mark.parametrize("target", ["evidence", "manifest", "receipt"])
def test_partial_publication_failure_never_leaves_a_pass_pair(
    repo: Path,
    evidence: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    root, _ = evidence
    out = root / BINDING["area_evidence_path"]
    attached = root / collector.ATTACHED_MANIFEST_NAME
    original_sync = collector._fsync_directory
    original_write = collector.write_exclusive
    failed = False

    def sync(path: Path) -> None:
        nonlocal failed
        published = out if target == "evidence" else attached
        if target != "receipt" and not failed and published.exists() and path == published.parent:
            failed = True
            raise OSError("injected published directory fsync failure")
        original_sync(path)

    def write(path: Path, data: bytes) -> None:
        if target == "receipt" and path.name == "trial-published.json":
            raise OSError("injected publication receipt failure")
        original_write(path, data)

    monkeypatch.setattr(collector, "_fsync_directory", sync)
    monkeypatch.setattr(collector, "write_exclusive", write)
    with pytest.raises((OSError, collector.AreaCollectionError)):
        _build(repo, evidence, tmp_path)
    assert not out.exists(), "failed publication retained PASS evidence"
    assert not attached.exists(), "failed publication retained a bound PASS manifest"
    assert _trial_records(root, "trial-failed.json")


def test_withdrawal_sync_failure_does_not_skip_the_other_publication(
    repo: Path,
    evidence: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = evidence
    original = collector._fsync_directory
    withdrawing = False

    def fail_after_publish() -> None:
        nonlocal withdrawing
        withdrawing = True
        raise RuntimeError("initial collection failure")

    def sync(path: Path) -> None:
        if withdrawing and path == root / "migration":
            raise OSError("withdrawal directory sync failure")
        original(path)

    monkeypatch.setattr(collector, "_fsync_directory", sync)
    with pytest.raises(collector.AreaCollectionError, match="PUBLISHED_STATE_UNKNOWN"):
        _build(repo, evidence, tmp_path, after_publish=fail_after_publish)
    assert not (root / BINDING["area_evidence_path"]).exists()
    assert not (root / collector.ATTACHED_MANIFEST_NAME).exists()
    failure = json.loads(_trial_records(root, "trial-failed.json")[0].read_text())
    assert failure["error_type"] == "RuntimeError"
    assert failure["published_state_unknown"] is True


def test_manifest_is_bound_to_the_bytes_actually_parsed(
    repo: Path,
    evidence: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, manifest = evidence
    original = Path.read_bytes
    swapped = False

    def read(path: Path) -> bytes:
        nonlocal swapped
        data = original(path)
        if path == manifest and not swapped:
            swapped = True
            changed = json.loads(data)
            changed["owner_annotation"] = "concurrent change"
            path.write_text(json.dumps(changed))
        return data

    monkeypatch.setattr(Path, "read_bytes", read)
    with pytest.raises(collector.AreaCollectionError, match="MANIFEST_CHANGED"):
        _build(repo, evidence, tmp_path)
    assert not (root / collector.ATTACHED_MANIFEST_NAME).exists()


@pytest.mark.parametrize("location", ["workspace_parent", "evidence_parent"])
def test_symlinked_output_parent_is_rejected_before_writing(
    repo: Path,
    evidence: tuple[Path, Path],
    tmp_path: Path,
    location: str,
) -> None:
    root, _ = evidence
    if location == "workspace_parent":
        link = tmp_path / "alias"
        link.symlink_to(repo, target_is_directory=True)
        options = {"workspace": link / "unexpected-workspace"}
    else:
        (root / "migration").symlink_to(repo, target_is_directory=True)
        options = {}
    before = sorted(p.name for p in repo.iterdir())
    with pytest.raises(collector.AreaCollectionError, match="SYMLINK"):
        _build(repo, evidence, tmp_path, **options)
    assert sorted(p.name for p in repo.iterdir()) == before
    assert not (root / "trials").exists()


def test_withdrawal_does_not_move_another_writers_replacement(
    repo: Path,
    evidence: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    root, _ = evidence
    attached = root / collector.ATTACHED_MANIFEST_NAME

    def replace() -> None:
        other = root / "other.json"
        other.write_text("other writer's bytes")
        other.replace(attached)
        raise RuntimeError("publication replaced")

    with pytest.raises(collector.AreaCollectionError, match="PUBLISHED_STATE_UNKNOWN"):
        _build(repo, evidence, tmp_path, after_publish=replace)
    assert attached.read_text() == "other writer's bytes"
    assert not (root / BINDING["area_evidence_path"]).exists()
