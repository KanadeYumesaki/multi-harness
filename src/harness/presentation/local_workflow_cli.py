"""Explicit human operations for the offline workflow; no automatic approval."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from harness.application.task_plan_service import TaskPlanRequest
from harness.domain.errors import HarnessError
from harness.domain.hashing import hash_canonical
from harness.domain.input_read import CapabilityScope
from harness.infrastructure.local_workflow_runtime import SystemClock, local_workflow
from harness.infrastructure.runtime_facade import HarnessRuntimeService, TaskPlanSetup


def configure_workflow(parser: argparse.ArgumentParser) -> None:
    operations = parser.add_subparsers(dest="operation", required=True)
    for name in ("create", "inspect", "approve", "run", "evaluate", "release", "recover"):
        command = operations.add_parser(name)
        for field in ("database", "artifact-root", "workspace", "repo-root"):
            command.add_argument(
                "--" + field,
                required=field != "repo-root",
                type=Path,
                default=Path.cwd() if field == "repo-root" else None,
            )
        command.add_argument("--run-id", required=True)
        if name == "create":
            command.add_argument("--task-path", required=True)
            command.add_argument("--declaration-path", required=True)
            command.add_argument("--scope", action="append", required=True)
            command.add_argument("--context-dir", action="append", default=[])
            command.add_argument("--compress-context", action="store_true")
            command.add_argument("--ttl-seconds", type=int, default=3600)
        if name in ("approve", "release"):
            command.add_argument("--auth-session", required=True)
            command.add_argument(
                "--plan-hash" if name == "approve" else "--evaluation-hash", required=True
            )


def execute_workflow(args: argparse.Namespace) -> int:
    try:
        if not args.run_id or len(args.run_id) > 128 or any(ord(c) < 32 for c in args.run_id):
            raise ValueError("run-id must be a nonempty identifier of at most 128 characters")
        with local_workflow(
            database=args.database,
            artifact_root=args.artifact_root,
            workspace=args.workspace,
            repo_root=args.repo_root,
        ) as service:
            if args.operation == "create":
                if not 1 <= args.ttl_seconds <= 86400:
                    raise ValueError("ttl-seconds must be between 1 and 86400")
                now = SystemClock().now()
                expiry = (datetime.now(UTC) + timedelta(seconds=args.ttl_seconds)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                input_snapshot = service.capture_input_snapshot()
                capability_id = str(
                    hash_canonical(
                        {"scope": sorted(set(args.scope))},
                        artifact_type="local-input-capability",
                        schema_major=1,
                    )
                )
                setup = TaskPlanSetup(
                    args.repo_root,
                    args.workspace,
                    args.artifact_root,
                    capability_id,
                    CapabilityScope(tuple(args.scope)),
                    now,
                )
                request = TaskPlanRequest(
                    stream_id=args.run_id + ":input",
                    capability_id=setup.capability_id,
                    task_relative_path=args.task_path,
                    declaration_relative_path=args.declaration_path,
                    task_read_decision_id=args.run_id + ":task",
                    declaration_read_decision_id=args.run_id + ":declaration",
                    bundle_id=args.run_id + ":bundle",
                    receipt_id=args.run_id + ":selection",
                    run_id=args.run_id,
                    execution_plan_id=args.run_id + ":plan",
                    issued_at=now,
                    expires_at=expiry,
                    now=now,
                    workspace_snapshot_id=args.run_id + ":snapshot",
                    candidate_directories=tuple(args.context_dir),
                    compress_candidates=args.compress_context,
                )
                outcome = HarnessRuntimeService(args.database).plan_task(setup, request)
                if outcome.task_read.read_evidence is None:
                    raise ValueError("task evidence is missing")
                if outcome.declaration.invocation.model is None:
                    raise ValueError("explicit Mock model is required")
                result = service.create(
                    outcome.frozen_request,
                    expected_input_snapshot=input_snapshot,
                    input_artifact_hash=outcome.task_read.read_evidence.content_hash,
                    model=outcome.declaration.invocation.model,
                    executable_path=outcome.declaration.runtime_envelope.executable_path,
                    executable_hash=str(outcome.declaration.runtime_envelope.executable_sha256),
                    allowed_prefixes=tuple(args.scope),
                    token_profile_expires_at=outcome.declaration.token_profile.expires_at,
                )
            elif args.operation == "approve":
                result = service.approve(
                    args.run_id, plan_hash=args.plan_hash, auth_session=args.auth_session
                )
            elif args.operation == "release":
                result = service.release(
                    args.run_id,
                    evaluation_hash=args.evaluation_hash,
                    auth_session=args.auth_session,
                )
            elif args.operation == "run":
                result = service.run(args.run_id)
            elif args.operation == "evaluate":
                result = service.evaluate(args.run_id)
            elif args.operation == "recover":
                result = service.recover(args.run_id)
            else:
                result = service.inspect(args.run_id)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2 if result["state"] == "EVALUATION_FAILED" and args.operation != "inspect" else 0
    except (HarnessError, ValueError, OSError) as error:
        print(
            json.dumps(
                {
                    "error_code": error.code.value
                    if isinstance(error, HarnessError)
                    else "INPUT_OR_STORAGE_ERROR",
                    "detail": str(error),
                },
                ensure_ascii=False,
            )
        )
        return 2
