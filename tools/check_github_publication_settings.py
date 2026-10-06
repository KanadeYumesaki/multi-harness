#!/usr/bin/env python3
"""GitHub.comの設定をGETで実測する。設定変更・公開・Actions起動はしない。

source-surveyは非公開元Repositoryの観測で、成功終了しない。public-copyだけが
新しい1 Commit配布コピー/公開先ID/Branch/完全CIを照合する。Secret Alert本文や
認証/外部stderrを保存しない。保存済み出力の再利用やoffline合格入口は無い。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


class InspectionFailed(Exception):
    """外部値を含めない原因Code。"""


def _digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise InspectionFailed("INPUT_NOT_REGULAR")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(  # noqa: S603 — 固定Git読取り、Shell無し。
            ["/usr/bin/git", *args],
            cwd=repo,
            capture_output=True,
            timeout=30,
            check=False,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": "/nonexistent",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            },
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InspectionFailed("GIT_READ_FAILED") from exc
    if result.returncode:
        raise InspectionFailed("GIT_READ_FAILED")
    return result.stdout.decode("utf-8").strip()


def source_binding(repo: Path) -> dict[str, Any]:
    if _git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise InspectionFailed("SOURCE_NOT_CLEAN")
    names = [
        ".github/workflows/ci.yml",
        "tools/ci_scope.py",
        "ci/github-publication-policy.json",
        "tools/check_github_publication_settings.py",
    ]
    return {
        "commit": _git(repo, "rev-parse", "HEAD"),
        "tree": _git(repo, "rev-parse", "HEAD^{tree}"),
        "history_commits": int(_git(repo, "rev-list", "--count", "HEAD")),
        "files": [{"path": n, "sha256": _digest(repo / n)} for n in names],
    }


def workflow_contract(repo: Path) -> dict[str, Any]:
    policy = json.loads((repo / "ci/github-publication-policy.json").read_bytes())
    if (
        policy.get("contract") != "github-publication-policy/1"
        or policy.get("workflow_path") != ".github/workflows/ci.yml"
        or policy.get("scope_path") != "tools/ci_scope.py"
        or policy.get("default_branch") != "main"
        or policy.get("api_version") != "2022-11-28"
        or policy.get("actions_app_slug") != "github-actions"
        or policy.get("required_approving_reviews") != 0
        or type(policy.get("required_approving_reviews")) is not int
    ):
        raise InspectionFailed("POLICY_CONTRACT_UNKNOWN")
    document = yaml.safe_load((repo / policy["workflow_path"]).read_text())
    jobs = document["jobs"]
    events = document.get("on", document.get(True))
    if (
        document.get("permissions") != {"contents": "read"}
        or "pull_request_target" in events
        or events.get("push", {}).get("branches") != [policy["default_branch"]]
    ):
        raise InspectionFailed("WORKFLOW_SECURITY_OR_MAIN_TRIGGER_MISMATCH")
    # 他のcheckoutのPythonを実行しない。正本の単純な定数だけをASTから読む。
    versions: dict[str, list[str]] = {}
    for node in ast.parse((repo / policy["scope_path"]).read_text()).body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            key = node.targets[0].id
            if key in {"LIGHT_PYTHONS", "FULL_PYTHONS"}:
                value = ast.literal_eval(node.value)
                if (
                    key in versions
                    or not isinstance(value, list)
                    or not value
                    or not all(
                        isinstance(v, str) and re.fullmatch(r"[0-9]+\.[0-9]+", v) for v in value
                    )
                    or len(value) != len(set(value))
                ):
                    raise InspectionFailed("SCOPE_PYTHON_MATRIX_INVALID")
                versions[key] = value
    if set(versions) != {"LIGHT_PYTHONS", "FULL_PYTHONS"} or not set(
        versions["LIGHT_PYTHONS"]
    ).issubset(versions["FULL_PYTHONS"]):
        raise InspectionFailed("SCOPE_PYTHON_MATRIX_INVALID")
    required_jobs = policy.get("required_jobs")
    if (
        not isinstance(required_jobs, list)
        or len(required_jobs) != len(set(required_jobs))
        or set(required_jobs) != {key for key, job in jobs.items() if "if" not in job}
        or policy.get("matrix_job") not in required_jobs
    ):
        raise InspectionFailed("REQUIRED_JOB_SET_MISMATCH")
    contexts: dict[str, list[str]] = {"required": [], "full": []}
    all_names: set[str] = set()
    actions: set[str] = set()
    for key, job in jobs.items():
        name = job.get("name", key)
        if name in all_names:
            raise InspectionFailed("WORKFLOW_CHECK_NAME_AMBIGUOUS")
        all_names.add(name)
        if job.get("permissions", document["permissions"]) != {"contents": "read"}:
            raise InspectionFailed("WORKFLOW_JOB_TOKEN_NOT_READ_ONLY")
        for step in job["steps"]:
            if "${{" in step.get("run", ""):
                raise InspectionFailed("WORKFLOW_EVENT_EXPRESSION_IN_SHELL")
            if "uses" in step:
                action = step["uses"]
                if not re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", action):
                    raise InspectionFailed("ACTION_NOT_OFFICIAL_FULL_SHA")
                if (
                    action.startswith("actions/checkout@")
                    and step.get("with", {}).get("persist-credentials") is not False
                ):
                    raise InspectionFailed("CHECKOUT_PERSISTS_CREDENTIALS")
                actions.add(action)
    for key in policy["required_jobs"]:
        job = jobs[key]
        if "if" in job:
            raise InspectionFailed("REQUIRED_JOB_CAN_BE_SKIPPED")
        name = job["name"]
        if key == policy["matrix_job"]:
            if (
                job["strategy"]["matrix"]["python"] != "${{ fromJSON(needs.plan.outputs.pythons) }}"
                or name.count("${{ matrix.python }}") != 1
            ):
                raise InspectionFailed("QUALITY_MATRIX_NOT_BOUND_TO_SCOPE")
            for kind, python_versions in [
                ("required", versions["LIGHT_PYTHONS"]),
                ("full", versions["FULL_PYTHONS"]),
            ]:
                contexts[kind].extend(
                    name.replace("${{ matrix.python }}", v) for v in python_versions
                )
        else:
            if "${{" in name:
                raise InspectionFailed("UNKNOWN_CHECK_NAME_EXPRESSION")
            for names in contexts.values():
                names.append(name)
    if any(len(names) != len(set(names)) for names in contexts.values()):
        raise InspectionFailed("CHECK_NAME_DUPLICATE")
    return {
        "policy": policy,
        "required_checks": sorted(contexts["required"]),
        "full_checks": sorted(contexts["full"]),
        "actions": sorted(actions),
    }


@dataclass(frozen=True)
class Observation:
    status: int | None
    data: Any = None
    reason: str = "OBSERVED"


class GithubReader:
    """GET専用。返答はメモリ内だけで解析し、必要な判定結果だけを保存する。"""

    def __init__(self, executable: Path, api_version: str) -> None:
        self.executable = executable
        self.api_version = api_version
        self.requests: list[dict[str, Any]] = []
        self.transport_failures = 0

    def get(self, endpoint: str) -> Observation:
        # GETだけを最大2回。連続した輸送障害3回でCircuitを開き、残りは欠測。
        if self.transport_failures >= 3:
            return Observation(None, reason="API_CIRCUIT_OPEN")
        for _ in range(2):
            result = self._get_once(endpoint)
            if (
                result.reason != "API_START_OR_TIMEOUT"
                and result.status != 429
                and not (result.status is not None and result.status >= 500)
            ):
                self.transport_failures = 0
                return result
            self.transport_failures += 1
            if self.transport_failures >= 3:
                break
        return result

    def _get_once(self, endpoint: str) -> Observation:
        if not re.fullmatch(r"[A-Za-z0-9_./?=&%-]+", endpoint) or endpoint.startswith("/"):
            raise InspectionFailed("API_ENDPOINT_INVALID")
        argv = [
            str(self.executable),
            "api",
            "--include",
            "--method",
            "GET",
            "--hostname",
            "github.com",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: " + self.api_version,
            "-H",
            "Cache-Control: no-cache",
            endpoint,
        ]
        env = {
            k: v
            for k, v in os.environ.items()
            if k in {"PATH", "HOME", "GH_CONFIG_DIR", "GH_TOKEN", "GITHUB_TOKEN", "LANG", "LC_ALL"}
        }
        env.update({"GH_PROMPT_DISABLED": "1", "GH_PAGER": "cat", "NO_COLOR": "1"})
        stamp = datetime.now(UTC).isoformat()
        try:
            result = subprocess.run(  # noqa: S603 — 検証済みPath、固定GET、stdin無し。
                argv,
                capture_output=True,
                input=b"",
                timeout=30,
                env=env,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            self.requests.append(
                {
                    "endpoint": endpoint,
                    "method": "GET",
                    "observed_at": stamp,
                    "http_status": None,
                    "reason": "API_START_OR_TIMEOUT",
                }
            )
            return Observation(None, reason="API_START_OR_TIMEOUT")
        # stderrをLogへ返さない。HTTP headersの認証Scope/Client IDも保存しない。
        try:
            if len(result.stdout) > 8 * 1024 * 1024:
                raise ValueError("response too large")
            text = result.stdout.decode("utf-8").replace("\r\n", "\n")
            headers, body = text.split("\n\n", 1)
            match = re.match(r"HTTP/[0-9.]+ ([0-9]{3})\b", headers)
            if match is None:
                raise ValueError("HTTP status missing")
            status = int(match.group(1))
            data = json.loads(body)
            reason = "OBSERVED"
            if status != 200:
                reason = "HTTP_ERROR_UNVERIFIED"
                if (
                    status == 403
                    and isinstance(data, dict)
                    and data.get("message")
                    == (
                        "Upgrade to GitHub Pro or make this repository "
                        "public to enable this feature."
                    )
                ):
                    reason = "UNAVAILABLE_PLAN"
                if (
                    status == 422
                    and isinstance(data, dict)
                    and data.get("errors")
                    == ("Fork PR approval is not allowed for private repositories.")
                ):
                    reason = "UNAVAILABLE_PRIVATE_REPOSITORY"
                data = None
            elif result.returncode:
                reason, data = "API_EXIT_WITH_HTTP_SUCCESS", None
            observation = Observation(status, data, reason)
        except (ValueError, UnicodeError):
            observation = Observation(None, reason="API_RESPONSE_INVALID")
        self.requests.append(
            {
                "endpoint": endpoint,
                "method": "GET",
                "observed_at": stamp,
                "rc": result.returncode,
                "http_status": observation.status,
                "reason": observation.reason,
            }
        )
        return observation

    def pages(self, endpoint: str, key: str) -> Observation:
        rows: list[Any] = []
        total: int | None = None
        for page in range(1, 21):
            response = self.get(endpoint + f"&per_page=100&page={page}")
            if response.reason != "OBSERVED" or not isinstance(response.data, dict):
                return response
            value = response.data.get(key)
            count = response.data.get("total_count")
            if not isinstance(value, list) or type(count) is not int or count < 0:
                return Observation(200, reason="API_PAGE_SHAPE_INVALID")
            if total is not None and count != total:
                return Observation(200, reason="API_PAGE_TOTAL_CHANGED")
            total = count
            rows.extend(value)
            if len(rows) == total:
                ids = [r.get("id") for r in rows if isinstance(r, dict)]
                if (
                    len(ids) != len(rows)
                    or any(type(i) is not int for i in ids)
                    or len(set(ids)) != len(ids)
                ):
                    return Observation(200, reason="API_PAGE_DUPLICATE_OR_ID_INVALID")
                return Observation(200, {key: rows})
            if len(rows) > total or len(value) != 100:
                return Observation(200, reason="API_PAGE_INCOMPLETE")
        return Observation(200, reason="API_PAGE_LIMIT_EXCEEDED")


def collect(
    reader: GithubReader, repository: str, branch: str, commit: str
) -> dict[str, Observation]:
    prefix = "repos/" + repository
    endpoints = {
        "repository": prefix,
        "actions": prefix + "/actions/permissions",
        "allowed_actions": prefix + "/actions/permissions/selected-actions",
        "token": prefix + "/actions/permissions/workflow",
        "fork": prefix + "/actions/permissions/fork-pr-contributor-approval",
        "protection": prefix + f"/branches/{branch}/protection",
        "rulesets": prefix + "/rulesets?per_page=100",
        "vulnerability_reporting": prefix + "/private-vulnerability-reporting",
        "branch": prefix + f"/branches/{branch}",
        "commit": prefix + "/commits/" + commit,
        "app": "apps/github-actions",
    }
    observed = {key: reader.get(path) for key, path in endpoints.items()}
    observed["check_runs"] = reader.pages(
        prefix + f"/commits/{commit}/check-runs?filter=latest", "check_runs"
    )
    observed["workflow_runs"] = reader.pages(
        prefix + f"/actions/runs?head_sha={commit}", "workflow_runs"
    )
    candidates = observed["workflow_runs"].data
    if isinstance(candidates, dict):
        runs = [
            r
            for r in candidates["workflow_runs"]
            if isinstance(r, dict)
            and r.get("path") == ".github/workflows/ci.yml"
            and r.get("event") == "push"
            and r.get("head_branch") == branch
            and r.get("head_sha") == commit
            and type(r.get("run_number")) is int
            and type(r.get("run_attempt")) is int
        ]
        if runs:
            latest = max(runs, key=lambda r: (r["run_number"], r["id"], r["run_attempt"]))
            observed["latest_run"] = Observation(200, latest)
            observed["jobs"] = reader.pages(
                prefix
                + f"/actions/runs/{latest['id']}/attempts/{latest['run_attempt']}"
                + "/jobs?filter=latest",
                "jobs",
            )
    return observed


def configuration_snapshot(observed: dict[str, Observation]) -> dict[str, Any]:
    """設定・CIで消費するFieldだけを比較。付随の一時値/Actor/URLを比較しない。

    Repository APIは毎回変わる付随値を含む。本文全体の一致は設定の一致ではない。
    この射影はメモリ内だけで使い、ReceiptへAPI本文を保存しない。
    """
    run = {
        key: True
        for key in [
            "id",
            "run_number",
            "run_attempt",
            "status",
            "conclusion",
            "head_sha",
            "head_branch",
            "path",
            "event",
        ]
    }
    schemas: dict[str, Any] = {
        "repository": {
            "full_name": True,
            "id": True,
            "visibility": True,
            "private": True,
            "archived": True,
            "default_branch": True,
            "security_and_analysis": {
                "secret_scanning": {"status": True},
                "secret_scanning_push_protection": {"status": True},
            },
        },
        "actions": {"enabled": True, "allowed_actions": True, "sha_pinning_required": True},
        "allowed_actions": {
            "github_owned_allowed": True,
            "verified_allowed": True,
            "patterns_allowed": [True],
        },
        "token": {"default_workflow_permissions": True, "can_approve_pull_request_reviews": True},
        "fork": {"approval_policy": True},
        "protection": {
            "required_status_checks": {
                "strict": True,
                "contexts": [True],
                "checks": [{"context": True, "app_id": True}],
            },
            "enforce_admins": {"enabled": True},
            "allow_force_pushes": {"enabled": True},
            "allow_deletions": {"enabled": True},
            "required_pull_request_reviews": {
                "required_approving_review_count": True,
                "dismiss_stale_reviews": True,
                "bypass_pull_request_allowances": {
                    "users": [{"id": True}],
                    "teams": [{"id": True}],
                    "apps": [{"id": True}],
                },
            },
        },
        "rulesets": [{"id": True, "enforcement": True}],
        "vulnerability_reporting": {"enabled": True},
        "app": {"id": True, "slug": True},
        "branch": {"commit": {"sha": True}},
        "commit": {"sha": True, "commit": {"tree": {"sha": True}}},
        "latest_run": run,
        "workflow_runs": {"workflow_runs": [run]},
        "check_runs": {
            "check_runs": [
                {
                    "id": True,
                    "name": True,
                    "head_sha": True,
                    "status": True,
                    "conclusion": True,
                    "app": {"id": True},
                }
            ]
        },
        "jobs": {
            "jobs": [
                {
                    key: True
                    for key in [
                        "id",
                        "name",
                        "run_id",
                        "run_attempt",
                        "status",
                        "conclusion",
                        "check_run_url",
                    ]
                }
            ]
        },
    }

    def project(value: Any, schema: Any) -> Any:
        if schema is True:
            return value
        if isinstance(schema, dict) and isinstance(value, dict):
            return {
                key: project(value[key], nested) for key, nested in schema.items() if key in value
            }
        if isinstance(schema, list) and isinstance(value, list):
            return sorted(
                (project(item, schema[0]) for item in value),
                key=lambda item: json.dumps(item, sort_keys=True),
            )
        return {"invalid_type": type(value).__name__}

    return {
        key: {
            "http_status": observed[key].status,
            "reason": observed[key].reason,
            "control_fields": project(observed[key].data, schema),
        }
        for key, schema in schemas.items()
        if key in observed
    }


def evaluate(
    observed: dict[str, Observation],
    contract: dict[str, Any],
    repository: str,
    repository_id: int,
    source: dict[str, Any],
    mode: str,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []

    def data(key: str) -> Any:
        obs = observed.get(key)
        return obs.data if obs and obs.status == 200 and obs.reason == "OBSERVED" else None

    def row(name: str, key: str, condition: bool, code: str) -> None:
        obs = observed.get(key)
        verified = (
            obs is not None
            and obs.status == 200
            and obs.reason == "OBSERVED"
            and obs.data is not None
        )
        rows.append(
            {
                "id": name,
                "status": ("PASS" if condition else "FAIL") if verified else "UNVERIFIED",
                "http_status": obs.status if obs else None,
                "reason": "SATISFIED"
                if condition and verified
                else (code if verified else obs.reason if obs else "OBSERVATION_MISSING"),
            }
        )

    policy = contract["policy"]
    meta = data("repository")
    meta = meta if isinstance(meta, dict) else {}
    identity = (
        meta.get("full_name", "").casefold() == repository.casefold()
        and type(meta.get("id")) is int
        and meta.get("id") == repository_id
    )
    row("repository_identity", "repository", identity, "REPOSITORY_IDENTITY_MISMATCH")
    row(
        "public_destination",
        "repository",
        meta.get("visibility") == "public"
        and meta.get("private") is False
        and meta.get("archived") is False,
        "DESTINATION_NOT_PUBLIC_ACTIVE",
    )
    row(
        "default_branch",
        "repository",
        meta.get("default_branch") == policy["default_branch"],
        "DEFAULT_BRANCH_MISMATCH",
    )
    token = data("token") or {}
    row(
        "workflow_token",
        "token",
        token.get("default_workflow_permissions") == "read"
        and token.get("can_approve_pull_request_reviews") is False,
        "WORKFLOW_TOKEN_TOO_BROAD_OR_MISSING",
    )
    actions = data("actions") or {}
    row(
        "actions_policy",
        "actions",
        actions.get("enabled") is True
        and actions.get("allowed_actions") == "selected"
        and actions.get("sha_pinning_required") is True,
        "ACTIONS_ALLOW_ALL_OR_UNPINNED_OR_DISABLED",
    )
    allowed = data("allowed_actions") or {}
    patterns = allowed.get("patterns_allowed")
    row(
        "allowed_action_refs",
        "allowed_actions",
        allowed.get("github_owned_allowed") is False
        and allowed.get("verified_allowed") is False
        and isinstance(patterns, list)
        and len(patterns) == len(set(patterns))
        and set(patterns) == set(contract["actions"]),
        "ACTION_WHITELIST_MISMATCH",
    )
    fork = data("fork") or {}
    row(
        "external_fork_approval",
        "fork",
        fork.get("approval_policy") == "all_external_contributors",
        "FORK_APPROVAL_INSUFFICIENT",
    )
    security = meta.get("security_and_analysis") or {}
    row(
        "secret_scanning",
        "repository",
        security.get("secret_scanning", {}).get("status") == "enabled",
        "SECRET_SCANNING_NOT_OBSERVED_ENABLED",
    )
    row(
        "push_protection",
        "repository",
        security.get("secret_scanning_push_protection", {}).get("status") == "enabled",
        "PUSH_PROTECTION_NOT_OBSERVED_ENABLED",
    )
    reporting = data("vulnerability_reporting") or {}
    row(
        "private_vulnerability_reporting",
        "vulnerability_reporting",
        reporting.get("enabled") is True,
        "PRIVATE_REPORTING_NOT_ENABLED",
    )
    app = data("app") or {}
    app_id = app.get("id")
    row(
        "github_actions_app",
        "app",
        type(app_id) is int and app_id > 0 and app.get("slug") == policy["actions_app_slug"],
        "ACTIONS_APP_ID_UNVERIFIED",
    )
    protection = data("protection") or {}
    status_checks = protection.get("required_status_checks") or {}
    checks = status_checks.get("checks")
    required = contract["required_checks"]
    valid_checks = (
        isinstance(checks, list)
        and len(checks) == len(required)
        and all(
            isinstance(c, dict) and c.get("app_id") == app_id and type(c.get("app_id")) is int
            for c in checks
        )
        and sorted(c.get("context", "") for c in checks) == required
    )
    row(
        "required_status_checks",
        "protection",
        status_checks.get("strict") is True
        and valid_checks
        and isinstance(status_checks.get("contexts"), list)
        and sorted(status_checks["contexts"]) == required,
        "REQUIRED_CHECKS_OR_APP_BINDING_MISMATCH",
    )
    reviews = protection.get("required_pull_request_reviews") or {}
    protected = (
        protection.get("enforce_admins", {}).get("enabled") is True
        and protection.get("allow_force_pushes", {}).get("enabled") is False
        and protection.get("allow_deletions", {}).get("enabled") is False
        and reviews.get("required_approving_review_count") == policy["required_approving_reviews"]
        and type(reviews.get("required_approving_review_count")) is int
        and reviews.get("dismiss_stale_reviews") is True
        and not any(
            reviews.get("bypass_pull_request_allowances", {}).get(k, [])
            for k in ["users", "teams", "apps"]
        )
    )
    row("branch_protection", "protection", protected, "BRANCH_PROTECTION_WEAK_OR_UNOBSERVED")
    rulesets = data("rulesets")
    row(
        "additional_rulesets",
        "rulesets",
        isinstance(rulesets, list) and not rulesets,
        "ADDITIONAL_RULESETS_REQUIRE_REVIEW",
    )
    branch = data("branch") or {}
    commit = data("commit") or {}
    copy_bound = (
        mode == "public-copy"
        and source["history_commits"] == 1
        and branch.get("commit", {}).get("sha") == source["commit"]
        and commit.get("sha") == source["commit"]
        and commit.get("commit", {}).get("tree", {}).get("sha") == source["tree"]
    )
    row("distribution_commit_tree", "commit", copy_bound, "DISTRIBUTION_NOT_BOUND_OR_SOURCE_SURVEY")
    run = data("latest_run") or {}
    jobs = data("jobs") or {}
    check_runs = data("check_runs") or {}
    relevant_jobs = [
        j
        for j in jobs.get("jobs", [])
        if isinstance(j, dict) and j.get("name") in contract["full_checks"]
    ]
    by_id = {c.get("id"): c for c in check_runs.get("check_runs", []) if isinstance(c, dict)}
    ci_ok = (
        run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and run.get("head_sha") == source["commit"]
        and run.get("head_branch") == policy["default_branch"]
        and run.get("path") == policy["workflow_path"]
        and run.get("event") == "push"
        and type(run.get("run_attempt")) is int
        and run["run_attempt"] > 0
        and type(run.get("id")) is int
        and run["id"] > 0
        and len(relevant_jobs) == len(contract["full_checks"])
        and sorted(j.get("name") for j in relevant_jobs) == contract["full_checks"]
    )
    for job in relevant_jobs:
        url = job.get("check_run_url", "")
        match = re.fullmatch(
            r"https://api\.github\.com/repos/" + re.escape(repository) + r"/check-runs/([0-9]+)",
            url,
        )
        check = by_id.get(int(match.group(1))) if match else None
        ci_ok = ci_ok and (
            job.get("run_attempt") == run.get("run_attempt")
            and type(job.get("run_attempt")) is int
            and job.get("run_id") == run.get("id")
            and job.get("status") == "completed"
            and job.get("conclusion") == "success"
            and isinstance(check, dict)
            and check.get("name") == job.get("name")
            and check.get("status") == "completed"
            and check.get("conclusion") == "success"
            and check.get("head_sha") == source["commit"]
            and check.get("app", {}).get("id") == app_id
        )
    row(
        "remote_full_ci",
        "latest_run",
        bool(ci_ok),
        "FULL_CI_MISSING_STALE_WRONG_APP_OR_NOT_SUCCESS",
    )
    return {
        "status": "PASS"
        if mode == "public-copy" and all(r["status"] == "PASS" for r in rows)
        else "BLOCKED",
        "rows": rows,
        "facts": {
            "repository_identity_matches": identity,
            "observed_visibility": meta.get("visibility")
            if meta.get("visibility") in {"private", "public", "internal"}
            else "UNKNOWN",
            "workflow_token": token.get("default_workflow_permissions")
            if token.get("default_workflow_permissions") in {"read", "write"}
            else "UNKNOWN",
            "allowed_actions": actions.get("allowed_actions")
            if actions.get("allowed_actions") in {"all", "local_only", "selected"}
            else "UNKNOWN",
            "sha_pinning_required": actions.get("sha_pinning_required")
            if type(actions.get("sha_pinning_required")) is bool
            else None,
        },
    }


def proposed_payloads(contract: dict[str, Any], app_id: int | None) -> dict[str, Any]:
    checks = [{"context": n, "app_id": app_id} for n in contract["required_checks"]]
    return {
        "status": "PROPOSAL_ONLY_NOT_APPLIED",
        "app_binding_verified": type(app_id) is int and app_id > 0 if app_id is not None else False,
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
        "security": {
            "security_and_analysis": {
                "secret_scanning": {"status": "enabled"},
                "secret_scanning_push_protection": {"status": "enabled"},
            }
        },
        "private_reporting": {
            "method": "PUT",
            "body": None,
            "path_suffix": "/private-vulnerability-reporting",
        },
        "protection": {
            "required_status_checks": {"strict": True, "checks": checks},
            "enforce_admins": True,
            "required_pull_request_reviews": {
                "required_approving_review_count": 0,
                "dismiss_stale_reviews": True,
                "require_code_owner_reviews": False,
            },
            "restrictions": None,
            "allow_force_pushes": False,
            "allow_deletions": False,
        },
    }


def run_inspection(
    repo: Path, repository: str, repository_id: int, mode: str, gh: Path, out: Path
) -> dict[str, Any]:
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", repository)
        or repository_id <= 0
    ):
        raise InspectionFailed("REPOSITORY_IDENTITY_INPUT_INVALID")
    repo = repo.resolve(strict=True)
    out = out.absolute()
    if out.exists() or out.is_symlink() or out.resolve().is_relative_to(repo):
        raise InspectionFailed("OUTPUT_MUST_BE_NEW_AND_OUTSIDE_REPOSITORY")
    if any(str(p).startswith("/mnt/") for p in [repo, out.resolve()]):
        raise InspectionFailed("WINDOWS_FILESYSTEM_OUTPUT_DENIED")
    before = source_binding(repo)
    contract = workflow_contract(repo)
    if _digest(repo / "tools/check_github_publication_settings.py") != _digest(Path(__file__)):
        raise InspectionFailed("INSPECTOR_SOURCE_MISMATCH")
    executable = gh.resolve(strict=True)
    executable_hash = _digest(executable)
    out.mkdir(parents=True, mode=0o700, exist_ok=False)
    reader = GithubReader(executable, contract["policy"]["api_version"])
    receipt: dict[str, Any] = {
        "contract": "github-publication-settings/1",
        "status": "BLOCKED",
        "mode": mode,
        "repository": repository,
        "repository_id": repository_id,
        "source": before,
        "gh_sha256": executable_hash,
        "required_checks": contract["required_checks"],
        "full_checks": contract["full_checks"],
        "actions": contract["actions"],
        "scope": "GitHub settings and bound full CI only; not publication approval or Runtime GO",
    }
    try:
        observations = collect(
            reader, repository, contract["policy"]["default_branch"], before["commit"]
        )
        result = evaluate(observations, contract, repository, repository_id, before, mode)
        # 二度目の全GET。途中の設定/Run変化を、新旧の部分的な合成で通さない。
        again = collect(reader, repository, contract["policy"]["default_branch"], before["commit"])
        second = evaluate(again, contract, repository, repository_id, before, mode)
        receipt.update(result)
        if (
            configuration_snapshot(observations) != configuration_snapshot(again)
            or result != second
        ):
            raise InspectionFailed("REMOTE_OBSERVATIONS_CHANGED")
        if source_binding(repo) != before or _digest(executable) != executable_hash:
            raise InspectionFailed("LOCAL_SOURCE_OR_TOOL_CHANGED")
        app = observations.get("app")
        app_id = (
            app.data.get("id")
            if app and isinstance(app.data, dict) and app.data.get("slug") == "github-actions"
            else None
        )
        (out / "proposed-payloads.json").write_text(
            json.dumps(proposed_payloads(contract, app_id), indent=2) + "\n"
        )
    except (
        InspectionFailed,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        SyntaxError,
    ) as exc:
        receipt["status"] = "BLOCKED"
        receipt["error"] = (
            str(exc) if isinstance(exc, InspectionFailed) else "INVALID_INPUT_OR_API_SHAPE"
        )
    finally:
        receipt["requests"] = reader.requests
        receipt["proposal_sha256"] = (
            _digest(out / "proposed-payloads.json")
            if (out / "proposed-payloads.json").exists()
            else None
        )
        (out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--repository", required=True, help="actual owner/name")
    parser.add_argument(
        "--repository-id", type=int, required=True, help="actual stable GitHub numeric ID"
    )
    parser.add_argument("--mode", choices=["source-survey", "public-copy"], required=True)
    parser.add_argument("--gh", type=Path, default=Path(shutil.which("gh") or "/usr/bin/gh"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        receipt = run_inspection(
            args.repo, args.repository, args.repository_id, args.mode, args.gh, args.out
        )
    except (
        InspectionFailed,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        SyntaxError,
    ) as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "reason": str(exc)
                    if isinstance(exc, InspectionFailed)
                    else "LOCAL_INPUT_INVALID",
                }
            )
        )
        return 2
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "mode": receipt["mode"],
                "rows": receipt.get("rows", []),
                "error": receipt.get("error"),
            }
        )
    )
    return 0 if receipt["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
