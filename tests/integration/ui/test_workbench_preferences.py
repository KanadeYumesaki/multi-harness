"""Public preferences: no inherited owner answers, no CLI or implicit approval."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from tests.integration.ui.test_workbench_api import _api, _get, _post
from tests.integration.workbench.conftest import make_env
from tests.support.workbench_fixtures import StubBoundaryProbe

from harness.infrastructure.sqlite.connection_factory import ConnectionFactory
from harness.infrastructure.sqlite.migrations import SCHEMA_VERSION, migrate
from harness.presentation.local_ui.composition import WorkbenchSetup, build_services

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]
URL = "/api/workbench/preferences"


@pytest.fixture
def env(tmp_path):
    built = make_env(tmp_path)
    try:
        yield built
    finally:
        built.services.close()


def selection(**changes):
    return {
        "provider_id": "codex",
        "model_id": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "approved": True,
        "expected_version": 0,
        **changes,
    }


def test_preference_save_is_explicit_and_has_no_execution_side_effects(env, monkeypatch):
    api = _api(env.services)
    status, initial = _get(api, URL)
    assert status == 200 and initial["configured"] is False and initial["version"] == 0

    def denied(*args, **kwargs):
        raise AssertionError("Preference operations must not start processes")

    monkeypatch.setattr(subprocess, "Popen", denied)
    assert _post(api, URL, selection(approved=False))[0] == 409
    assert _post(api, URL, selection(approved="true"))[0] == 409
    status, saved = _post(api, URL, selection())
    assert status == 200, saved
    assert saved["selection"]["reasoning_effort"] == "high"
    assert saved["grants_send_approval"] is False
    assert saved["grants_apply_approval"] is False
    assert env.runner.calls == []
    for query in (
        "SELECT COUNT(*) FROM approval_grant",
        "SELECT COUNT(*) FROM cli_invocation_journal",
        "SELECT COUNT(*) FROM workbench_session",
    ):
        assert env.services.connection.execute(query).fetchone()[0] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"provider_id": "unknown"},
        {"model_id": "bad model"},
        {"model_id": "m" * 97},
        {"reasoning_effort": "ultra"},
        {"reasoning_effort": []},
        {"model_id": "unlisted-model", "reasoning_effort": "high"},
        {"expected_version": -1},
        {"expected_version": True},
        {"command": "anything"},
    ],
)
def test_invalid_preferences_never_persist(env, changes):
    api = _api(env.services)
    assert 400 <= _post(api, URL, selection(**changes))[0] < 500
    assert _get(api, URL)[1]["configured"] is False
    assert env.runner.calls == []


def test_preference_route_requires_origin_and_session(env):
    api = _api(env.services)
    assert _get(api, URL, token=None)[0] == 403
    assert _post(api, URL, selection(), origin="http://other.invalid")[0] == 403
    assert _post(api, URL, selection(), token="wrong")[0] == 403
    assert _get(api, URL)[1]["configured"] is False


def test_concurrent_version_and_tampered_storage_fail_closed(env):
    api = _api(env.services)
    assert _post(api, URL, selection())[0] == 200
    assert _post(api, URL, selection(model_id="other-model", reasoning_effort=None))[0] == 409
    assert _get(api, URL)[1]["selection"]["model_id"] == "gpt-5.6-sol"
    # Inject a storage fault through the same factory, preserving the stored checksum.
    factory = ConnectionFactory(env.services.database_path)
    connection = factory.connect()
    try:
        with factory.begin_immediate(connection):
            connection.execute("UPDATE workbench_preference SET model_id=?", ("changed",))
        assert _get(api, URL)[0] >= 400
        assert _post(api, URL, selection(expected_version=1))[0] >= 400
    finally:
        connection.close()


def test_preferences_survive_service_restart_and_do_not_approve_a_plan(env):
    status, response = _post(_api(env.services), URL, selection())
    assert status == 200, response
    git = shutil.which("git")
    assert git is not None
    subprocess.run(  # noqa: S603 - isolated fixture repository only
        [
            git,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-m",
            "advance fixture head",
        ],
        cwd=env.worktree,
        check=True,
        capture_output=True,
        timeout=30,
    )
    env.services.close()
    env.services = build_services(
        repo_root=ROOT,
        database_path=env.root / "db/state.sqlite3",
        artifact_root=env.root / "cas",
        workbench=WorkbenchSetup(
            workspace=env.worktree,
            runtime_profile=env.profile,
            workspace_label="demo-worktree",
            auth_session="test-user",
            runner=env.runner,
            boundary_probe=StubBoundaryProbe(),
        ),
    )
    api = _api(env.services)
    saved = _get(api, URL)[1]
    assert saved["configured"] is True and saved["version"] == 1
    assert saved["selection"]["reasoning_effort"] == "high"
    assert env.runner.calls == []
    status, session = _post(
        api,
        "/api/workbench/sessions",
        {
            "provider_id": "codex",
            "model_id": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "relative_path": "hello.py",
            "instruction": "Add a comment.",
        },
    )
    assert status == 201, session
    assert (
        _post(
            api, URL, selection(model_id="another-model", reasoning_effort=None, expected_version=1)
        )[0]
        == 200
    )
    preserved = _get(api, "/api/workbench/sessions/" + session["session_id"])[1]
    assert preserved["model_id"] == "gpt-5.6-sol"
    assert preserved["reasoning_effort"] == "high"
    assert preserved["execution_plan_hash"] == session["execution_plan_hash"]
    status, _ = _post(
        api,
        "/api/workbench/sessions/" + session["session_id"] + "/send",
        {"execution_plan_hash": session["execution_plan_hash"]},
    )
    assert status == 409
    assert env.runner.calls == []


def test_public_repo_without_owner_packages_starts_unconfigured(tmp_path):
    public = tmp_path / "public"
    public.mkdir()
    # No private history, decision packages or audit reports are copied.
    for name in ("design-source", "schemas", "spec", "src/harness/masking/ucd"):
        shutil.copytree(ROOT / name, public / name)
    shutil.copy2(ROOT / "registry-snapshot.json", public / "registry-snapshot.json")
    snapshot = json.loads((public / "registry-snapshot.json").read_bytes())
    design = "design-v" + snapshot["design_version"] + "-runtime-go.md"
    shutil.copy2(ROOT / design, public / design)
    services = build_services(
        repo_root=public,
        database_path=tmp_path / "db/state.sqlite3",
        artifact_root=tmp_path / "cas",
    )
    try:
        api = _api(services)
        for route in (
            "/api/providers",
            "/api/provider-values",
            "/api/route-profile",
            "/api/execution-modes",
        ):
            status, response = _get(api, route)
            assert status == 200, (route, response)
        assert _get(api, "/api/provider-values")[1]["package_status"] == "NOT_CONFIGURED"
        assert _get(api, "/api/provider-values")[1]["save_allowed"] is False
        assert _get(api, "/api/route-profile")[1]["active_route_policy"] is False
        assert (
            _post(api, "/api/provider-values", {"approve_save": True, "confirmations": {}})[0]
            == 422
        )
        assert not (public / "docs/decision").exists()
    finally:
        services.close()


def test_upgrade_preserves_existing_state_and_creates_empty_preferences(tmp_path, monkeypatch):
    import harness.infrastructure.sqlite.migrations as migration

    (tmp_path / "db").mkdir()
    factory = ConnectionFactory(tmp_path / "db/state.sqlite3")
    previous_version = SCHEMA_VERSION - 1
    with monkeypatch.context() as previous:
        previous.setattr(migration, "SCHEMA_VERSION", previous_version)
        previous.setattr(migration, "_MIGRATIONS", migration._MIGRATIONS[:-1])
        assert migrate(factory, recorded_at="2026-09-29T00:00:00Z") == previous_version
    connection = factory.connect()
    try:
        with factory.begin_immediate(connection):
            connection.execute(
                "INSERT INTO workbench_session VALUES(?,?,?,?)",
                ("synthetic-preserved", "DRAFTED", 1, "sha256:" + "0" * 64),
            )
        before = [tuple(row) for row in connection.execute("SELECT * FROM schema_migration")]
        old_rows = [tuple(row) for row in connection.execute("SELECT * FROM workbench_session")]
    finally:
        connection.close()
    assert migrate(factory, recorded_at="2026-09-29T01:00:00Z") == SCHEMA_VERSION
    connection = factory.connect()
    try:
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM schema_migration WHERE version<?", (SCHEMA_VERSION,)
            )
        ] == before
        assert [
            tuple(row) for row in connection.execute("SELECT * FROM workbench_session")
        ] == old_rows
        assert connection.execute("SELECT COUNT(*) FROM workbench_preference").fetchone()[0] == 0
    finally:
        connection.close()


def test_credential_shaped_model_is_rejected_without_storage_or_echo(env):
    value = "sk-" + "aB9" * 16
    status, result = _post(
        _api(env.services), URL, selection(model_id=value, reasoning_effort=None)
    )
    assert status == 422
    assert value not in json.dumps(result)
    assert _get(_api(env.services), URL)[1]["configured"] is False


def test_generated_scope_digest_is_not_scanned_as_user_content(env, monkeypatch):
    from harness.application.workbench_service import WorkbenchService
    from harness.domain.hashing import ContentHash

    monkeypatch.setattr(
        WorkbenchService,
        "_preference_scope",
        lambda self: ContentHash.parse("sha256:" + "a" * 20 + "123456789012" + "b" * 32),
    )
    status, response = _post(_api(env.services), URL, selection())
    assert status == 200, response
    assert env.runner.calls == []


@pytest.mark.parametrize("reasoning_effort", ["low", "high", None])
def test_session_history_keeps_frozen_reasoning_selection(
    env: Any, reasoning_effort: str | None
) -> None:
    api = _api(env.services)
    status, session = _post(
        api,
        "/api/workbench/sessions",
        {
            "provider_id": "codex",
            "model_id": "gpt-5.6-sol",
            "reasoning_effort": reasoning_effort,
            "relative_path": "hello.py",
            "instruction": "Add a comment.",
        },
    )
    assert status == 201, session
    # Current preferences must not replace the frozen value in an older session.
    new_effort = None if reasoning_effort is not None else "high"
    assert _post(api, URL, selection(reasoning_effort=new_effort))[0] == 200
    status, overview = _get(api, "/api/workbench")
    assert status == 200, overview
    row = next(r for r in overview["sessions"] if r["session_id"] == session["session_id"])
    assert row["reasoning_effort"] == reasoning_effort
    for suffix in ("", "/confirmation"):
        status, view = _get(api, "/api/workbench/sessions/" + session["session_id"] + suffix)
        assert status == 200, view
        assert view["reasoning_effort"] == row["reasoning_effort"]
    assert env.runner.calls == []
    for query in (
        "SELECT COUNT(*) FROM approval_grant",
        "SELECT COUNT(*) FROM cli_invocation_journal",
    ):
        assert env.services.connection.execute(query).fetchone()[0] == 0
