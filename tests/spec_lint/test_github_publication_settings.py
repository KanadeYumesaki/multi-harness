"""Synthetic GitHub API responses are contract tests, never live configuration proof."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

import check_github_publication_settings as inspection

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "synthetic-owner/synthetic-source"
REPOSITORY_ID = 301
APP_ID = 302
SOURCE = {"commit": "a" * 40, "tree": "b" * 40, "history_commits": 1}


def good_observations(
    contract: dict[str, Any], source: dict[str, Any] | None = None
) -> dict[str, inspection.Observation]:
    source = source or SOURCE
    checks = [{"context": n, "app_id": APP_ID} for n in contract["required_checks"]]
    run = {
        "id": 401,
        "run_number": 1,
        "run_attempt": 2,
        "status": "completed",
        "conclusion": "success",
        "head_sha": source["commit"],
        "head_branch": "main",
        "path": ".github/workflows/ci.yml",
        "event": "push",
    }
    jobs = [
        {
            "id": 600 + i,
            "name": n,
            "run_id": 401,
            "run_attempt": 2,
            "status": "completed",
            "conclusion": "success",
            "check_run_url": f"https://api.github.com/repos/{REPOSITORY}/check-runs/{500 + i}",
        }
        for i, n in enumerate(contract["full_checks"])
    ]
    check_runs = [
        {
            "id": 500 + i,
            "name": n,
            "head_sha": source["commit"],
            "status": "completed",
            "conclusion": "success",
            "app": {"id": APP_ID},
        }
        for i, n in enumerate(contract["full_checks"])
    ]
    payloads = {
        "repository": {
            "full_name": REPOSITORY,
            "id": REPOSITORY_ID,
            "visibility": "public",
            "private": False,
            "archived": False,
            "default_branch": "main",
            "security_and_analysis": {
                "secret_scanning": {"status": "enabled"},
                "secret_scanning_push_protection": {"status": "enabled"},
            },
        },
        "actions": {"enabled": True, "allowed_actions": "selected", "sha_pinning_required": True},
        "allowed_actions": {
            "github_owned_allowed": False,
            "verified_allowed": False,
            "patterns_allowed": contract["actions"],
        },
        "token": {
            "default_workflow_permissions": "read",
            "can_approve_pull_request_reviews": False,
        },
        "fork": {"approval_policy": "all_external_contributors"},
        "protection": {
            "required_status_checks": {
                "strict": True,
                "checks": checks,
                "contexts": contract["required_checks"],
            },
            "enforce_admins": {"enabled": True},
            "allow_force_pushes": {"enabled": False},
            "allow_deletions": {"enabled": False},
            "required_pull_request_reviews": {
                "required_approving_review_count": 0,
                "dismiss_stale_reviews": True,
                "bypass_pull_request_allowances": {"users": [], "teams": [], "apps": []},
            },
        },
        "rulesets": [],
        "vulnerability_reporting": {"enabled": True},
        "branch": {"commit": {"sha": source["commit"]}},
        "commit": {"sha": source["commit"], "commit": {"tree": {"sha": source["tree"]}}},
        "app": {"id": APP_ID, "slug": "github-actions"},
        "latest_run": run,
        "workflow_runs": {"workflow_runs": [run]},
        "jobs": {"jobs": jobs},
        "check_runs": {"check_runs": check_runs},
    }
    return {key: inspection.Observation(200, value) for key, value in payloads.items()}


def test_real_workflow_names_actions_and_dynamic_matrix_are_derived() -> None:
    contract = inspection.workflow_contract(ROOT)
    assert "quality (py3.11)" not in contract["required_checks"]
    assert "quality (py3.11)" in contract["full_checks"]
    assert "quality (py3.12)" in contract["required_checks"]
    assert "nightly-deep" in contract["required_checks"]
    assert "release-tag-signature" not in contract["required_checks"]
    workflows = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    expected = sorted(
        {
            step["uses"]
            for job in workflows["jobs"].values()
            for step in job["steps"]
            if "uses" in step
        }
    )
    assert contract["actions"] == expected


def test_complete_synthetic_settings_are_accepted_but_source_survey_never_passes() -> None:
    contract = inspection.workflow_contract(ROOT)
    observed = good_observations(contract)
    assert (
        inspection.evaluate(observed, contract, REPOSITORY, REPOSITORY_ID, SOURCE, "public-copy")[
            "status"
        ]
        == "PASS"
    )
    assert (
        inspection.evaluate(observed, contract, REPOSITORY, REPOSITORY_ID, SOURCE, "source-survey")[
            "status"
        ]
        == "BLOCKED"
    )


MUTATIONS = [
    ("repository", ("id",), 999),
    ("repository", ("full_name",), "other/synthetic"),
    ("repository", ("visibility",), "private"),
    ("repository", ("private",), True),
    ("repository", ("archived",), True),
    ("repository", ("default_branch",), "other"),
    ("repository", ("security_and_analysis",), {}),
    ("repository", ("security_and_analysis", "secret_scanning", "status"), "disabled"),
    (
        "repository",
        ("security_and_analysis", "secret_scanning_push_protection", "status"),
        "disabled",
    ),
    ("actions", ("enabled",), False),
    ("actions", ("allowed_actions",), "all"),
    ("actions", ("sha_pinning_required",), False),
    ("actions", ("sha_pinning_required",), 1),
    ("allowed_actions", ("github_owned_allowed",), True),
    ("allowed_actions", ("verified_allowed",), True),
    ("allowed_actions", ("patterns_allowed",), ["actions/*"]),
    ("token", ("default_workflow_permissions",), "write"),
    ("token", ("can_approve_pull_request_reviews",), True),
    ("fork", ("approval_policy",), "first_time_contributors"),
    ("vulnerability_reporting", ("enabled",), False),
    ("app", ("slug",), "synthetic-other-app"),
    ("app", ("id",), -1),
    ("protection", ("enforce_admins", "enabled"), False),
    ("protection", ("allow_force_pushes", "enabled"), True),
    ("protection", ("allow_deletions", "enabled"), True),
    ("protection", ("required_status_checks", "strict"), False),
    ("protection", ("required_status_checks", "checks"), []),
    ("protection", ("required_status_checks", "contexts"), []),
    ("protection", ("required_pull_request_reviews", "required_approving_review_count"), 1),
    (
        "protection",
        ("required_pull_request_reviews", "bypass_pull_request_allowances", "users"),
        [{"id": 909}],
    ),
    ("rulesets", (), [{"id": 901, "enforcement": "evaluate"}]),
    ("branch", ("commit", "sha"), "c" * 40),
    ("commit", ("commit", "tree", "sha"), "c" * 40),
    ("latest_run", ("status",), "in_progress"),
    ("latest_run", ("conclusion",), "failure"),
    ("latest_run", ("head_sha",), "c" * 40),
    ("latest_run", ("run_attempt",), 3),
    ("latest_run", ("event",), "schedule"),
    ("latest_run", ("path",), ".github/workflows/other.yml"),
    ("latest_run", ("head_branch",), "other"),
    ("jobs", ("jobs",), []),
    ("check_runs", ("check_runs",), []),
]


@pytest.mark.parametrize("key,path,value", MUTATIONS)
def test_each_unsafe_or_missing_setting_rejects(
    key: str, path: tuple[str, ...], value: Any
) -> None:
    contract = inspection.workflow_contract(ROOT)
    observed = good_observations(contract)
    target = observed[key].data
    if not path:
        observed[key] = inspection.Observation(200, value)
    else:
        for name in path[:-1]:
            target = target[name]
        target[path[-1]] = value
    assert (
        inspection.evaluate(observed, contract, REPOSITORY, REPOSITORY_ID, SOURCE, "public-copy")[
            "status"
        ]
        == "BLOCKED"
    )


@pytest.mark.parametrize(
    "status,reason",
    [
        (403, "UNAVAILABLE_PLAN"),
        (404, "HTTP_ERROR_UNVERIFIED"),
        (422, "UNAVAILABLE_PRIVATE_REPOSITORY"),
        (None, "API_START_OR_TIMEOUT"),
    ],
)
@pytest.mark.parametrize(
    "key",
    [
        "repository",
        "actions",
        "fork",
        "protection",
        "rulesets",
        "vulnerability_reporting",
        "app",
        "latest_run",
    ],
)
def test_api_denial_or_missing_never_means_configured(
    key: str, status: int | None, reason: str
) -> None:
    contract = inspection.workflow_contract(ROOT)
    observed = good_observations(contract)
    observed[key] = inspection.Observation(status, reason=reason)
    result = inspection.evaluate(
        observed, contract, REPOSITORY, REPOSITORY_ID, SOURCE, "public-copy"
    )
    assert result["status"] == "BLOCKED"
    assert any(row["status"] == "UNVERIFIED" and row["reason"] == reason for row in result["rows"])


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_app",
        "wrong_head",
        "old_attempt",
        "skipped",
        "cancelled",
        "foreign_url",
        "duplicate",
        "other_run",
    ],
)
def test_ci_must_be_same_commit_app_and_latest_attempt(mutation: str) -> None:
    contract = inspection.workflow_contract(ROOT)
    observed = good_observations(contract)
    job = observed["jobs"].data["jobs"][0]
    check = observed["check_runs"].data["check_runs"][0]
    if mutation == "wrong_app":
        check["app"]["id"] = -1
    elif mutation == "wrong_head":
        check["head_sha"] = "c" * 40
    elif mutation == "old_attempt":
        job["run_attempt"] = 1
    elif mutation in {"skipped", "cancelled"}:
        job["conclusion"] = mutation
    elif mutation == "foreign_url":
        job["check_run_url"] = "https://example.invalid/check-runs/500"
    elif mutation == "duplicate":
        observed["jobs"].data["jobs"][1]["name"] = job["name"]
    else:
        job["run_id"] = 400
    result = inspection.evaluate(
        observed, contract, REPOSITORY, REPOSITORY_ID, SOURCE, "public-copy"
    )
    assert next(row for row in result["rows"] if row["id"] == "remote_full_ci")["status"] == "FAIL"


def test_context_order_is_irrelevant_and_any_app_binding_is_rejected() -> None:
    contract = inspection.workflow_contract(ROOT)
    observed = good_observations(contract)
    settings = observed["protection"].data["required_status_checks"]
    settings["contexts"] = list(reversed(settings["contexts"]))
    settings["checks"] = list(reversed(settings["checks"]))
    assert (
        inspection.evaluate(observed, contract, REPOSITORY, REPOSITORY_ID, SOURCE, "public-copy")[
            "status"
        ]
        == "PASS"
    )
    settings["checks"][0]["app_id"] = -1
    assert (
        inspection.evaluate(observed, contract, REPOSITORY, REPOSITORY_ID, SOURCE, "public-copy")[
            "status"
        ]
        == "BLOCKED"
    )


def test_multi_commit_history_cannot_be_a_public_copy() -> None:
    contract = inspection.workflow_contract(ROOT)
    source = dict(SOURCE, history_commits=2)
    assert (
        inspection.evaluate(
            good_observations(contract), contract, REPOSITORY, REPOSITORY_ID, source, "public-copy"
        )["status"]
        == "BLOCKED"
    )


def test_parser_get_only_redacts_headers_stderr_and_unknown_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        captured.update(argv=argv, **kwargs)
        payload = (
            b"HTTP/2.0 200 OK\nX-OAuth-Scopes: SYNTHETIC_PRIVATE_SCOPE\n\n"
            b'{"enabled": true, "secret": "SYNTHETIC_DO_NOT_SAVE"}'
        )
        return subprocess.CompletedProcess(argv, 0, payload, b"SYNTHETIC_EXTERNAL_STDERR")

    monkeypatch.setattr(inspection.subprocess, "run", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC_CREDENTIAL")
    monkeypatch.setenv("GH_DEBUG", "api")
    reader = inspection.GithubReader(Path("/synthetic/gh"), "2022-11-28")
    observed = reader.get("repos/synthetic-owner/synthetic-source/actions/permissions")
    assert observed.status == 200
    assert captured["argv"][captured["argv"].index("--method") + 1] == "GET"
    assert captured["argv"][captured["argv"].index("--hostname") + 1] == "github.com"
    assert captured["timeout"] == 30 and captured["input"] == b""
    assert "shell" not in captured
    assert "ANTHROPIC_API_KEY" not in captured["env"] and "GH_DEBUG" not in captured["env"]
    saved = json.dumps(reader.requests)
    assert not any(
        value in saved
        for value in [
            "SYNTHETIC_DO_NOT_SAVE",
            "SYNTHETIC_PRIVATE_SCOPE",
            "SYNTHETIC_EXTERNAL_STDERR",
            "SYNTHETIC_CREDENTIAL",
        ]
    )


@pytest.mark.parametrize(
    "payload,rc",
    [(b"not HTTP", 0), (b"HTTP/2.0 200 OK\n\nnot JSON", 0), (b"HTTP/2.0 200 OK\n\n{}", 1)],
)
def test_invalid_or_nonzero_response_is_unverified(
    monkeypatch: pytest.MonkeyPatch, payload: bytes, rc: int
) -> None:
    monkeypatch.setattr(
        inspection.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, rc, payload, b""),
    )
    response = inspection.GithubReader(Path("/synthetic/gh"), "2022-11-28").get(
        "repos/synthetic-owner/synthetic-source"
    )
    assert response.reason != "OBSERVED" and response.data is None


def test_transport_failure_retries_only_get_then_opens_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def timeout(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, 30)

    monkeypatch.setattr(inspection.subprocess, "run", timeout)
    reader = inspection.GithubReader(Path("/synthetic/gh"), "2022-11-28")
    for _ in range(5):
        assert reader.get("apps/github-actions").data is None
    assert len(calls) == 3
    assert reader.get("apps/github-actions").reason == "API_CIRCUIT_OPEN"


@pytest.mark.parametrize("kind", ["partial", "duplicate", "changed_total", "limit", "empty"])
def test_pagination_cannot_hide_missing_or_duplicate_results(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    reader = inspection.GithubReader(Path("/synthetic/gh"), "2022-11-28")

    def get(endpoint: str) -> inspection.Observation:
        page = int(endpoint.split("page=")[-1])
        if kind == "empty":
            return inspection.Observation(200, {"total_count": 0, "jobs": []})
        count = 200 if kind != "limit" else 2100
        rows = [{"id": (page - 1) * 100 + i} for i in range(100)]
        if kind == "partial":
            rows = rows[:1]
        if kind == "duplicate":
            rows[-1] = copy.deepcopy(rows[0])
        if kind == "changed_total" and page > 1:
            count = 201
        return inspection.Observation(200, {"total_count": count, "jobs": rows})

    monkeypatch.setattr(reader, "get", get)
    observed = reader.pages(
        "repos/synthetic-owner/synthetic-source/actions/runs/1/jobs?filter=latest", "jobs"
    )
    assert (observed.reason == "OBSERVED") == (kind == "empty")


def test_collector_uses_latest_run_attempt_not_an_older_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = inspection.workflow_contract(ROOT)
    observed = good_observations(contract)
    runs = [copy.deepcopy(observed["latest_run"].data) for _ in range(2)]
    runs[1].update(id=402, run_number=2, run_attempt=1, status="in_progress", conclusion=None)
    reader = inspection.GithubReader(Path("/synthetic/gh"), "2022-11-28")
    paths: list[str] = []
    monkeypatch.setattr(
        reader, "get", lambda endpoint: inspection.Observation(404, reason="HTTP_ERROR_UNVERIFIED")
    )

    def pages(endpoint: str, key: str) -> inspection.Observation:
        paths.append(endpoint)
        return inspection.Observation(200, {key: runs if key == "workflow_runs" else []})

    monkeypatch.setattr(reader, "pages", pages)
    result = inspection.collect(reader, REPOSITORY, "main", SOURCE["commit"])
    assert result["latest_run"].data["id"] == 402
    assert any("/402/attempts/1/jobs" in path for path in paths)
    assert (
        inspection.evaluate(result, contract, REPOSITORY, REPOSITORY_ID, SOURCE, "public-copy")[
            "status"
        ]
        == "BLOCKED"
    )


def synthetic_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in [
        "ci/github-publication-policy.json",
        ".github/workflows/ci.yml",
        "tools/ci_scope.py",
        "tools/check_github_publication_settings.py",
    ]:
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    for args in [
        ["git", "init", "-b", "main"],
        ["git", "add", "."],
        [
            "git",
            "-c",
            "user.name=Synthetic",
            "-c",
            "user.email=synthetic@example.invalid",
            "commit",
            "-m",
            "synthetic fixture",
        ],
    ]:
        subprocess.run(args, cwd=repo, capture_output=True, check=True, timeout=10)  # noqa: S603 — synthetic Git argv.
    return repo


@pytest.mark.parametrize("change", ["scope", "weak_policy", "skip_job"])
def test_contract_drift_fails_without_executing_other_checkout(tmp_path: Path, change: str) -> None:
    repo = synthetic_repo(tmp_path)
    if change == "scope":
        (repo / "tools/ci_scope.py").write_text(
            "FULL_PYTHONS = ['3.11']\nLIGHT_PYTHONS = ['3.12']\n"
            "raise RuntimeError('must not execute')\n"
        )
    elif change == "weak_policy":
        policy = json.loads((repo / "ci/github-publication-policy.json").read_bytes())
        policy["required_jobs"] = ["plan"]
        (repo / "ci/github-publication-policy.json").write_text(json.dumps(policy))
    else:
        path = repo / ".github/workflows/ci.yml"
        document = yaml.safe_load(path.read_text())
        document["jobs"]["spec"]["if"] = "false"
        path.write_text(yaml.safe_dump(document))
    with pytest.raises(inspection.InspectionFailed):
        inspection.workflow_contract(repo)


@pytest.mark.parametrize("changed", ["remote", "local", "malformed"])
def test_receipt_keeps_failure_and_never_overwrites_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    repo = synthetic_repo(tmp_path)
    executable = tmp_path / "gh"
    executable.write_text("synthetic tool bytes")
    source = inspection.source_binding(repo)
    contract = inspection.workflow_contract(repo)
    observed = good_observations(contract, source)
    calls = 0

    def collect(reader: inspection.GithubReader, *args: Any) -> dict[str, inspection.Observation]:
        nonlocal calls
        calls += 1
        current = copy.deepcopy(observed)
        if calls == 2:
            if changed == "remote":
                current["token"].data["default_workflow_permissions"] = "write"
            elif changed == "local":
                (repo / "tools/ci_scope.py").write_text("changed")
        if changed == "malformed":
            current["repository"] = inspection.Observation(200, {"full_name": None})
        return current

    monkeypatch.setattr(inspection, "collect", collect)
    out = tmp_path / "evidence"
    receipt = inspection.run_inspection(
        repo, REPOSITORY, REPOSITORY_ID, "public-copy", executable, out
    )
    assert receipt["status"] == "BLOCKED"
    saved = (out / "receipt.json").read_bytes()
    assert json.loads(saved)["error"]
    with pytest.raises(inspection.InspectionFailed, match="OUTPUT_MUST_BE_NEW"):
        inspection.run_inspection(repo, REPOSITORY, REPOSITORY_ID, "public-copy", executable, out)
    assert (out / "receipt.json").read_bytes() == saved


def test_proposed_configuration_is_not_a_write_request(tmp_path: Path) -> None:
    contract = inspection.workflow_contract(ROOT)
    payload = inspection.proposed_payloads(contract, None)
    assert payload["status"] == "PROPOSAL_ONLY_NOT_APPLIED"
    assert payload["app_binding_verified"] is False
    assert all(
        c["app_id"] is None for c in payload["protection"]["required_status_checks"]["checks"]
    )
    assert (
        payload["protection"]["required_pull_request_reviews"]["required_approving_review_count"]
        == 0
    )


def test_no_direct_apply_or_approval_skip_cli_option() -> None:
    result = subprocess.run(  # noqa: S603 — known tool help, no effect.
        [sys.executable, str(ROOT / "tools/check_github_publication_settings.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    for option in ["--apply", "--force", "--yes", "--skip", "--auto-approve", "--snapshot"]:
        assert option not in result.stdout


@pytest.mark.parametrize("mode", ["public-copy", "source-survey"])
def test_real_cli_collects_synthetic_api_with_get_only_and_redacted_receipt(
    tmp_path: Path, mode: str
) -> None:
    repo = synthetic_repo(tmp_path)
    source = inspection.source_binding(repo)
    contract = inspection.workflow_contract(repo)
    observed = good_observations(contract, source)
    prefix = "repos/" + REPOSITORY
    keys = {
        prefix: "repository",
        prefix + "/actions/permissions": "actions",
        prefix + "/actions/permissions/selected-actions": "allowed_actions",
        prefix + "/actions/permissions/workflow": "token",
        prefix + "/actions/permissions/fork-pr-contributor-approval": "fork",
        prefix + "/branches/main/protection": "protection",
        prefix + "/rulesets?per_page=100": "rulesets",
        prefix + "/private-vulnerability-reporting": "vulnerability_reporting",
        prefix + "/branches/main": "branch",
        prefix + "/commits/" + source["commit"]: "commit",
        "apps/github-actions": "app",
    }
    responses = {endpoint: observed[key].data for endpoint, key in keys.items()}
    for endpoint, key in [
        (
            prefix + f"/commits/{source['commit']}/check-runs?filter=latest&per_page=100&page=1",
            "check_runs",
        ),
        (
            prefix + f"/actions/runs?head_sha={source['commit']}&per_page=100&page=1",
            "workflow_runs",
        ),
        (prefix + "/actions/runs/401/attempts/2/jobs?filter=latest&per_page=100&page=1", "jobs"),
    ]:
        responses[endpoint] = dict(observed[key].data, total_count=len(observed[key].data[key]))
    responses[prefix]["secret"] = "SYNTHETIC_DO_NOT_PERSIST"
    fixture = tmp_path / "responses.json"
    fixture.write_text(json.dumps(responses))
    calls = tmp_path / "argv.jsonl"
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
        f"with Path({str(calls)!r}).open('a') as log: log.write(json.dumps(sys.argv[1:])+'\\n')\n"
        "if sys.argv[sys.argv.index('--method')+1] != 'GET': sys.exit(9)\n"
        f"data=json.loads(Path({str(fixture)!r}).read_text())[sys.argv[-1]]\n"
        "print('HTTP/2.0 200 OK\\n'+"
        "'X-OAuth-Scopes: SYNTHETIC_NOT_FOR_RECEIPT\\n\\n'+json.dumps(data))\n"
    )
    gh.chmod(0o700)
    out = tmp_path / "evidence"
    result = subprocess.run(  # noqa: S603 — actual collector CLI, explicitly synthetic executable.
        [
            sys.executable,
            str(repo / "tools/check_github_publication_settings.py"),
            "--repo",
            str(repo),
            "--repository",
            REPOSITORY,
            "--repository-id",
            str(REPOSITORY_ID),
            "--mode",
            mode,
            "--gh",
            str(gh),
            "--out",
            str(out),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == (0 if mode == "public-copy" else 2), result.stdout + result.stderr
    receipt = json.loads((out / "receipt.json").read_bytes())
    assert receipt["status"] == ("PASS" if mode == "public-copy" else "BLOCKED")
    assert receipt["source"]["commit"] == source["commit"]
    saved = json.dumps(receipt) + (out / "proposed-payloads.json").read_text() + result.stdout
    assert "SYNTHETIC_DO_NOT_PERSIST" not in saved and "SYNTHETIC_NOT_FOR_RECEIPT" not in saved
    assert calls.is_file()
    for line in calls.read_text().splitlines():
        argv = json.loads(line)
        assert argv[argv.index("--method") + 1] == "GET"
    assert inspection.source_binding(repo) == source


@pytest.mark.parametrize("changed", ["ephemeral_metadata", "array_order"])
def test_only_relevant_configuration_fields_are_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    repo = synthetic_repo(tmp_path)
    gh = tmp_path / "gh"
    gh.write_text("synthetic executable bytes")
    source = inspection.source_binding(repo)
    contract = inspection.workflow_contract(repo)
    observed = good_observations(contract, source)
    calls = 0

    def collect(reader: inspection.GithubReader, *args: Any) -> dict[str, inspection.Observation]:
        nonlocal calls
        calls += 1
        current = copy.deepcopy(observed)
        if changed == "ephemeral_metadata":
            current["repository"].data["temp_clone_token"] = "SYNTHETIC_UNSAVED_" + str(calls)
            current["repository"].data["stargazers_count"] = calls
        elif calls == 2:
            current["protection"].data["required_status_checks"]["checks"].reverse()
            current["jobs"].data["jobs"].reverse()
        return current

    monkeypatch.setattr(inspection, "collect", collect)
    out = tmp_path / "evidence"
    receipt = inspection.run_inspection(repo, REPOSITORY, REPOSITORY_ID, "public-copy", gh, out)
    assert receipt["status"] == "PASS"
    assert "SYNTHETIC_UNSAVED" not in (out / "receipt.json").read_text()
    assert not inspection.configuration_snapshot(observed).get("temp_clone_token")


def test_actual_policy_drift_is_detected_even_when_both_samples_pass() -> None:
    contract = inspection.workflow_contract(ROOT)
    before = good_observations(contract)
    after = copy.deepcopy(before)
    after["latest_run"].data["id"] = 402
    for job in after["jobs"].data["jobs"]:
        job["run_id"] = 402
    assert (
        inspection.evaluate(before, contract, REPOSITORY, REPOSITORY_ID, SOURCE, "public-copy")[
            "status"
        ]
        == "PASS"
    )
    assert (
        inspection.evaluate(after, contract, REPOSITORY, REPOSITORY_ID, SOURCE, "public-copy")[
            "status"
        ]
        == "PASS"
    )
    assert inspection.configuration_snapshot(before) != inspection.configuration_snapshot(after)


def test_public_update_requires_an_explicit_single_initial_root(tmp_path: Path) -> None:
    repo = synthetic_repo(tmp_path)
    initial = inspection.source_binding(repo)["commit"]
    subprocess.run(  # noqa: S603 — fixed executable; synthetic Git fixture arguments.
        [
            "/usr/bin/git",
            "-c",
            "user.name=Synthetic",
            "-c",
            "user.email=synthetic@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "synthetic public maintenance",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        timeout=10,
    )
    binding = inspection.source_binding(repo, initial)
    assert binding["public_root_commit"] == initial
    assert binding["history_commits"] == 2
    contract = inspection.workflow_contract(ROOT)
    observations = good_observations(contract, binding)
    assert (
        inspection.evaluate(
            observations, contract, REPOSITORY, REPOSITORY_ID, binding, "public-update"
        )["status"]
        == "PASS"
    )
    # The initial-copy contract still rejects multi-commit input.
    assert (
        inspection.evaluate(
            observations, contract, REPOSITORY, REPOSITORY_ID, binding, "public-copy"
        )["status"]
        == "BLOCKED"
    )
    with pytest.raises(inspection.InspectionFailed, match="PUBLIC_HISTORY_ROOT_MISMATCH"):
        inspection.source_binding(repo, binding["commit"])


@pytest.mark.parametrize("invalid", ["main", "HEAD", "a" * 39, "a" * 41, "A" * 40, "--all"])
def test_public_root_requires_a_full_literal_sha(tmp_path: Path, invalid: str) -> None:
    with pytest.raises(inspection.InspectionFailed, match="PUBLIC_ROOT_COMMIT_INVALID"):
        inspection.source_binding(synthetic_repo(tmp_path), invalid)


def test_public_update_rejects_a_second_root_from_unrelated_history(tmp_path: Path) -> None:
    repo = synthetic_repo(tmp_path)
    initial = inspection.source_binding(repo)["commit"]
    git = [
        "/usr/bin/git",
        "-c",
        "user.name=Synthetic",
        "-c",
        "user.email=synthetic@example.invalid",
    ]
    # A second root can share the exact same tree; bytes alone are insufficient.
    tree = inspection.source_binding(repo)["tree"]
    second = subprocess.run(  # noqa: S603 — fixed executable; synthetic Git fixture arguments.
        [*git, "commit-tree", tree],
        input="unrelated root\n",
        text=True,
        cwd=repo,
        check=True,
        capture_output=True,
        timeout=10,
    ).stdout.strip()
    subprocess.run(  # noqa: S603 — fixed executable; synthetic Git fixture arguments.
        [*git, "merge", "--allow-unrelated-histories", "--no-edit", second],
        cwd=repo,
        check=True,
        capture_output=True,
        timeout=10,
    )
    with pytest.raises(inspection.InspectionFailed, match="PUBLIC_HISTORY_ROOT_MISMATCH"):
        inspection.source_binding(repo, initial)


@pytest.mark.parametrize(
    ("mode", "root"),
    [("public-update", None), ("public-copy", "a" * 40), ("source-survey", "a" * 40)],
)
def test_root_mode_mismatch_is_rejected_before_any_github_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, root: str | None
) -> None:
    def no_network(*args: object, **kwargs: object) -> object:
        raise AssertionError("GitHub GET must not happen for invalid mode/root input")

    monkeypatch.setattr(inspection, "GithubReader", no_network)
    with pytest.raises(inspection.InspectionFailed, match="PUBLIC_ROOT_REQUIRED_ONLY_FOR_UPDATE"):
        inspection.run_inspection(
            ROOT,
            REPOSITORY,
            REPOSITORY_ID,
            mode,
            Path("/usr/bin/gh"),
            tmp_path / "out",
            public_root_commit=root,
        )
