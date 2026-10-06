"""Maintenance UI: explicit startup opt-in and real signed, audited operations."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from harness.domain.errors import HarnessError
from harness.infrastructure.runtime_facade import HarnessRuntimeService
from harness.presentation.cli import main
from harness.presentation.local_ui.api import LocalUiApi, Request
from harness.presentation.local_ui.composition import build_services
from harness.presentation.local_ui.server import load_assets, start_server

REPO = Path(__file__).resolve().parents[3]
TOKEN = "c" * 64
ORIGIN = "http://127.0.0.1:65535"


def invoke(
    api: LocalUiApi,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    token: str = TOKEN,
    origin: str = ORIGIN,
) -> tuple[int, Any]:
    response = api.handle(
        Request(
            method=method,
            path=path,
            headers={
                "x-harness-session": token,
                "origin": origin,
                "content-type": "application/json",
            },
            body=json.dumps(body or {}).encode(),
        )
    )
    return response.status, json.loads(response.body)


def test_invalid_operator_identity_rejected_before_storage_creation(tmp_path: Path) -> None:
    with pytest.raises(HarnessError):
        build_services(
            repo_root=REPO,
            database_path=tmp_path / "absent/state.db",
            artifact_root=tmp_path / "absent/cas",
            operator_auth_session="local-uid:invalid",
        )
    assert not (tmp_path / "absent").exists()


def test_default_ui_is_read_only_even_with_valid_session_and_approval(tmp_path: Path) -> None:
    services = build_services(
        repo_root=REPO, database_path=tmp_path / "state.db", artifact_root=tmp_path / "cas"
    )
    try:
        runtime = HarnessRuntimeService(services.database_path)
        runtime.intake_gate().stop()
        api = LocalUiApi(services, origin=ORIGIN, session_token=TOKEN, assets=load_assets(TOKEN))
        status, review = invoke(api, "GET", "/api/operations")
        assert status == 200 and review["write_enabled"] is False
        assert review["resume"]["eligible"]
        status, result = invoke(
            api,
            "POST",
            "/api/operations/resume",
            {"review_hash": review["resume"]["review_hash"], "approve": True},
        )
        assert status == 409 and result["error"]["code"] == "APPROVAL_REQUIRED"
        assert not runtime.intake_gate().evaluate("probe").admitted
        assert services.connection.execute("SELECT COUNT(*) FROM approval_grant").fetchone()[0] == 0
    finally:
        services.close()


@pytest.mark.parametrize(
    "fault",
    ["origin", "session", "no-approval", "string-approval", "subject", "path", "stale-hash"],
)
def test_operation_endpoint_rejects_unapproved_or_changed_requests(
    tmp_path: Path, fault: str
) -> None:
    services = build_services(
        repo_root=REPO,
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "cas",
        operator_auth_session=f"local-uid:{os.getuid()}",
    )
    try:
        runtime = HarnessRuntimeService(services.database_path)
        runtime.intake_gate().stop()
        api = LocalUiApi(services, origin=ORIGIN, session_token=TOKEN, assets=load_assets(TOKEN))
        assert invoke(api, "GET", "/api/operations", token="invalid")[0] == 403
        review = invoke(api, "GET", "/api/operations")[1]
        body: dict[str, Any] = {"review_hash": review["resume"]["review_hash"], "approve": True}
        if fault == "no-approval":
            body.pop("approve")
        if fault == "string-approval":
            body["approve"] = "true"
        if fault == "subject":
            body["auth_session"] = f"local-uid:{os.getuid()}"
        if fault == "path":
            body["database"] = str(services.database_path)
        if fault == "stale-hash":
            runtime.intake_gate().stop()
        status, _ = invoke(
            api,
            "POST",
            "/api/operations/resume",
            body,
            token="invalid" if fault == "session" else TOKEN,
            origin="http://different.invalid" if fault == "origin" else ORIGIN,
        )
        assert status in (400, 403, 409, 422)
        assert not runtime.intake_gate().evaluate("probe").admitted
        assert services.connection.execute("SELECT COUNT(*) FROM approval_grant").fetchone()[0] == 0
    finally:
        services.close()


def test_real_http_orphan_reconciliation_requires_separate_fresh_resume(tmp_path: Path) -> None:
    services = build_services(
        repo_root=REPO,
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "cas",
        operator_auth_session=f"local-uid:{os.getuid()}",
    )
    runtime = HarnessRuntimeService(services.database_path)
    runtime.intake_gate().stop()
    # Fixed Python fixture and pytest-owned DB.
    child = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            """import os,sys
from pathlib import Path
from harness.infrastructure.runtime_facade import HarnessRuntimeService
with HarnessRuntimeService(Path(sys.argv[1])).intake_gate().admission('http-orphan', kind='effect'):
    os._exit(17)
""",
            str(services.database_path),
        ],
        check=False,
        capture_output=True,
        timeout=15,
    )
    assert child.returncode == 17, child.stderr
    server = start_server(
        build_api=lambda origin, token, assets: LocalUiApi(
            services, origin=origin, session_token=token, assets=assets
        ),
        port=0,
        max_body_bytes=services.max_body_bytes,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(server.url, timeout=5) as response:  # noqa: S310
            html = response.read().decode()
        match = re.search(r'name="harness-session" content="([a-f0-9]+)"', html)
        assert match and 'id="operations-panel"' in html
        token = match.group(1)

        def http(path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
            request = urllib.request.Request(  # noqa: S310 - local test server only
                server.url + path,
                data=None if body is None else json.dumps(body).encode(),
                headers={
                    "X-Harness-Session": token,
                    "Origin": server.url,
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read())

        status, before = http("/api/operations")
        assert status == 200 and before["reconcile"]["eligible"]
        assert not before["resume"]["eligible"]
        approval = {"review_hash": before["reconcile"]["review_hash"], "approve": True}
        status, result = http("/api/operations/reconcile", approval)
        assert status == 200 and result["mode"] == "DRAINING" and not result["resumed"]
        assert runtime.verify_ledger(result["operation_id"])
        assert http("/api/operations/reconcile", approval)[0] == 409
        assert (
            http(
                "/api/operations/resume",
                {"review_hash": before["resume"]["review_hash"], "approve": True},
            )[0]
            == 409
        )
        status, fresh = http("/api/operations")
        assert status == 200 and fresh["resume"]["eligible"]
        status, result = http(
            "/api/operations/resume",
            {"review_hash": fresh["resume"]["review_hash"], "approve": True},
        )
        assert status == 200 and result["resumed"]
        assert runtime.verify_ledger(result["operation_id"])
        with runtime.intake_gate().admission("new-request"):
            assert runtime.intake_gate().active_count() == 1
        assert (
            services.connection.execute(
                "SELECT COUNT(*) FROM approval_grant WHERE status='CONSUMED'"
            ).fetchone()[0]
            == 2
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        services.close()
        assert not thread.is_alive()


def test_cli_reconciliation_uses_explicit_review_and_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database, cas = tmp_path / "state.db", tmp_path / "cas"
    runtime = HarnessRuntimeService(database)
    runtime.migrate(recorded_at="2026-09-24T00:00:00Z")
    runtime.intake_gate().stop()
    common = ["--database", str(database), "--artifact-root", str(cas), "--repo-root", str(REPO)]
    assert main(["deploy", "inspect", *common, "--operation", "reconcile"]) == 0
    review = json.loads(capsys.readouterr().out)
    assert review["operation"] == "RECONCILE_OPERATIONS" and not review["eligible"]
    assert (
        main(
            [
                "deploy",
                "reconcile",
                *common,
                "--review-hash",
                review["review_hash"],
                "--auth-session",
                f"local-uid:{os.getuid()}",
                "--reason",
                "reconcile-quiescent-source",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["error_code"] == "DEPLOY_DRAIN_REQUIRED"
