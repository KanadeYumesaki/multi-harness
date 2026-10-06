#!/usr/bin/env python3
"""`backup_restore` 領域の判定述語。**観測値と生ログのBytesから合否を導く。**

## 観測器と判定器を分ける

`tools/backup_restore_observation.py` は「何が起きたか」を記録するだけで、
PASS／FAIL の Bool を書かない。合否はここで、記録と生ログから導き直す。
記録された `stdout_json` も信用せず、保存した stdout Bytes を読み直して照合する。

## 欠測とFAILを分ける

| 結果 | 意味 |
|---|---|
| `PASS` | 必要な観測が揃い、要件を満たした |
| `FAIL` | 観測した結果が要件に反した（受け付けた・作用が増えた・理由が違う・記録と生ログが矛盾） |
| `UNVERIFIED` | 観測が無い・型が違う・生ログが読めない |

領域は全Checkが `PASS` のときだけ `PASS`。1件でも `FAIL` なら `FAIL`、
それ以外の欠測は `UNVERIFIED`。**欠測をPASSへ倒す経路を作らない。**

## Verifier の受理とは別物

`verify_runtime_go.py` は `backup_restore` 固有の summary を検査しない。
ここの `PASS` は観測の意味的な合格であり、Verifier の構造受理とは別欄で報告する。

## 再開の実行を合格に読み替えない

`deploy drain --check` の `ACCEPTED` は Deploy 可否の判定であって、受付の再開ではない。
再開操作の入口が存在しない現状では「実行していない」ことだけを確認し、
入口が現れたのに実行していなければ `UNVERIFIED` にする。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

CONTRACT = "cc-ops-evidence-01/backup-restore-observations/1"

PASS = "PASS"  # noqa: S105 - 判定Statusの名前であり、Secretではない
FAIL = "FAIL"
UNVERIFIED = "UNVERIFIED"

REFUSAL_EXCEPTION = "harness.application.intake_gate.IntakeRefused"
REFUSAL_CODE = "DEPLOY_DRAIN_REQUIRED"

#: `operations_repository` の表と同じ区分。終端でない state を進行中とする。
TERMINAL_OPERATION_JOURNAL_STATES = ("RECEIPT_DURABLE",)
TERMINAL_CLI_INVOCATION_STATES = ("RESPONSE_CAPTURED",)
CLI_INVOCATION_LABEL = "cli_invocation_journal:"

DRAIN_ENVIRONMENTS = (
    "QUIESCENT",
    "ACTIVE_OPERATION_JOURNAL",
    "ACTIVE_CLI_INVOCATION",
    "PENDING_APPROVAL",
    "ADMISSION_RESERVED",
)
RESTORE_START_CONFLICTS = ("PENDING_APPROVAL", "ADMISSION_RESERVED")
FAILURE_KINDS = ("COPY_FAILURE", "VERIFICATION_REJECTED")
WINDOW_WORKBENCH_ENTRIES = (
    "WorkbenchGateway.create_session",
    "WorkbenchGateway.approve_send",
    "WorkbenchGateway.approve_apply",
    "WorkbenchGateway.apply",
)

#: 入口が拒否したときに増えてはならない観測値。`operation_control` は別に比べる。
COMPARED_FIELDS = (
    "operation_admission_kinds",
    "operation_journal_states",
    "cli_invocation_journal_states",
    "approval_grant_statuses",
    "workbench_session_states",
    "event_ledger_rows",
    "event_ledger_sha256",
    "artifact_manifest_rows",
    "artifact_manifest_sha256",
    "cas_object_files",
    "cas_tree_sha256",
    "effect_port_calls",
    "runner_calls",
    "fake_cli_launches",
    "worktree_target_sha256",
)


class Missing(Exception):
    """観測値が無い・型が違う・生ログが読めない。`UNVERIFIED` へ写す。"""


class Violation(Exception):
    """観測値が要件に反する。`FAIL` へ写す。"""


# ---------------------------------------------------------------------------
# 生ログ
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-standard JSON constant {value}")


def parse_json_line(data: bytes) -> tuple[dict[str, Any] | None, str | None]:
    """stdout が「改行で終わる1行の JSON Object」であることを要求する。

    stderr はここへ渡さない。stdout が空でも stderr を代わりに読まない。
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "STDOUT_NOT_UTF8"
    if not text.endswith("\n") or text.count("\n") != 1:
        return None, "STDOUT_NOT_SINGLE_LINE"
    try:
        value = json.loads(text, parse_constant=_reject_constant)
    except ValueError:
        return None, "STDOUT_NOT_JSON"
    if not isinstance(value, dict):
        return None, "STDOUT_NOT_OBJECT"
    return value, None


# ---------------------------------------------------------------------------
# 取り出しと比較
# ---------------------------------------------------------------------------


def _matches(value: Any, kind: type | tuple[type, ...]) -> bool:
    kinds = kind if isinstance(kind, tuple) else (kind,)
    for expected in kinds:
        if expected is type(None) and value is None:
            return True
        if expected is bool and type(value) is bool:
            return True
        if expected is int and type(value) is int:
            return True
        if expected not in (bool, int, type(None)) and isinstance(value, expected):
            return True
    return False


def need(obj: Any, *path: str | int, kind: type | tuple[type, ...] | None = None) -> Any:
    current = obj
    for key in path:
        if isinstance(key, int):
            if not isinstance(current, list) or not -len(current) <= key < len(current):
                raise Missing(_label(path))
        elif not isinstance(current, dict) or key not in current:
            raise Missing(_label(path))
        current = current[key]
    if kind is not None and not _matches(current, kind):
        raise Missing(f"{_label(path)}: unexpected type {type(current).__name__}")
    return current


def _label(path: tuple[str | int, ...]) -> str:
    return ".".join(str(item) for item in path) or "<root>"


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise Violation(message)


def raw_bytes(call: dict[str, Any], stream: str, raw_root: Path | None, label: str) -> bytes:
    meta = need(call, stream, kind=dict)
    relative = need(meta, "path", kind=str)
    declared = need(meta, "sha256", kind=str)
    size = need(meta, "bytes", kind=int)
    if raw_root is None:
        raise Missing(f"{label}: raw log root was not provided")
    root = raw_root.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise Violation(f"{label}: raw {stream} path escapes the evidence root")
    if not path.is_file():
        raise Missing(f"{label}: raw {stream} file missing: {relative}")
    data = path.read_bytes()
    expect(sha256_bytes(data) == declared, f"{label}: raw {stream} hash mismatch")
    expect(len(data) == size, f"{label}: raw {stream} size mismatch")
    return data


def cli_call(
    observation: dict[str, Any], raw_root: Path | None, label: str, *, expect_json: bool = True
) -> dict[str, Any]:
    """子Process記録の形・timeout・残存Process・生ログHashを確かめ、stdout JSONを読み直す。"""
    call = observation if isinstance(observation, dict) else {}
    if not call:
        raise Missing(f"{label}: CLI record missing")
    need(call, "argv", kind=list)
    need(call, "returncode", kind=int)
    expect(need(call, "timed_out", kind=bool) is False, f"{label}: child process timed out")
    expect(
        need(call, "residual_process_group", kind=str) == "ABSENT",
        f"{label}: child process group not confirmed absent",
    )
    stdout = raw_bytes(call, "stdout", raw_root, label)
    raw_bytes(call, "stderr", raw_root, label)
    if not expect_json:
        return {"_stdout_text": stdout.decode("utf-8", errors="strict")}
    parsed, error = parse_json_line(stdout)
    expect(
        call.get("stdout_json") == parsed,
        f"{label}: recorded stdout_json contradicts the raw stdout bytes",
    )
    if parsed is None:
        raise Violation(f"{label}: stdout is not a single JSON object ({error})")
    return parsed


def returncode(observation: dict[str, Any]) -> int:
    return int(need(observation, "returncode", kind=int))


def refused_attempt(attempt: Any, label: str) -> None:
    returned = need(attempt, "returned", kind=bool)
    expect(
        returned is False,
        f"{label}: entry completed instead of being refused by the operation gate",
    )
    exception_type = need(attempt, "exception_type", kind=str)
    code = need(attempt, "error_code", kind=(str, type(None)))
    expect(
        exception_type == REFUSAL_EXCEPTION,
        f"{label}: refused by {exception_type}, not by the operation gate",
    )
    expect(code == REFUSAL_CODE, f"{label}: refusal code {code!r} is not {REFUSAL_CODE}")


def completed_attempt(attempt: Any, label: str) -> None:
    returned = need(attempt, "returned", kind=bool)
    expect(
        returned is True,
        f"{label}: control entry did not complete "
        f"({attempt.get('exception_type')}: {attempt.get('error_code')})",
    )


def cli_refused(observation: Any, raw_root: Path | None, label: str) -> None:
    body = cli_call(observation, raw_root, label)
    expect(returncode(observation) == 2, f"{label}: returncode {returncode(observation)} != 2")
    expect(body.get("planned") is False, f"{label}: plan-task did not report planned=false")
    expect(
        body.get("error_code") == REFUSAL_CODE,
        f"{label}: refusal code {body.get('error_code')!r} is not {REFUSAL_CODE}",
    )


def cli_planned(observation: Any, raw_root: Path | None, label: str) -> None:
    body = cli_call(observation, raw_root, label)
    expect(returncode(observation) == 0, f"{label}: returncode {returncode(observation)} != 0")
    expect(body.get("planned") is True, f"{label}: plan-task did not plan")


def snap(value: Any, label: str) -> dict[str, Any]:
    need(value, "operation_control", kind=dict)
    mode = need(value, "operation_control", "mode", kind=str)
    need(value, "operation_control", "version", kind=int)
    need(value, "operation_control", "refusals", kind=int)
    if mode not in ("OPEN", "DRAINING", "RESTORING"):
        raise Violation(f"{label}: unknown operation mode {mode!r}")
    for field in (
        "operation_admission_kinds",
        "operation_journal_states",
        "cli_invocation_journal_states",
        "approval_grant_statuses",
        "workbench_session_states",
    ):
        mapping = need(value, field, kind=dict)
        if not all(isinstance(k, str) and _matches(v, int) for k, v in mapping.items()):
            raise Missing(f"{label}.{field}: malformed counts")
    for field in ("event_ledger_rows", "artifact_manifest_rows", "cas_object_files"):
        need(value, field, kind=int)
    for field in ("event_ledger_sha256", "artifact_manifest_sha256", "cas_tree_sha256"):
        need(value, field, kind=str)
    need(value, "effect_port_calls", kind=int)
    need(value, "runner_calls", kind=(int, type(None)))
    need(value, "fake_cli_launches", kind=(int, type(None)))
    need(value, "worktree_target_sha256", kind=(str, type(None)))
    need(value, "database", kind=str)
    return dict(value)


def mode(snapshot: dict[str, Any]) -> str:
    return str(snapshot["operation_control"]["mode"])


def total(mapping: dict[str, int]) -> int:
    return sum(mapping.values())


def unchanged(
    before: dict[str, Any], after: dict[str, Any], label: str, *, allowed: tuple[str, ...] = ()
) -> None:
    for field in COMPARED_FIELDS:
        if field in allowed:
            continue
        expect(
            before[field] == after[field],
            f"{label}: {field} changed {before[field]!r} -> {after[field]!r}",
        )


def executed_effect_delta(before: dict[str, Any], after: dict[str, Any]) -> int:
    """拒否区間で増えた作用の数。**拒否した試行数とは別に数える。**"""
    delta = after["effect_port_calls"] - before["effect_port_calls"]
    for field in ("runner_calls", "fake_cli_launches"):
        if before[field] is not None and after[field] is not None:
            delta += after[field] - before[field]
    if before["worktree_target_sha256"] != after["worktree_target_sha256"]:
        delta += 1
    delta += total(after["workbench_session_states"]) - total(before["workbench_session_states"])
    delta += after["event_ledger_rows"] - before["event_ledger_rows"]
    delta += after["cas_object_files"] - before["cas_object_files"]
    return delta


def expected_active(snapshot: dict[str, Any]) -> list[list[Any]]:
    entries: list[list[Any]] = [
        [state, count]
        for state, count in snapshot["operation_journal_states"].items()
        if state not in TERMINAL_OPERATION_JOURNAL_STATES and count
    ]
    entries += [
        [CLI_INVOCATION_LABEL + state, count]
        for state, count in snapshot["cli_invocation_journal_states"].items()
        if state not in TERMINAL_CLI_INVOCATION_STATES and count
    ]
    admitted = total(snapshot["operation_admission_kinds"])
    if admitted:
        entries.append(["ADMITTED", admitted])
    return sorted(entries)


def expected_pending(snapshot: dict[str, Any]) -> list[list[Any]]:
    issued = snapshot["approval_grant_statuses"].get("ISSUED", 0)
    return [["ISSUED", issued]] if issued else []


def pairs(value: Any, label: str) -> list[list[Any]]:
    if not isinstance(value, list) or not all(
        isinstance(item, list)
        and len(item) == 2
        and isinstance(item[0], str)
        and _matches(item[1], int)
        for item in value
    ):
        raise Missing(f"{label}: malformed state counts")
    return sorted(value)


def drain_body(observation: Any, raw_root: Path | None, label: str) -> dict[str, Any]:
    body = cli_call(observation, raw_root, label)
    for field, kind in (
        ("deployment_result_id", str),
        ("state", str),
        ("error_code", (str, type(None))),
        ("events", list),
        ("active_run_count", int),
        ("pending_approval_count", int),
        ("migration_started", bool),
        ("intake_stopped", bool),
        ("deadline_exceeded", bool),
        ("observed_at", str),
        ("observation_source", str),
    ):
        need(body, field, kind=kind)
    active = pairs(body.get("observed_active_states"), f"{label}.observed_active_states")
    pending = pairs(body.get("observed_pending_statuses"), f"{label}.observed_pending_statuses")
    expect(
        body["active_run_count"] == sum(count for _name, count in active),
        f"{label}: active_run_count contradicts observed_active_states",
    )
    expect(
        body["pending_approval_count"] == sum(count for _name, count in pending),
        f"{label}: pending_approval_count contradicts observed_pending_statuses",
    )
    rc = returncode(observation)
    expect(rc in (0, 2), f"{label}: unexpected returncode {rc}")
    expect(
        (rc == 0) == (body["state"] == "ACCEPTED"),
        f"{label}: returncode {rc} contradicts state {body['state']}",
    )
    return body


# ---------------------------------------------------------------------------
# Check 実行
# ---------------------------------------------------------------------------


def _run(
    check_id: str, item: str, requirement: str, function: Callable[[], None]
) -> dict[str, Any]:
    try:
        function()
    except Missing as exc:
        return _result(check_id, item, requirement, UNVERIFIED, f"MISSING: {exc}")
    except Violation as exc:
        return _result(check_id, item, requirement, FAIL, f"VIOLATION: {exc}")
    except (TypeError, KeyError, AttributeError, IndexError, ValueError) as exc:
        # 形の崩れた観測値。推測で読まず、見ていない扱いにする。
        return _result(
            check_id, item, requirement, UNVERIFIED, f"MALFORMED: {type(exc).__name__}: {exc}"
        )
    return _result(check_id, item, requirement, PASS, None)


def _result(
    check_id: str, item: str, requirement: str, status: str, reason: str | None
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "item": item,
        "requirement": requirement,
        "status": status,
        "reasons": [] if reason is None else [reason],
    }


# ---------------------------------------------------------------------------
# 共通
# ---------------------------------------------------------------------------


def _check_integrity(obs: Any) -> None:
    expect(need(obs, "contract", kind=str) == CONTRACT, "observation contract mismatch")
    if isinstance(obs, dict) and "halted" in obs:
        raise Violation(f"observation halted: {obs['halted'].get('reason')!r}")
    repo = Path(need(obs, "repo", kind=str))
    modules = need(obs, "implementation_modules", kind=dict)
    expect(bool(modules), "no implementation module paths recorded")
    for name, location in modules.items():
        expect(
            isinstance(location, str) and Path(location).is_relative_to(repo / "src"),
            f"{name} was imported from {location!r}, outside the measured repository",
        )


# ---------------------------------------------------------------------------
# application_drain
# ---------------------------------------------------------------------------


def _check_drain(obs: Any, kind: str, raw_root: Path | None) -> None:
    entry = need(obs, "drain", kind, kind=dict)
    label = f"drain[{kind}]"
    database = need(entry, "environment", "database", kind=str)
    need(entry, "environment", "created_new", kind=bool)
    expect(entry["environment"]["created_new"] is True, f"{label}: environment was not new")
    before = snap(need(entry, "snapshot_before_drain"), label)
    after = snap(need(entry, "snapshot_after_drain"), label)
    body = drain_body(need(entry, "drain", kind=dict), raw_root, label)
    rc = returncode(entry["drain"])
    expect(body["observation_source"] == database, f"{label}: drain observed another database")
    expect(mode(before) == "OPEN", f"{label}: mode before drain is {mode(before)}")
    expect(mode(after) == "DRAINING", f"{label}: mode after drain is {mode(after)}")
    unchanged(before, after, f"{label} drain")
    expect(
        pairs(body["observed_active_states"], label) == expected_active(before),
        f"{label}: observed_active_states {body['observed_active_states']} do not match "
        f"the database {expected_active(before)}",
    )
    expect(
        pairs(body["observed_pending_statuses"], label) == expected_pending(before),
        f"{label}: observed_pending_statuses do not match the database",
    )
    expect(body["intake_stopped"] is True, f"{label}: intake_stopped is not true")
    if kind == "QUIESCENT":
        expect(rc == 0 and body["state"] == "ACCEPTED", f"{label}: quiesced drain not accepted")
        expect(body["error_code"] is None and body["events"] == [], f"{label}: unexpected events")
        expect(body["migration_started"] is True, f"{label}: migration_started is not true")
        expect(body["deadline_exceeded"] is False, f"{label}: deadline exceeded")
        _check_open_control(entry, raw_root, label)
    else:
        expect(rc == 2 and body["state"] == "REJECTED", f"{label}: drain was not rejected")
        expect(body["error_code"] == REFUSAL_CODE, f"{label}: error_code {body['error_code']!r}")
        expect(body["events"] == ["ACTION_BLOCKED"], f"{label}: events {body['events']!r}")
        expect(body["migration_started"] is False, f"{label}: migration_started is not false")
        journals = [row for row in expected_active(before) if row[0] != "ADMITTED"]
        cli_rows = [row for row in journals if row[0].startswith(CLI_INVOCATION_LABEL)]
        op_rows = [row for row in journals if not row[0].startswith(CLI_INVOCATION_LABEL)]
        admitted = total(before["operation_admission_kinds"])
        issued = before["approval_grant_statuses"].get("ISSUED", 0)
        # 前提ごとに「その1種類だけ」があることを確かめる。混ざると何が拒否させたか言えない。
        only = {
            "ACTIVE_OPERATION_JOURNAL": bool(op_rows) and not cli_rows and not admitted,
            "ACTIVE_CLI_INVOCATION": bool(cli_rows) and not op_rows and not admitted,
            "PENDING_APPROVAL": issued > 0 and not journals and not admitted,
            "ADMISSION_RESERVED": admitted > 0 and not journals and not issued,
        }[kind]
        expect(only, f"{label}: precondition is not isolated in the database snapshot")
        if not kind.startswith("PENDING"):
            expect(issued == 0, f"{label}: unexpected pending approval")
    reference = after
    if kind == "ADMISSION_RESERVED":
        released = snap(need(entry, "snapshot_after_release"), label)
        expect(total(released["operation_admission_kinds"]) == 0, f"{label}: reservation left")
        expect(mode(released) == "DRAINING", f"{label}: release reopened intake")
        unchanged(after, released, f"{label} release", allowed=("operation_admission_kinds",))
        reference = released
    probes = need(entry, "probes_after_drain", kind=dict)
    cli_refused(need(probes, "plan_task"), raw_root, f"{label} plan-task after drain")
    refused_attempt(
        need(probes, "workbench_create_session"), f"{label} workbench create after drain"
    )
    after_probes = snap(need(entry, "snapshot_after_probes"), label)
    expect(mode(after_probes) == "DRAINING", f"{label}: probes reopened intake")
    expect(
        after_probes["operation_control"]["version"] == reference["operation_control"]["version"],
        f"{label}: operation control changed during refused probes",
    )
    unchanged(reference, after_probes, f"{label} refused probes")


def _check_open_control(entry: dict[str, Any], raw_root: Path | None, label: str) -> None:
    control = need(entry, "open_control", kind=dict)
    before = snap(need(control, "snapshot_before"), label)
    after = snap(need(control, "snapshot_after"), label)
    expect(mode(before) == "OPEN" and mode(after) == "OPEN", f"{label}: control not in OPEN")
    cli_planned(need(control, "plan_task"), raw_root, f"{label} plan-task control")
    expect(
        after["event_ledger_rows"] > before["event_ledger_rows"],
        f"{label}: plan-task control did not reach the ledger",
    )
    completed_attempt(need(control, "workbench_create_session"), f"{label} workbench control")
    expect(
        total(after["workbench_session_states"]) == total(before["workbench_session_states"]) + 1,
        f"{label}: workbench control did not create exactly one session",
    )


# ---------------------------------------------------------------------------
# effects_during_restore
# ---------------------------------------------------------------------------


def _window(obs: Any) -> dict[str, Any]:
    return dict(need(obs, "restore_window", kind=dict))


def _check_window_order(obs: Any) -> None:
    window = _window(obs)
    need(window, "environment", "created_new", kind=bool)
    expect(window["environment"]["created_new"] is True, "restore window: environment not new")
    target = need(window, "restore_target_before", kind=dict)
    expect(
        need(target, "database_exists", kind=bool) is False
        and need(target, "cas_exists", kind=bool) is False,
        "restore window: restore target existed before restore",
    )
    checkpoint = need(window, "checkpoint", kind=dict)
    expect(need(checkpoint, "reached", kind=bool) is True, "restore window: checkpoint not reached")
    expect(
        need(checkpoint, "restore_database_exists", kind=bool) is True,
        "restore window: database was not copied before the checkpoint",
    )
    expect(
        need(checkpoint, "restore_cas_exists", kind=bool) is False,
        "restore window: CAS copy had started before the checkpoint",
    )
    during = snap(need(checkpoint, "snapshot_before_attempts"), "restore window")
    expect(mode(during) == "RESTORING", f"restore window: mode is {mode(during)} in the window")
    expect(
        need(window, "restore_target_class", kind=str)
        == "harness.infrastructure.sqlite.operations_repository.SqliteRestoreTarget",
        "restore window: restore target is not the production SqliteRestoreTarget",
    )


def _check_window_refusals(obs: Any, raw_root: Path | None) -> None:
    window = _window(obs)
    checkpoint = need(window, "checkpoint", kind=dict)
    attempts = need(checkpoint, "attempts", kind=list)
    kinds = need(window, "effect_kinds_declared", kind=list)
    expect(bool(kinds), "restore window: no effect kinds declared")
    required = [f"EffectOrchestrator.execute[{kind}]" for kind in kinds]
    required += list(WINDOW_WORKBENCH_ENTRIES)
    by_entry = {need(item, "entrypoint", kind=str): item for item in attempts}
    expect(len(by_entry) == len(attempts), "restore window: duplicate attempt entrypoints")
    for entrypoint in required:
        attempt = by_entry.get(entrypoint)
        if attempt is None:
            raise Missing(f"restore window: attempt missing for {entrypoint}")
        refused_attempt(attempt, f"restore window {entrypoint}")
        expect(
            need(attempt, "caller", kind=str) == "separate-thread",
            f"restore window {entrypoint}: not called from a separate thread",
        )
    cli_refused(need(checkpoint, "plan_task"), raw_root, "restore window plan-task")


def _check_window_no_effect(obs: Any) -> None:
    window = _window(obs)
    checkpoint = need(window, "checkpoint", kind=dict)
    before = snap(need(checkpoint, "snapshot_before_attempts"), "restore window")
    after = snap(need(checkpoint, "snapshot_after_attempts"), "restore window")
    attempts = need(checkpoint, "attempts", kind=list)
    expect(mode(after) == "RESTORING", "restore window: mode changed inside the window")
    unchanged(before, after, "restore window")
    expect(executed_effect_delta(before, after) == 0, "restore window: effects executed")
    refused = sum(1 for item in attempts if item.get("exception_type") == REFUSAL_EXCEPTION)
    delta = after["operation_control"]["refusals"] - before["operation_control"]["refusals"]
    expect(
        delta == refused == len(attempts),
        f"restore window: refusal counter delta {delta} does not match {refused} refusals",
    )
    outcome = need(window, "restore_outcome", kind=dict)
    counted = need(outcome, "service_effects_during_restore", kind=int)
    expect(counted == delta, "restore window: service count differs from the refusal counter")
    state = need(outcome, "state", kind=str)
    expect(
        state == ("REJECTED" if counted else "ACCEPTED"),
        f"restore window: restore state {state} with {counted} counted attempts",
    )
    closing = snap(need(window, "snapshot_after_restore"), "restore window")
    if state == "REJECTED":
        expect(mode(closing) == "RESTORING", "restore window: rejected restore reopened")
    else:
        expect(mode(closing) == "DRAINING", "restore window: accepted restore reopened")


def _check_open_controls(obs: Any, raw_root: Path | None) -> None:
    controls = need(_window(obs), "controls_open", kind=dict)
    effects = need(controls, "effects", kind=list)
    kinds = need(_window(obs), "effect_kinds_declared", kind=list)
    seen = set()
    for item in effects:
        kind = need(item, "effect_kind", kind=str)
        seen.add(kind)
        completed_attempt(need(item, "attempt"), f"open control effect {kind}")
        expect(
            need(item, "port_calls_after", kind=int) > need(item, "port_calls_before", kind=int),
            f"open control effect {kind}: no port was reached",
        )
    expect(seen == set(kinds), "open control: not every effect kind was exercised")
    create = need(controls, "workbench_create_session", kind=dict)
    completed_attempt(need(create, "attempt"), "open control create_session")
    expect(
        need(create, "sessions_after", kind=int) == need(create, "sessions_before", kind=int) + 1,
        "open control: create_session did not add a session",
    )
    send = need(controls, "workbench_send", kind=dict)
    for step in ("approve_send", "start_send"):
        completed_attempt(need(send, step), f"open control {step}")
    expect(need(send, "idle", kind=bool) is True, "open control: send did not finish")
    expect(
        need(send, "runner_calls_after", kind=int) - need(send, "runner_calls_before", kind=int)
        == 1,
        "open control: send did not reach the runner exactly once",
    )
    expect(
        need(send, "launches_after", kind=int) > need(send, "launches_before", kind=int),
        "open control: send did not launch the synthetic CLI",
    )
    expect(
        need(send, "session_state", kind=str) == "PROPOSAL_READY",
        "open control: send did not produce a proposal",
    )
    apply = need(controls, "workbench_apply", kind=dict)
    for step in ("approve_apply", "apply"):
        completed_attempt(need(apply, step), f"open control {step}")
    expect(
        need(apply, "target_sha256_after", kind=str)
        != need(apply, "target_sha256_before", kind=str),
        "open control: apply did not change the worktree file",
    )
    expect(need(apply, "session_state", kind=str) == "APPLIED", "open control: not APPLIED")
    cli_planned(need(controls, "plan_task"), raw_root, "open control plan-task")


def _check_start_conflict(obs: Any, kind: str, raw_root: Path | None) -> None:
    entry = need(obs, "restore_start_conflicts", kind, kind=dict)
    label = f"restore start[{kind}]"
    before = snap(need(entry, "snapshot_before_verify"), label)
    after = snap(need(entry, "snapshot_after_verify"), label)
    if kind == "PENDING_APPROVAL":
        expect(before["approval_grant_statuses"].get("ISSUED", 0) > 0, f"{label}: no approval")
    else:
        expect(total(before["operation_admission_kinds"]) > 0, f"{label}: no reservation")
    created = cli_call(need(entry, "backup_create"), raw_root, f"{label} backup create")
    expect(returncode(entry["backup_create"]) == 0, f"{label}: backup create failed")
    need(created, "backup_id", kind=str)
    target_before = need(entry, "restore_target_before", kind=dict)
    expect(
        need(target_before, "database_exists", kind=bool) is False
        and need(target_before, "cas_exists", kind=bool) is False,
        f"{label}: restore target existed before verify",
    )
    body = cli_call(need(entry, "backup_verify"), raw_root, f"{label} backup verify")
    expect(returncode(entry["backup_verify"]) == 2, f"{label}: verify returncode is not 2")
    expect(body.get("state") == "REJECTED", f"{label}: verify state {body.get('state')!r}")
    expect(body.get("error_code") == REFUSAL_CODE, f"{label}: {body.get('error_code')!r}")
    expect(body.get("error_type") == "IntakeRefused", f"{label}: {body.get('error_type')!r}")
    target_after = need(entry, "restore_target_after", kind=dict)
    expect(
        need(target_after, "database_exists", kind=bool) is False
        and need(target_after, "cas_exists", kind=bool) is False,
        f"{label}: restore started despite the conflict",
    )
    expect(mode(before) == "OPEN" and mode(after) == "OPEN", f"{label}: mode changed")
    expect(
        before["operation_control"] == after["operation_control"],
        f"{label}: operation control changed by a refused restore start",
    )
    # 競合を消して通したのではないことを、承認と予約が残っていることで示す。
    unchanged(before, after, label)


def _separate_runtime(entry: dict[str, Any], raw_root: Path | None, label: str) -> None:
    separate = need(entry, "separate_runtime", kind=dict)
    cli_refused(need(separate, "plan_task"), raw_root, f"{label} separate plan-task")
    effect = need(separate, "effect", kind=dict)
    refused_attempt(need(effect, "attempt"), f"{label} separate effect")
    expect(
        need(effect, "port_calls_after", kind=int) == need(effect, "port_calls_before", kind=int),
        f"{label}: separate runtime effect reached a port",
    )
    refused_attempt(
        need(separate, "workbench_create_session"), f"{label} separate workbench create"
    )
    before = snap(need(entry, "snapshot_after_verify"), label)
    after = snap(need(separate, "snapshot_after"), label)
    expect(mode(after) == "RESTORING", f"{label}: separate runtime reopened ({mode(after)})")
    unchanged(before, after, f"{label} separate runtime")
    expect(
        after["operation_control"]["refusals"] - before["operation_control"]["refusals"] == 2,
        f"{label}: separate runtime refusals were not recorded by the gate",
    )


def _check_failure(obs: Any, kind: str, raw_root: Path | None) -> None:
    entry = need(obs, "failure_retention", kind, kind=dict)
    label = f"failure[{kind}]"
    created = cli_call(need(entry, "backup_create"), raw_root, f"{label} backup create")
    expect(returncode(entry["backup_create"]) == 0, f"{label}: backup create failed")
    need(created, "backup_id", kind=str)
    before = snap(need(entry, "snapshot_before_verify"), label)
    expect(mode(before) == "OPEN", f"{label}: mode before verify is {mode(before)}")
    body = cli_call(need(entry, "backup_verify"), raw_root, f"{label} backup verify")
    rc = returncode(entry["backup_verify"])
    expect(rc != 0, f"{label}: verify returned 0")
    expect(body.get("state") == "REJECTED", f"{label}: verify state {body.get('state')!r}")
    after = snap(need(entry, "snapshot_after_verify"), label)
    expect(mode(after) == "RESTORING", f"{label}: stop not retained ({mode(after)})")
    target = need(entry, "restore_target_after", kind=dict)
    expect(need(target, "database_exists", kind=bool) is True, f"{label}: restore never ran")
    if kind == "COPY_FAILURE":
        damage = need(entry, "damage", kind=dict)
        expect(need(damage, "existed_before", kind=bool) is True, f"{label}: nothing damaged")
        expect(need(damage, "exists_after", kind=bool) is False, f"{label}: damage not applied")
        expect(
            body.get("error_code") == "ARTIFACT_CONTENT_CONFLICT",
            f"{label}: copy failure reported {body.get('error_code')!r}",
        )
        expect(
            need(target, "cas_object_files", kind=int)
            < need(entry, "source_cas_object_files", kind=int),
            f"{label}: CAS copy was not interrupted",
        )
    else:
        cli_planned(need(entry, "post_backup_change"), raw_root, f"{label} post-backup change")
        expect(rc == 2, f"{label}: verify returncode {rc}")
        problems = body.get("problems")
        expect(isinstance(problems, list) and bool(problems), f"{label}: no problems reported")
        expect(
            body.get("error_code") in ("LEDGER_CHAIN_TAMPERED", "BACKUP_RESTORE_FAILED"),
            f"{label}: verification error_code {body.get('error_code')!r}",
        )
        expect(need(target, "cas_exists", kind=bool) is True, f"{label}: CAS not restored")
    _separate_runtime(entry, raw_root, label)


# ---------------------------------------------------------------------------
# operator_restore_workflow
# ---------------------------------------------------------------------------


def _check_operator_flow(obs: Any, raw_root: Path | None) -> None:
    flow = need(obs, "operator_flow", kind=dict)
    label = "operator flow"
    created = cli_call(need(flow, "backup_create"), raw_root, f"{label} backup create")
    expect(returncode(flow["backup_create"]) == 0, f"{label}: backup create failed")
    verified = cli_call(need(flow, "backup_verify"), raw_root, f"{label} backup verify")
    expect(returncode(flow["backup_verify"]) == 0, f"{label}: backup verify failed")
    expect(verified.get("state") == "ACCEPTED", f"{label}: verify {verified.get('state')!r}")
    head = verified.get("restored_chain_head")
    expect(
        isinstance(head, str) and head.startswith("sha256:"),
        f"{label}: restored head missing",
    )
    expect(head == verified.get("original_chain_head"), f"{label}: chain heads differ")
    expect(verified.get("artifact_manifest_count_equal") is True, f"{label}: manifest count")
    expect(verified.get("effects_during_restore") == 0, f"{label}: attempts during restore")
    expect(verified.get("problems") == [], f"{label}: problems {verified.get('problems')!r}")
    drained = drain_body(need(flow, "drain"), raw_root, f"{label} drain")
    expect(drained["state"] == "ACCEPTED", f"{label}: drain {drained['state']}")

    comparison = need(flow, "content_comparison", kind=dict)
    views = {name: need(comparison, name, kind=dict) for name in ("source", "backup", "restored")}
    for name, view in views.items():
        expect("error" not in view, f"{label}: {name} could not be re-read: {view.get('error')}")
        need(view, "streams", kind=list)
        need(view, "artifacts", kind=list)
        need(view, "manifest_rows", kind=list)
        need(view, "ledger_rows_sha256", kind=str)
    for field in ("streams", "artifacts", "manifest_rows", "ledger_rows_sha256", "integrity_check"):
        expect(
            views["source"][field] == views["backup"][field] == views["restored"][field],
            f"{label}: {field} differs between source, backup and restored",
        )
    expect(views["restored"]["integrity_check"] == ["ok"], f"{label}: integrity check")
    artifacts = views["restored"]["artifacts"]
    expect(bool(artifacts), f"{label}: no artifacts to compare")
    expect(
        sorted(need(created, "artifact_ids", kind=list))
        == sorted(item["id"] for item in artifacts),
        f"{label}: backup artifact ids differ from the re-read CAS",
    )
    expect(
        sorted(tuple(pair) for pair in need(created, "source_chain_heads", kind=list))
        == sorted((item["stream_id"], item["head"]) for item in views["restored"]["streams"]),
        f"{label}: backup chain heads differ from the re-read ledger",
    )


def _check_no_auto_open(obs: Any, raw_root: Path | None) -> None:
    flow = need(obs, "operator_flow", kind=dict)
    after_verify = snap(need(flow, "snapshot_after_verify"), "operator flow")
    after_drain = snap(need(flow, "snapshot_after_drain"), "operator flow")
    expect(mode(after_verify) == "DRAINING", f"operator flow: verify left {mode(after_verify)}")
    expect(mode(after_drain) == "DRAINING", f"operator flow: drain left {mode(after_drain)}")
    cli_refused(need(flow, "intake_after_drain"), raw_root, "operator flow intake after drain")


def _check_resume_not_faked(obs: Any, raw_root: Path | None) -> None:
    flow = need(obs, "operator_flow", kind=dict)
    resume = need(flow, "resume", kind=dict)
    decision = need(resume, "decision", kind=dict)
    drained = drain_body(need(flow, "drain"), raw_root, "operator flow drain")
    expect(
        need(decision, "state", kind=str) == drained["state"],
        "operator flow: resume decision does not quote the drain verdict",
    )
    expect(
        need(decision, "meaning", kind=str) == "DEPLOY_VERDICT_NOT_RESUME",
        "operator flow: drain verdict is presented as a resume",
    )
    execution = need(resume, "execution", kind=str)
    help_text = cli_call(need(resume, "deploy_help"), raw_root, "deploy --help", expect_json=False)[
        "_stdout_text"
    ]
    expect(returncode(resume["deploy_help"]) == 0, "operator flow: deploy --help failed")
    observed = need(resume, "deploy_subcommands_observed", kind=list)
    expect(
        all(name in help_text for name in observed) and bool(observed),
        "operator flow: recorded deploy subcommands do not appear in the raw help text",
    )
    after_drain = snap(need(flow, "snapshot_after_drain"), "operator flow")
    expect(mode(after_drain) != "OPEN", "operator flow: intake reopened without a resume")
    if not any("resume" in name for name in observed):
        expect(execution == "NOT_PERFORMED", "resume claimed without an operator entry")
        return
    expect(execution in ("NOT_PERFORMED", "PERFORMED"), "unknown resume execution state")
    if execution != "PERFORMED":
        raise Missing("a resume entry exists but was not exercised")
    inspection = need(resume, "inspection")
    review = cli_call(inspection, raw_root, "operator inspect")
    expect(returncode(inspection) == 0, "operator inspect failed")
    expect(
        review.get("eligible") is True and review.get("resumed") is False,
        "inspection was not an eligible unexecuted review",
    )
    call = need(resume, "call")
    result = cli_call(call, raw_root, "operator resume")
    expect(
        returncode(call) == 0 and result.get("resumed") is True, "operator resume did not succeed"
    )
    expect(result.get("review") == review, "resume was not bound to the inspected state")
    expect(
        mode(snap(need(resume, "snapshot_after"), "after resume")) == "OPEN",
        "resume did not open persisted intake",
    )
    replay = need(resume, "replay")
    denied = cli_call(replay, raw_root, "operator replay")
    expect(
        returncode(replay) == 2 and denied.get("error_code") == "APPROVAL_INVALIDATED",
        "replayed approval was not refused",
    )
    intake = need(resume, "intake_after")
    cli_planned(intake, raw_root, "operator intake after resume")
    verification = need(resume, "verify_ledger")
    cli_call(verification, raw_root, "resume ledger")
    expect(returncode(verification) == 0, "resume ledger verification failed")


# ---------------------------------------------------------------------------
# 集計
# ---------------------------------------------------------------------------


def checks(observations: Any, *, raw_root: Path | None) -> list[dict[str, Any]]:
    drain = "application_drain"
    effects = "effects_during_restore"
    operator = "operator_restore_workflow"
    results = [
        _run(
            "C-INTEGRITY",
            "collection",
            "観測契約が一致し、途中停止が無く、実装Moduleが測定対象Repositoryから読まれた",
            lambda: _check_integrity(observations),
        )
    ]
    for kind in DRAIN_ENVIRONMENTS:
        results.append(
            _run(
                f"D-{kind}",
                drain,
                f"{kind}: drain判定がDB実数と一致し、drain後は通常入口が受付を拒否し追加が無い",
                lambda kind=kind: _check_drain(observations, kind, raw_root),
            )
        )
    results += [
        _run(
            "R-WINDOW-ORDER",
            effects,
            "新規復元先へ本物のSqliteRestoreTargetがDBを写した後・CAS前の同期点でRESTORING",
            lambda: _check_window_order(observations),
        ),
        _run(
            "R-WINDOW-REFUSALS",
            effects,
            "区間内で別Threadの通常作用入口・Workbench入口・別Processのplan-taskが"
            "同一DBのGateで拒否された",
            lambda: _check_window_refusals(observations, raw_root),
        ),
        _run(
            "R-WINDOW-NO-EFFECT",
            effects,
            "区間内の作用数0（Port/Runner/起動/File/Session/Ledger/CAS不変）、"
            "拒否数は拒否Counter差分と一致",
            lambda: _check_window_no_effect(observations),
        ),
        _run(
            "R-OPEN-CONTROLS",
            effects,
            "OPEN時は同じ種類の入口が合成処理まで到達する",
            lambda: _check_open_controls(observations, raw_root),
        ),
    ]
    for kind in RESTORE_START_CONFLICTS:
        results.append(
            _run(
                f"R-START-{kind}",
                effects,
                f"{kind}: 未決状態がある復元開始を拒否し、承認・予約を消さない",
                lambda kind=kind: _check_start_conflict(observations, kind, raw_root),
            )
        )
    for kind in FAILURE_KINDS:
        results.append(
            _run(
                f"R-{kind}",
                effects,
                f"{kind}: 失敗後もRESTORINGを保持し、別Runtimeの受付と作用を拒否する",
                lambda kind=kind: _check_failure(observations, kind, raw_root),
            )
        )
    results += [
        _run(
            "O-CLI-FLOW-CONTENT",
            operator,
            "実CLI create→verify→drain と、Ledger連鎖・Artifact Hashの再読取り照合",
            lambda: _check_operator_flow(observations, raw_root),
        ),
        _run(
            "O-NO-AUTO-OPEN",
            operator,
            "正常復元後もDRAININGで、drain後の新規受付は拒否される",
            lambda: _check_no_auto_open(observations, raw_root),
        ),
        _run(
            "O-RESUME-NOT-FAKED",
            operator,
            "drain判定を再開と書かず、再開操作は実行していないことを記録した",
            lambda: _check_resume_not_faked(observations, raw_root),
        ),
    ]
    return results


def _rollup(statuses: list[str]) -> str:
    if statuses and all(status == PASS for status in statuses):
        return PASS
    if FAIL in statuses:
        return FAIL
    return UNVERIFIED


def counts(observations: Any) -> dict[str, Any]:
    """拒否した試行数と、実行された作用数を**別の数**として出す。読めなければ None。"""
    result: dict[str, Any] = {}
    try:
        window = observations["restore_window"]["checkpoint"]
        before, after = window["snapshot_before_attempts"], window["snapshot_after_attempts"]
        result["restore_window"] = {
            "refused_in_process_attempts": sum(
                1 for item in window["attempts"] if item.get("exception_type") == REFUSAL_EXCEPTION
            ),
            "refused_cli_attempts": 1 if window["plan_task"].get("returncode") == 2 else 0,
            "refusal_counter_delta": after["operation_control"]["refusals"]
            - before["operation_control"]["refusals"],
            "executed_effects": executed_effect_delta(before, after),
        }
    except (KeyError, TypeError, AttributeError):
        result["restore_window"] = None
    for kind in FAILURE_KINDS:
        try:
            entry = observations["failure_retention"][kind]
            before = entry["snapshot_after_verify"]
            after = entry["separate_runtime"]["snapshot_after"]
            effect = entry["separate_runtime"]["effect"]
            result[kind] = {
                "refused_separate_runtime_attempts": sum(
                    1
                    for item in (
                        effect["attempt"],
                        entry["separate_runtime"]["workbench_create_session"],
                    )
                    if item.get("exception_type") == REFUSAL_EXCEPTION
                )
                + (1 if entry["separate_runtime"]["plan_task"].get("returncode") == 2 else 0),
                "executed_effects": executed_effect_delta(before, after)
                + (effect["port_calls_after"] - effect["port_calls_before"]),
            }
        except (KeyError, TypeError, AttributeError):
            result[kind] = None
    return result


def evaluate(observations: Any, *, raw_root: Path | None) -> dict[str, Any]:
    results = checks(observations, raw_root=raw_root)
    items: dict[str, str] = {}
    for item in sorted({result["item"] for result in results}):
        items[item] = _rollup([r["status"] for r in results if r["item"] == item])
    return {
        "contract": "cc-ops-evidence-01/backup-restore-predicates/1",
        "status": _rollup([result["status"] for result in results]),
        "items": items,
        "checks": results,
        "counts": counts(observations),
    }
