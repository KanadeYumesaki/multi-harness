#!/usr/bin/env python3
"""`backup_restore` 領域の観測器。**本番の組立てと入口を使い、合成I/Oだけで測る。**

## 判定しない

ここは「何が起きたか」を記録するだけで、PASS／FAIL の Bool を書かない。
合否は `tools/backup_restore_predicates.py` が、この記録と生ログのBytesから導く。

## 何を本番のまま使い、何を合成にするか

| 観測 | 本番のまま | 合成 |
|---|---|---|
| Drain | `harness deploy drain --check`（子Process） | DB/CAS の中身 |
| 新規受付 | `harness plan-task`、`build_services()` のGateway | 偽CLI、境界測定の代役 |
| 作用入口 | `EffectOrchestrator` ＋ `intake_gate()` | 外へ出ない計数Port |
| 復元 | `backup_restore_service()` の `SqliteRestoreTarget`、CLI `backup verify` | — |

`RestoreGuard` を作用Portへ差し込む `--probe-effect` は使わない。部品試験であり、
通常入口の保護を示さない。

## 止まったら推測しない

子Processのtimeout・残存Process・前提手順の失敗は `ObservationHalted` で止め、
途中までの記録を残す。作用を伴うコマンドを自動で再試行しない。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
for _entry in (str(ROOT / "tests" / "support"), str(ROOT / "tools"), str(ROOT / "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from backup_restore_predicates import (  # noqa: E402
    CONTRACT,
    DRAIN_ENVIRONMENTS,
    FAILURE_KINDS,
    RESTORE_START_CONFLICTS,
    parse_json_line,
)

SEED_STAMP = "2026-09-17T00:00:00Z"
PLAN_NOW = "2026-09-17T00:00:00Z"
PLAN_EXPIRES = "2026-09-17T01:00:00Z"
POLICY_EXPIRES = "2999-01-01T00:00:00Z"
CLI_TIMEOUT_SECONDS = 180
ATTEMPT_TIMEOUT_SECONDS = 120
SEND_IDLE_TIMEOUT_SECONDS = 180
PROVIDER_ID = "codex"
MODEL_ID = "fake-model"
TARGET = "hello.py"
INSTRUCTION = "説明コメントを足してください。"

SYNTHETIC_IO = {
    "effect_ports": "in-memory counting ports; no network, process or file I/O",
    "workbench_runner": (
        "harness.infrastructure.provider.cli_runner.SubprocessCliRunner wrapped by a call counter"
    ),
    "cli_executable": (
        "tests/support/fake_cli.py behind a launch-counting wrapper; synthetic, no network"
    ),
    "boundary_probe": (
        "tests/support/workbench_fixtures.StubBoundaryProbe; replaces only the sandbox "
        "measurement, not approvals or the operation gate"
    ),
    "provider_network": "none",
    "user_database_opened": False,
}


class ObservationHalted(RuntimeError):
    """続けると観測結果が確定しない。推測で進めず止める。"""

    def __init__(self, reason: str, partial: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.partial = partial


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def qualified(value: object) -> str:
    kind = value if isinstance(value, type) else type(value)
    return f"{kind.__module__}.{kind.__qualname__}"


# ---------------------------------------------------------------------------
# 生ログと子Process
# ---------------------------------------------------------------------------

_LABEL = re.compile(r"[^a-z0-9]+")


@dataclass
class RawLogs:
    """子Processの stdout と stderr を**別File**へ、上書きせずに残す。"""

    directory: Path
    base: Path
    count: int = 0

    def write(self, label: str, stream: str, data: bytes) -> dict[str, Any]:
        self.count += 1
        name = f"{self.count:03d}-{_LABEL.sub('-', label.lower()).strip('-')}.{stream}"
        path = self.directory / name
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return {
            "path": path.relative_to(self.base).as_posix(),
            "sha256": sha256_bytes(data),
            "bytes": len(data),
        }


def _process_group_state(group: int) -> str:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return "ABSENT"
    except PermissionError:
        return "UNKNOWN"
    return "PRESENT"


def run_bounded(
    logs: RawLogs,
    label: str,
    entrypoint: str,
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    expect_json: bool = True,
) -> dict[str, Any]:
    """`list[str]` で起動し、timeout・残存Process・stdout/stderr を別々に記録する。

    stdout が空でも stderr を代わりに JSON として読まない。timeout したら
    Process Group を止め、残存を確かめられなければ `ObservationHalted`。
    """
    record: dict[str, Any] = {
        "entrypoint": entrypoint,
        "argv": list(argv),
        "cwd": str(cwd),
        "timeout_seconds": timeout,
        "started_at": utc_now(),
    }
    started = time.monotonic()
    process = subprocess.Popen(  # noqa: S603 - argv list, no shell, explicit env
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired as exc:
            record.update(timed_out=True, residual_process_group="UNKNOWN", returncode=None)
            raise ObservationHalted(f"CHILD_NOT_REAPED: {entrypoint}", record) from exc
    record.update(
        finished_at=utc_now(),
        elapsed_monotonic_seconds=round(time.monotonic() - started, 3),
        timed_out=timed_out,
        returncode=process.returncode,
        residual_process_group=_process_group_state(process.pid),
        stdout=logs.write(label, "stdout", stdout),
        stderr=logs.write(label, "stderr", stderr),
    )
    if expect_json:
        parsed, error = parse_json_line(stdout)
        record.update(stdout_json=parsed, stdout_parse_error=error)
    if timed_out:
        raise ObservationHalted(f"CHILD_TIMEOUT: {entrypoint}", record)
    if record["residual_process_group"] != "ABSENT":
        raise ObservationHalted(f"CHILD_PROCESS_GROUP_REMAINS: {entrypoint}", record)
    return record


def run_cli(
    env: SyntheticEnvironment,
    logs: RawLogs,
    label: str,
    args: list[str],
    *,
    expect_json: bool = True,
) -> dict[str, Any]:
    """`harness` CLI を測定対象Repositoryの `src` から子Processで起動する。"""
    if args[:2] == ["deploy", "drain"]:
        entrypoint = "harness deploy drain --check"
    elif args[:1] in (["backup"], ["deploy"]) and len(args) > 1:
        entrypoint = f"harness {args[0]} {args[1]}"
    else:
        entrypoint = f"harness {args[0]}"
    return run_bounded(
        logs,
        label,
        entrypoint,
        [sys.executable, "-m", "harness.presentation.cli", *args],
        cwd=env.repo,
        env={
            "PATH": os.defpath,
            "HOME": str(env.home),
            "LANG": "C.UTF-8",
            "PYTHONPATH": str(env.repo / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        timeout=CLI_TIMEOUT_SECONDS,
        expect_json=expect_json,
    )


def require_returncode(record: dict[str, Any], expected: int, label: str) -> dict[str, Any]:
    if record.get("returncode") != expected:
        raise ObservationHalted(f"PRECONDITION_CLI_FAILED: {label}", record)
    return record


# ---------------------------------------------------------------------------
# 入口の呼出し
# ---------------------------------------------------------------------------


def attempt(entrypoint: str, call: Callable[[], Any]) -> tuple[dict[str, Any], Any]:
    """呼出しの結果を記録する。例外は型とCodeを残して返し、判定は述語が行う。"""
    record: dict[str, Any] = {
        "entrypoint": entrypoint,
        "thread": threading.current_thread().name,
        "caller": "collector-thread",
        "started_at": utc_now(),
    }
    started = time.monotonic()
    try:
        value = call()
    except Exception as exc:
        code = getattr(exc, "code", None)
        record.update(
            returned=False,
            exception_type=qualified(exc),
            error_code=getattr(code, "value", None),
            detail=str(exc)[:300],
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        return record, None
    record.update(
        returned=True,
        exception_type=None,
        error_code=None,
        detail=None,
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
    return record, value


def attempt_in_worker(entrypoint: str, call: Callable[[], Any]) -> tuple[dict[str, Any], Any]:
    """別Threadから呼ぶ。入口は自分のDB接続を開くので、復元Threadとは別接続になる。"""
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="observe-entry")
    future = pool.submit(attempt, entrypoint, call)
    try:
        record, value = future.result(timeout=ATTEMPT_TIMEOUT_SECONDS)
    except FutureTimeoutError as exc:
        pool.shutdown(wait=False, cancel_futures=True)
        raise ObservationHalted(f"ATTEMPT_TIMEOUT: {entrypoint}") from exc
    pool.shutdown(wait=True)
    record["caller"] = "separate-thread"
    return record, value


def require_completed(record: dict[str, Any], value: Any) -> Any:
    if not record.get("returned"):
        raise ObservationHalted(f"PRECONDITION_STEP_FAILED: {record['entrypoint']}", record)
    return value


# ---------------------------------------------------------------------------
# 合成環境
# ---------------------------------------------------------------------------


@dataclass
class CountingPorts:
    """合成の作用Port。**外へ何も出さず、呼ばれた回数だけを数える。**"""

    calls: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def _record(self, name: str) -> None:
        with self.lock:
            self.calls.append(name)

    def count(self) -> int:
        with self.lock:
            return len(self.calls)

    def send(self, endpoint: str, payload: bytes) -> int:
        self._record("ExternalSendPort.send")
        return 0

    def launch(self, argv: list[str]) -> int:
        self._record("ProcessLaunchPort.launch")
        return 0

    def commit(self, relative_path: str, payload: bytes) -> str:
        self._record("WorkspaceWritePort.commit")
        return ""

    def reserve_budget(self, tokens: int) -> str:
        self._record("PaidExecutionPort.reserve_budget")
        return "synthetic-reservation"

    def invoke_provider(self, reservation_id: str, prompt: str) -> str:
        self._record("PaidExecutionPort.invoke_provider")
        return ""


@dataclass
class CountingRunner:
    """本物の `SubprocessCliRunner` を包み、呼出し回数を数える。"""

    inner: Any
    calls: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def stop(self) -> None:
        self.inner.stop()

    def run(
        self,
        spec: Any,
        *,
        stdin_payload: bytes,
        timeout_seconds: int,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> Any:
        with self.lock:
            self.calls += 1
        return self.inner.run(
            spec,
            stdin_payload=stdin_payload,
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
        )


class _FixedClock:
    def now(self) -> str:
        return PLAN_NOW


@dataclass
class SyntheticEnvironment:
    name: str
    root: Path
    repo: Path
    database: Path
    cas: Path
    home: Path
    plan_workspace: Path
    ports: CountingPorts = field(default_factory=CountingPorts)
    worktree: Path | None = None
    profile: Path | None = None
    launch_log: Path | None = None
    runner: CountingRunner | None = None
    services: Any = None
    plan_runs: int = 0
    closers: list[Callable[[], None]] = field(default_factory=list)
    created_new: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "root": str(self.root),
            "database": str(self.database),
            "cas": str(self.cas),
            "plan_workspace": str(self.plan_workspace),
            "worktree": None if self.worktree is None else str(self.worktree),
            "cli_profile": None if self.profile is None else str(self.profile),
            "created_new": self.created_new,
            "seed": "migrate + ArtifactStore.put + SqliteEventLedgerRepository.append (synthetic)",
        }

    def gateway(self) -> Any:
        if self.services is None or self.services.workbench is None:
            raise ObservationHalted(f"WORKBENCH_NOT_OPEN: {self.name}")
        return self.services.workbench

    def launch_count(self) -> int | None:
        if self.launch_log is None:
            return None
        return len(self.launch_log.read_text(encoding="utf-8").splitlines())

    def plan_task_args(self) -> list[str]:
        self.plan_runs += 1
        run_id = f"observe-{self.plan_runs}"
        return [
            "plan-task",
            "--database",
            str(self.database),
            "--artifact-root",
            str(self.cas),
            "--workspace",
            str(self.plan_workspace),
            "--task-path",
            "docs/task.md",
            "--declaration-path",
            "docs/declaration.json",
            "--scope",
            "docs",
            "--run-id",
            run_id,
            "--execution-plan-id",
            f"plan-{run_id}",
            "--bundle-id",
            f"bundle-{run_id}",
            "--receipt-id",
            f"receipt-{run_id}",
            "--stream-id",
            f"OBSERVE_PLAN_{self.plan_runs}",
            "--capability-id",
            "cap-backup-restore-observation",
            "--workspace-snapshot-id",
            "observation-workspace-snapshot",
            "--issued-at",
            PLAN_NOW,
            "--expires-at",
            PLAN_EXPIRES,
            "--now",
            PLAN_NOW,
            "--repo-root",
            str(self.repo),
        ]

    def close_services(self) -> None:
        if self.services is not None:
            services, self.services = self.services, None
            services.close()

    def close(self) -> None:
        self.close_services()
        while self.closers:
            self.closers.pop()()


def seed(database: Path, cas_root: Path) -> None:
    from harness.domain.artifact import ArtifactMetadata
    from harness.domain.hashing import hash_bytes
    from harness.infrastructure.artifact.filesystem_cas import FilesystemArtifactCas
    from harness.infrastructure.artifact.store import ArtifactStore
    from harness.infrastructure.sqlite.artifact_manifest_repository import (
        SqliteArtifactManifestRepository,
    )
    from harness.infrastructure.sqlite.connection_factory import ConnectionFactory
    from harness.infrastructure.sqlite.event_ledger_repository import (
        SqliteEventLedgerRepository,
    )
    from harness.infrastructure.sqlite.migrations import migrate
    from harness.ports.event_ledger import NewEvent

    factory = ConnectionFactory(database)
    migrate(factory, recorded_at=SEED_STAMP)
    connection = factory.connect()
    try:
        store = ArtifactStore(
            FilesystemArtifactCas(cas_root), SqliteArtifactManifestRepository(connection)
        )
        ledger = SqliteEventLedgerRepository(connection)
        with factory.begin_immediate(connection):
            for index, payload in enumerate((b"observation alpha\n", b"observation beta\n")):
                store.put(
                    payload,
                    ArtifactMetadata(
                        media_type="text/plain",
                        size_bytes=len(payload),
                        data_classification="INTERNAL",
                        trust_level="VERIFIED_INTERNAL",
                    ),
                    artifact_id=f"synthetic-{index}",
                    stored_at=SEED_STAMP,
                )
                ledger.append(
                    [
                        NewEvent(
                            stream_id=f"synthetic-{index}",
                            event_type=event_type,
                            payload_hash=hash_bytes(payload),
                            recorded_at=SEED_STAMP,
                        )
                        for event_type in ("RUN_CREATED", "INTENT_CREATED", "PLAN_RESOLVED")
                    ],
                    expected_stream_sequence=0,
                )
    finally:
        connection.close()


def write_plan_workspace(root: Path) -> Path:
    """`harness plan-task` の合成入力。Plan宣言は人が書く側の値を固定で持つ。"""
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "task.md").write_bytes(b"# Synthetic observation task\n\nRename the greeting.\n")
    executable = Path(sys.executable).resolve()
    declaration = {
        "planner_algorithm_version": "harness-planner/1.0",
        "planner_identity": "harness-planner/mvp0a",
        "authority_scope": "ACTION_EXECUTION",
        "intent": {
            "action_type": "LOCAL_FILE_PROPOSAL",
            "data_classification": "SYNTHETIC",
            "trust_level": "UNTRUSTED_INPUT",
            "risk_level": "LOW",
            "maximum_attempts": 1,
            "timeout_seconds": 60,
        },
        "runtime_envelope": {
            "runtime_spec_version": "1.0.0",
            "runtime_type": "MOCK_ADAPTER",
            "launcher_version": "harness-launcher/1.0",
            "executable_path": str(executable),
            "executable_sha256": sha256_file(executable),
            "argv": ["harness-mock-provider", "--offline"],
            "working_directory": "/workspace",
            "workspace_id": "observation-workspace",
            "os_boundary": "LINUX",
            "provider": "mock",
            "adapter": "mock-adapter/1.0",
            "auth_route": "NONE",
            "model": "mock-model",
        },
        "invocation": {
            "invocation_mode": "MOCK",
            "provider": "mock",
            "billing_mode": "FREE",
            "expected_effect": "NONE",
            "model": "mock-model",
        },
        "token_profile": {
            "snapshot_id": "observation-profile",
            "provider": "mock",
            "model": "mock-model",
            "tokenizer_name": "harness-utf8-byte-upper-bound",
            "tokenizer_version": "1.0.0",
            "counting_adapter_version": "harness-token-counter/2",
            "context_limit": 4096,
            "maximum_output_limit": 1024,
            "estimate_assurance": "CONSERVATIVE",
            "overheads": {
                "system_message_overhead": 3,
                "developer_message_overhead": 0,
                "tool_definition_overhead": 0,
                "per_message_overhead": 2,
                "structured_output_overhead": 0,
                "streaming_frame_overhead": 0,
                "retry_fallback_reservation": 0,
            },
            "retrieved_at": "2026-09-16T00:00:00Z",
            "expires_at": "2027-09-16T00:00:00Z",
        },
        "token_budget": {
            "policy_id": "observation-budget",
            "total_tokens": 4000,
            "reserved_output_tokens": 16,
            "reserved_tool_tokens": 0,
        },
        "actions": [
            {
                "action_type": "LOCAL_FILE_WRITE",
                "normalized_inputs": {"source": "task"},
                "normalized_outputs": {"target_relative_path": "docs/out.md"},
            }
        ],
    }
    (docs / "declaration.json").write_text(
        json.dumps(declaration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return root


def write_counting_cli(bin_dir: Path, launch_log: Path) -> Path:
    """偽CLIの前に起動記録を1行書く包み。**起動されたProcessを実数で数える。**"""
    from workbench_fixtures import write_fake_cli

    target = write_fake_cli(bin_dir, mode="OK", provider=PROVIDER_ID)
    wrapper = bin_dir / "counting-fake-cli.py"
    wrapper.write_text(
        "import json, os, runpy, sys\n"
        f"with open({str(launch_log)!r}, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'pid': os.getpid(), 'argv': sys.argv[1:]}) + '\\n')\n"
        "    handle.flush()\n"
        "    os.fsync(handle.fileno())\n"
        f"runpy.run_path({str(target)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    return wrapper


def create_environment(
    parent: Path, name: str, repo: Path, *, workbench: bool
) -> SyntheticEnvironment:
    root = parent / name
    root.mkdir(parents=True, exist_ok=False)
    database = root / "state" / "state.sqlite3"
    cas = root / "cas"
    database.parent.mkdir()
    created_new = not database.exists() and not cas.exists()
    seed(database, cas)
    home = root / "home"
    home.mkdir()
    env = SyntheticEnvironment(
        name=name,
        root=root,
        repo=repo,
        database=database,
        cas=cas,
        home=home,
        plan_workspace=write_plan_workspace(root / "plan-workspace"),
        created_new=created_new,
    )
    if workbench:
        open_workbench(env)
    return env


def open_workbench(env: SyntheticEnvironment) -> None:
    """`harness ui` と同じ `build_services()` で Workbench を組む。"""
    from workbench_fixtures import (
        StubBoundaryProbe,
        build_demo_worktree,
        fake_argv_prefix,
        write_runtime_profile,
    )

    from harness.infrastructure.provider.cli_runner import SubprocessCliRunner
    from harness.presentation.local_ui.composition import WorkbenchSetup, build_services

    if env.worktree is None:
        env.worktree = build_demo_worktree(env.root / "demo")
        state = env.root / "cli-state"
        # 中立作業Directoryは空であることを本番が要求する。
        # 起動記録は TMPDIR（境界の内側で書込み可）へ置く。
        (state / "tmp").mkdir(parents=True)
        env.launch_log = state / "tmp" / "fake-cli-launches.jsonl"
        env.launch_log.touch()
        wrapper = write_counting_cli(env.root / "cli-bin", env.launch_log)
        env.profile = write_runtime_profile(
            env.root / "cli-profile.json",
            executables={PROVIDER_ID: fake_argv_prefix(wrapper)},
            home=env.home,
            neutral_workdir=state / "cwd",
            state_dir=state,
            cli_runtime_root=env.root / "cli-bin",
        )
        env.runner = CountingRunner(SubprocessCliRunner())
    assert env.profile is not None  # noqa: S101 - set together with worktree above
    env.services = build_services(
        repo_root=env.repo,
        database_path=env.database,
        artifact_root=env.cas,
        workbench=WorkbenchSetup(
            workspace=env.worktree,
            runtime_profile=env.profile,
            workspace_label="backup-restore-observation",
            auth_session="local-uid:" + str(os.getuid()),
            runner=env.runner,
            boundary_probe=StubBoundaryProbe(),
        ),
    )


def build_effect_entry(env: SyntheticEnvironment, runtime: Any, ports: CountingPorts) -> Any:
    """通常の作用入口。**Portは合成、受付Gateは同一DBの本番Gate。**"""
    from harness.application.effect_orchestrator import EffectOrchestrator
    from harness.infrastructure.sqlite.connection_factory import ConnectionFactory
    from harness.infrastructure.sqlite.event_ledger_repository import (
        SqliteEventLedgerRepository,
    )

    connection = ConnectionFactory(env.database).connect(cross_thread=True)
    env.closers.append(connection.close)
    return EffectOrchestrator(
        clock=_FixedClock(),
        ledger=SqliteEventLedgerRepository(connection),
        external_send=ports,
        process_launch=ports,
        workspace_write=ports,
        paid_execution=ports,
        operation_gate=runtime.intake_gate(),
    )


def effect_request(kind: Any, phase: str) -> Any:
    from harness.application.effect_orchestrator import EffectRequest

    return EffectRequest(
        attempt_id=f"observe-{phase}-{kind.value.lower()}",
        stream_id=f"observe-{phase}",
        effect_kind=kind,
        policy_expires_at=POLICY_EXPIRES,
    )


def create_call(env: SyntheticEnvironment) -> Callable[[], Any]:
    gateway = env.gateway()
    return lambda: gateway.create_session(
        provider_id=PROVIDER_ID, model_id=MODEL_ID, relative_path=TARGET, instruction=INSTRUCTION
    )


# ---------------------------------------------------------------------------
# 状態の読取り
# ---------------------------------------------------------------------------


def cas_inventory(root: Path) -> dict[str, Any]:
    objects = root / "objects"
    files = (
        sorted(path for path in objects.rglob("*") if path.is_file()) if objects.is_dir() else []
    )
    rows = [[path.relative_to(root).as_posix(), sha256_file(path)] for path in files]
    return {
        "cas_object_files": len(rows),
        "cas_tree_sha256": sha256_bytes(json.dumps(rows).encode("utf-8")),
    }


def snapshot(env: SyntheticEnvironment) -> dict[str, Any]:
    """同一DBを別接続で読む。**数えずに 0 を書かない。** 表が無ければ例外で止まる。"""
    from harness.infrastructure.sqlite.connection_factory import ConnectionFactory

    connection = ConnectionFactory(env.database).connect()
    try:

        def grouped(query: str) -> dict[str, int]:
            return {str(row[0]): int(row[1]) for row in connection.execute(query)}

        control = connection.execute(
            "SELECT mode, version, refusals FROM operation_control WHERE singleton=1"
        ).fetchone()
        if control is None:
            raise ObservationHalted(f"OPERATION_CONTROL_ROW_MISSING: {env.database}")
        ledger = [
            [str(row[0]), int(row[1]), str(row[2]), row[3]]
            for row in connection.execute(
                "SELECT stream_id, sequence_number, event_hash, previous_event_hash"
                " FROM event_ledger ORDER BY stream_id, sequence_number"
            )
        ]
        manifest = [
            [str(row[0]), str(row[1]), int(row[2]), int(row[3])]
            for row in connection.execute(
                "SELECT artifact_id, content_hash, size_bytes, payload_deleted"
                " FROM artifact_manifest ORDER BY artifact_id"
            )
        ]
        result: dict[str, Any] = {
            "observed_at": utc_now(),
            "database": str(env.database),
            "operation_control": {
                "mode": str(control[0]),
                "version": int(control[1]),
                "refusals": int(control[2]),
            },
            "operation_admission_kinds": grouped(
                "SELECT kind, COUNT(*) FROM operation_admission GROUP BY kind ORDER BY kind"
            ),
            "operation_journal_states": grouped(
                "SELECT state, COUNT(*) FROM operation_journal GROUP BY state ORDER BY state"
            ),
            "cli_invocation_journal_states": grouped(
                "SELECT state, COUNT(*) FROM cli_invocation_journal GROUP BY state ORDER BY state"
            ),
            "approval_grant_statuses": grouped(
                "SELECT status, COUNT(*) FROM approval_grant GROUP BY status ORDER BY status"
            ),
            "workbench_session_states": grouped(
                "SELECT state, COUNT(*) FROM workbench_session GROUP BY state ORDER BY state"
            ),
            "event_ledger_rows": len(ledger),
            "event_ledger_sha256": sha256_bytes(json.dumps(ledger).encode("utf-8")),
            "artifact_manifest_rows": len(manifest),
            "artifact_manifest_sha256": sha256_bytes(json.dumps(manifest).encode("utf-8")),
        }
    finally:
        connection.close()
    result.update(cas_inventory(env.cas))
    result["effect_port_calls"] = env.ports.count()
    result["runner_calls"] = None if env.runner is None else env.runner.calls
    result["fake_cli_launches"] = env.launch_count()
    result["worktree_target_sha256"] = (
        None if env.worktree is None else sha256_file(env.worktree / TARGET)
    )
    return result


def target_state(database: Path, cas: Path) -> dict[str, Any]:
    return {
        "database": str(database),
        "cas": str(cas),
        "database_exists": database.exists(),
        "cas_exists": cas.exists(),
        "cas_object_files": cas_inventory(cas)["cas_object_files"] if cas.exists() else 0,
    }


def reread(database: Path, cas: Path) -> dict[str, Any]:
    """Chainを端から検証し、全Artifactを読み直してHashを取る（既存の読直し器を使う）。"""
    from harness.infrastructure.sqlite.connection_factory import ConnectionFactory
    from measure_backup_restore import inspect_restored

    try:
        view = inspect_restored(database, cas)
        connection = ConnectionFactory(database).connect()
        try:
            rows = [
                list(row)
                for row in connection.execute(
                    "SELECT stream_id, sequence_number, event_type, payload_hash, recorded_at,"
                    " previous_event_hash, event_hash FROM event_ledger"
                    " ORDER BY stream_id, sequence_number"
                )
            ]
        finally:
            connection.close()
    except Exception as exc:
        # 読めなかった事実を残す。述語がFAILにする。
        return {"error": f"{qualified(exc)}: {exc}"[:400]}
    view["ledger_rows_sha256"] = sha256_bytes(json.dumps(rows).encode("utf-8"))
    return dict(view)


@dataclass(frozen=True)
class BackupPaths:
    backup_database: Path
    backup_cas: Path
    restore_database: Path
    restore_cas: Path


def backup_paths(env: SyntheticEnvironment) -> BackupPaths:
    """Backup先の親だけを作る。復元先は `SqliteRestoreTarget` が新規であることを確かめる。"""
    (env.root / "backup").mkdir(exist_ok=False)
    return BackupPaths(
        backup_database=env.root / "backup" / "backup.sqlite3",
        backup_cas=env.root / "backup" / "cas",
        restore_database=env.root / "restored" / "restored.sqlite3",
        restore_cas=env.root / "restored" / "cas",
    )


def backup_create_args(env: SyntheticEnvironment, paths: BackupPaths, backup_id: str) -> list[str]:
    return [
        "backup",
        "create",
        "--database",
        str(env.database),
        "--cas-root",
        str(env.cas),
        "--backup-database",
        str(paths.backup_database),
        "--backup-cas-root",
        str(paths.backup_cas),
        "--backup-id",
        backup_id,
        "--recorded-at",
        SEED_STAMP,
    ]


def backup_verify_args(env: SyntheticEnvironment, paths: BackupPaths, backup_id: str) -> list[str]:
    return [
        "backup",
        "verify",
        backup_id,
        "--backup-database",
        str(paths.backup_database),
        "--backup-cas-root",
        str(paths.backup_cas),
        "--restore-database",
        str(paths.restore_database),
        "--restore-cas-root",
        str(paths.restore_cas),
        "--source-database",
        str(env.database),
        "--source-cas-root",
        str(env.cas),
    ]


def drain_args(env: SyntheticEnvironment, deployment_id: str) -> list[str]:
    return [
        "deploy",
        "drain",
        "--check",
        "--database",
        str(env.database),
        "--deployment-id",
        deployment_id,
    ]


# ---------------------------------------------------------------------------
# 前提状態（本番の入口・Repositoryで作る）
# ---------------------------------------------------------------------------


def prepare_pending_send_approval(env: SyntheticEnvironment) -> dict[str, Any]:
    gateway = env.gateway()
    created_record, created = attempt("WorkbenchGateway.create_session", create_call(env))
    require_completed(created_record, created)
    approved_record, approved = attempt(
        "WorkbenchGateway.approve_send",
        lambda: gateway.approve_send(
            created["session_id"], execution_plan_hash=created["execution_plan_hash"]
        ),
    )
    require_completed(approved_record, approved)
    return {
        "origin": "WorkbenchGateway.create_session + approve_send (grant stays ISSUED)",
        "attempts": [created_record, approved_record],
        "session_id": created["session_id"],
    }


def prepare_claimed_send(env: SyntheticEnvironment) -> dict[str, Any]:
    """送信を claim し、dispatch 前に止める。**claim 後に Process を失った状態を作る。**"""
    from workbench_fixtures import StubBoundaryProbe

    from harness.infrastructure.filesystem.local_workflow_workspace import LocalWorktree
    from harness.infrastructure.filesystem.workspace_boundary import FilesystemPolicy
    from harness.infrastructure.local_workflow_runtime import current_runtime_identity
    from harness.infrastructure.sqlite.connection_factory import ConnectionFactory
    from harness.infrastructure.workbench_runtime import build_workbench_service
    from harness.presentation.local_ui.composition import SystemClock, UuidSource

    env.close_services()
    assert env.worktree is not None and env.profile is not None  # noqa: S101 - workbench env
    worktree = LocalWorktree(env.worktree, FilesystemPolicy.load(env.repo))
    composition = build_workbench_service(
        repo_root=env.repo,
        database_path=env.database,
        artifact_root=env.cas,
        workspace=env.worktree,
        runtime_profile=env.profile,
        workspace_label="backup-restore-observation",
        clock=SystemClock(),
        ids=UuidSource(),
        runtime_identity=current_runtime_identity(env.repo),
        factory=ConnectionFactory(env.database),
        worktree=worktree,
        runner=env.runner,
        boundary_probe=StubBoundaryProbe(),
    )
    try:
        service = composition.service
        auth = "local-uid:" + str(os.getuid())
        created_record, created = attempt(
            "WorkbenchService.create_send_plan",
            lambda: service.create_send_plan(
                provider_id=PROVIDER_ID,
                model_id=MODEL_ID,
                relative_path=TARGET,
                instruction=INSTRUCTION,
            ),
        )
        require_completed(created_record, created)
        approved_record, approved = attempt(
            "WorkbenchService.approve_send",
            lambda: service.approve_send(
                created["session_id"],
                execution_plan_hash=created["execution_plan_hash"],
                auth_session=auth,
            ),
        )
        require_completed(approved_record, approved)
        claimed_record, claimed = attempt(
            "WorkbenchService.claim_send", lambda: service.claim_send(created["session_id"])
        )
        require_completed(claimed_record, claimed)
    finally:
        composition.close()
        worktree.close()
    open_workbench(env)
    return {
        "origin": (
            "build_workbench_service() WorkbenchService.create_send_plan + approve_send + "
            "claim_send; dispatch_send not called (process loss after a durable claim)"
        ),
        "attempts": [created_record, approved_record, claimed_record],
        "session_id": created["session_id"],
        "dispatch_send_called": False,
    }


def prepare_operation_journal(env: SyntheticEnvironment) -> dict[str, Any]:
    """本番Repositoryで `PREPARED_DURABLE` を1件作る。**作用は起こしていない。**"""
    from harness.domain.effect import OperationJournal
    from harness.domain.hashing import ContentHash
    from harness.infrastructure.sqlite.connection_factory import ConnectionFactory
    from harness.infrastructure.sqlite.effect_journal_repository import (
        SqliteEffectJournalRepository,
    )

    journal = OperationJournal(
        operation_journal_id="observe-journal",
        operation_id="observe-operation",
        effect_id="observe-effect",
        run_id="observe-run",
        action_id="observe-action",
        attempt_id="observe-attempt",
        operation_type="LOCAL_FILE_COMMIT",
        target_resource_identity="synthetic://observation/target",
        fencing_token=1,
        before_hash=ContentHash.parse("sha256:" + "0" * 64),
        expected_after_hash=ContentHash.parse("sha256:" + "1" * 64),
        prepared_event_id="observe-run:1",
        prepared_at=SEED_STAMP,
        durability_level="STORAGE_SYNC",
    )
    factory = ConnectionFactory(env.database)
    connection = factory.connect()
    try:
        with factory.begin_immediate(connection):
            SqliteEffectJournalRepository(connection).create_prepared(journal)
    finally:
        connection.close()
    return {
        "origin": (
            "SqliteEffectJournalRepository.create_prepared on the synthetic database; "
            "a durable PREPARED_DURABLE row with no effect executed"
        ),
        "operation_journal_id": journal.operation_journal_id,
    }


@contextmanager
def held_reservation(env: SyntheticEnvironment) -> Iterator[dict[str, Any]]:
    from harness.infrastructure.runtime_facade import HarnessRuntimeService

    gate = HarnessRuntimeService(env.database).intake_gate()
    with gate.admission("backup-restore-observation-held", kind="intake"):
        yield {
            "origin": (
                "HarnessRuntimeService.intake_gate().admission(kind='intake') "
                "held open by the collector process"
            ),
            "kind": "intake",
        }


# ---------------------------------------------------------------------------
# 観測シナリオ
# ---------------------------------------------------------------------------


def observe_drain(record: dict[str, Any], *, repo: Path, workspace: Path, logs: RawLogs) -> None:
    for kind in DRAIN_ENVIRONMENTS:
        entry: dict[str, Any] = {}
        record[kind] = entry
        env = create_environment(
            workspace / "drain", kind.lower().replace("_", "-"), repo, workbench=True
        )
        try:
            entry["environment"] = env.describe()
            _observe_drain_environment(entry, env, kind, logs)
        finally:
            env.close()


def _observe_drain_environment(
    entry: dict[str, Any], env: SyntheticEnvironment, kind: str, logs: RawLogs
) -> None:
    tag = f"drain-{kind}"
    if kind == "QUIESCENT":
        control: dict[str, Any] = {}
        entry["open_control"] = control
        control["snapshot_before"] = snapshot(env)
        control["plan_task"] = run_cli(env, logs, f"{tag}-open-plan-task", env.plan_task_args())
        control["workbench_create_session"], _ = attempt(
            "WorkbenchGateway.create_session", create_call(env)
        )
        control["snapshot_after"] = snapshot(env)
    elif kind == "ACTIVE_OPERATION_JOURNAL":
        entry["precondition"] = prepare_operation_journal(env)
    elif kind == "ACTIVE_CLI_INVOCATION":
        entry["precondition"] = prepare_claimed_send(env)
    elif kind == "PENDING_APPROVAL":
        entry["precondition"] = prepare_pending_send_approval(env)

    if kind == "ADMISSION_RESERVED":
        with held_reservation(env) as held:
            entry["precondition"] = held
            entry["snapshot_before_drain"] = snapshot(env)
            entry["drain"] = run_cli(env, logs, f"{tag}-drain", drain_args(env, f"dep-{kind}"))
            entry["snapshot_after_drain"] = snapshot(env)
        entry["snapshot_after_release"] = snapshot(env)
    else:
        entry["snapshot_before_drain"] = snapshot(env)
        entry["drain"] = run_cli(env, logs, f"{tag}-drain", drain_args(env, f"dep-{kind}"))
        entry["snapshot_after_drain"] = snapshot(env)

    probes: dict[str, Any] = {}
    entry["probes_after_drain"] = probes
    probes["plan_task"] = run_cli(env, logs, f"{tag}-probe-plan-task", env.plan_task_args())
    probes["workbench_create_session"], _ = attempt(
        "WorkbenchGateway.create_session", create_call(env)
    )
    entry["snapshot_after_probes"] = snapshot(env)


def observe_restore_window(
    record: dict[str, Any], *, repo: Path, workspace: Path, logs: RawLogs
) -> None:
    from harness.domain.policy_freshness import EffectKind
    from harness.infrastructure.runtime_facade import HarnessRuntimeService

    env = create_environment(workspace / "restore-window", "source", repo, workbench=True)
    try:
        record["environment"] = env.describe()
        runtime = HarnessRuntimeService(env.database)
        effect = build_effect_entry(env, runtime, env.ports)
        gateway = env.gateway()
        record["effect_kinds_declared"] = [kind.value for kind in EffectKind]

        # --- OPEN の対照: 同じ種類の入口が合成処理まで届く --------------------
        controls: dict[str, Any] = {}
        record["controls_open"] = controls
        controls["snapshot_before"] = snapshot(env)
        effects: list[dict[str, Any]] = []
        controls["effects"] = effects
        for kind in EffectKind:
            before = env.ports.count()
            attempt_record, _ = attempt(
                f"EffectOrchestrator.execute[{kind.value}]",
                lambda kind=kind: effect.execute(effect_request(kind, "open")),
            )
            effects.append(
                {
                    "effect_kind": kind.value,
                    "attempt": attempt_record,
                    "port_calls_before": before,
                    "port_calls_after": env.ports.count(),
                }
            )
        sessions_before = _sessions(env)
        create_record, created = attempt("WorkbenchGateway.create_session", create_call(env))
        controls["workbench_create_session"] = {
            "attempt": create_record,
            "sessions_before": sessions_before,
            "sessions_after": _sessions(env),
        }
        require_completed(create_record, created)
        send: dict[str, Any] = {}
        controls["workbench_send"] = send
        proposal = _run_to_proposal(env, created, send)
        apply: dict[str, Any] = {}
        controls["workbench_apply"] = apply
        apply["target_sha256_before"] = _target_hash(env)
        approve_record, approved = attempt(
            "WorkbenchGateway.approve_apply",
            lambda: gateway.approve_apply(
                proposal["session_id"],
                apply_execution_plan_hash=proposal["apply_execution_plan_hash"],
                proposal_hash=proposal["proposal_hash"],
            ),
        )
        apply["approve_apply"] = approve_record
        require_completed(approve_record, approved)
        apply_record, applied = attempt(
            "WorkbenchGateway.apply",
            lambda: gateway.apply(
                proposal["session_id"],
                apply_execution_plan_hash=approved["apply_execution_plan_hash"],
            ),
        )
        apply["apply"] = apply_record
        require_completed(apply_record, applied)
        apply["target_sha256_after"] = _target_hash(env)
        apply["session_state"] = gateway.session(proposal["session_id"])["state"]
        controls["plan_task"] = run_cli(env, logs, "window-open-plan-task", env.plan_task_args())
        controls["snapshot_after"] = snapshot(env)

        # --- 区間内で使う対象（未決の承認を残さない） --------------------------
        prepared: dict[str, Any] = {}
        record["prepared_for_window"] = prepared
        drafted_record, drafted = attempt("WorkbenchGateway.create_session", create_call(env))
        prepared["drafted_session"] = drafted_record
        require_completed(drafted_record, drafted)
        second_record, second = attempt("WorkbenchGateway.create_session", create_call(env))
        prepared["proposal_session_create"] = second_record
        require_completed(second_record, second)
        second_send: dict[str, Any] = {}
        prepared["proposal_session_send"] = second_send
        second_proposal = _run_to_proposal(env, second, second_send)
        prepared["snapshot"] = snapshot(env)

        # --- 本物の復元区間 ---------------------------------------------------
        paths = backup_paths(env)
        backup = runtime.create_backup(
            source_cas=env.cas,
            destination_database=paths.backup_database,
            destination_cas=paths.backup_cas,
            backup_id="bkp-window",
            created_at=SEED_STAMP,
        )
        record["backup"] = {
            "entrypoint": "HarnessRuntimeService.create_backup",
            "backup_id": backup.backup_id,
            "database_sha256": backup.database_sha256,
            "artifact_ids": list(backup.artifact_ids),
        }
        record["restore_target_before"] = target_state(paths.restore_database, paths.restore_cas)
        service = runtime.backup_restore_service(
            restore_database=paths.restore_database, restore_cas=paths.restore_cas
        )
        record["restore_service_entrypoint"] = "HarnessRuntimeService.backup_restore_service"
        record["restore_target_class"] = qualified(vars(service)["_target"])
        record["restore_admission_class"] = qualified(vars(service)["_intake"])
        checkpoint: dict[str, Any] = {"reached": False}
        record["checkpoint"] = checkpoint

        def during_restore(_guard: object) -> None:
            checkpoint["reached"] = True
            checkpoint["reached_at"] = utc_now()
            checkpoint["restore_database_exists"] = paths.restore_database.exists()
            checkpoint["restore_cas_exists"] = paths.restore_cas.exists()
            checkpoint["snapshot_before_attempts"] = snapshot(env)
            attempts: list[dict[str, Any]] = []
            checkpoint["attempts"] = attempts
            for kind in EffectKind:
                attempts.append(
                    attempt_in_worker(
                        f"EffectOrchestrator.execute[{kind.value}]",
                        lambda kind=kind: effect.execute(effect_request(kind, "window")),
                    )[0]
                )
            attempts.append(
                attempt_in_worker("WorkbenchGateway.create_session", create_call(env))[0]
            )
            attempts.append(
                attempt_in_worker(
                    "WorkbenchGateway.approve_send",
                    lambda: gateway.approve_send(
                        drafted["session_id"], execution_plan_hash=drafted["execution_plan_hash"]
                    ),
                )[0]
            )
            attempts.append(
                attempt_in_worker(
                    "WorkbenchGateway.approve_apply",
                    lambda: gateway.approve_apply(
                        second_proposal["session_id"],
                        apply_execution_plan_hash=second_proposal["apply_execution_plan_hash"],
                        proposal_hash=second_proposal["proposal_hash"],
                    ),
                )[0]
            )
            attempts.append(
                attempt_in_worker(
                    "WorkbenchGateway.apply",
                    lambda: gateway.apply(
                        second_proposal["session_id"],
                        apply_execution_plan_hash=second_proposal["apply_execution_plan_hash"],
                    ),
                )[0]
            )
            checkpoint["plan_task"] = run_cli(env, logs, "window-plan-task", env.plan_task_args())
            checkpoint["snapshot_after_attempts"] = snapshot(env)

        outcome = service.restore_and_verify(
            backup=backup, backup_restore_id="restore-window", during_restore=during_restore
        )
        record["restore_outcome"] = {
            "state": outcome.result.state,
            "error_code": None
            if outcome.result.error_code is None
            else outcome.result.error_code.value,
            "problems": list(outcome.result.problems),
            "service_effects_during_restore": outcome.effects_during_restore,
            "service_effects_meaning": "refused admission attempts counted by the service",
            "guard_attempt_ports": [item.port for item in outcome.guard_attempts],
        }
        record["snapshot_after_restore"] = snapshot(env)
    finally:
        env.close()


def _sessions(env: SyntheticEnvironment) -> int:
    return sum(snapshot(env)["workbench_session_states"].values())


def _target_hash(env: SyntheticEnvironment) -> str:
    assert env.worktree is not None  # noqa: S101 - workbench env
    return sha256_file(env.worktree / TARGET)


def _run_to_proposal(
    env: SyntheticEnvironment, created: dict[str, Any], record: dict[str, Any]
) -> dict[str, Any]:
    gateway = env.gateway()
    assert env.runner is not None  # noqa: S101 - workbench env
    record["runner_calls_before"] = env.runner.calls
    record["launches_before"] = env.launch_count()
    approve_record, approved = attempt(
        "WorkbenchGateway.approve_send",
        lambda: gateway.approve_send(
            created["session_id"], execution_plan_hash=created["execution_plan_hash"]
        ),
    )
    record["approve_send"] = approve_record
    require_completed(approve_record, approved)
    start_record, started = attempt(
        "WorkbenchGateway.start_send",
        lambda: gateway.start_send(
            approved["session_id"], execution_plan_hash=approved["execution_plan_hash"]
        ),
    )
    record["start_send"] = start_record
    require_completed(start_record, started)
    record["idle"] = gateway.wait_for_idle(SEND_IDLE_TIMEOUT_SECONDS)
    if not record["idle"]:
        raise ObservationHalted("SEND_DID_NOT_FINISH", record)
    proposal: dict[str, Any] = gateway.session(created["session_id"])
    record["session_state"] = proposal["state"]
    record["runner_calls_after"] = env.runner.calls
    record["launches_after"] = env.launch_count()
    if proposal["state"] != "PROPOSAL_READY":
        raise ObservationHalted(f"PROPOSAL_NOT_READY: {proposal['state']}", record)
    return proposal


def observe_restore_start_conflicts(
    record: dict[str, Any], *, repo: Path, workspace: Path, logs: RawLogs
) -> None:
    for kind in RESTORE_START_CONFLICTS:
        entry: dict[str, Any] = {}
        record[kind] = entry
        env = create_environment(
            workspace / "restore-start",
            kind.lower().replace("_", "-"),
            repo,
            workbench=kind == "PENDING_APPROVAL",
        )
        try:
            entry["environment"] = env.describe()
            paths = backup_paths(env)
            tag = f"start-{kind}"
            if kind == "PENDING_APPROVAL":
                entry["precondition"] = prepare_pending_send_approval(env)
                env.close_services()
                _verify_against_conflict(entry, env, paths, logs, tag)
            else:
                with held_reservation(env) as held:
                    entry["precondition"] = held
                    _verify_against_conflict(entry, env, paths, logs, tag)
        finally:
            env.close()


def _verify_against_conflict(
    entry: dict[str, Any],
    env: SyntheticEnvironment,
    paths: BackupPaths,
    logs: RawLogs,
    tag: str,
) -> None:
    entry["backup_create"] = require_returncode(
        run_cli(env, logs, f"{tag}-backup-create", backup_create_args(env, paths, "bkp-start")),
        0,
        f"{tag} backup create",
    )
    entry["snapshot_before_verify"] = snapshot(env)
    entry["restore_target_before"] = target_state(paths.restore_database, paths.restore_cas)
    entry["backup_verify"] = run_cli(
        env, logs, f"{tag}-backup-verify", backup_verify_args(env, paths, "bkp-start")
    )
    entry["restore_target_after"] = target_state(paths.restore_database, paths.restore_cas)
    entry["snapshot_after_verify"] = snapshot(env)


def observe_failure_retention(
    record: dict[str, Any], *, repo: Path, workspace: Path, logs: RawLogs
) -> None:
    from harness.domain.policy_freshness import EffectKind
    from harness.infrastructure.runtime_facade import HarnessRuntimeService

    for kind in FAILURE_KINDS:
        entry: dict[str, Any] = {}
        record[kind] = entry
        env = create_environment(
            workspace / "failure", kind.lower().replace("_", "-"), repo, workbench=True
        )
        try:
            entry["environment"] = env.describe()
            env.close_services()
            paths = backup_paths(env)
            tag = f"failure-{kind}"
            entry["backup_create"] = require_returncode(
                run_cli(env, logs, f"{tag}-backup-create", backup_create_args(env, paths, "bkp")),
                0,
                f"{tag} backup create",
            )
            entry["source_cas_object_files"] = cas_inventory(env.cas)["cas_object_files"]
            if kind == "COPY_FAILURE":
                objects = sorted(
                    p for p in (paths.backup_cas / "objects").rglob("*") if p.is_file()
                )
                if not objects:
                    raise ObservationHalted("BACKUP_HAS_NO_CAS_OBJECT", entry)
                victim = objects[-1]
                entry["damage"] = {
                    "removed_object": victim.relative_to(env.root).as_posix(),
                    "sha256": sha256_file(victim),
                    "existed_before": victim.is_file(),
                }
                victim.unlink()
                entry["damage"]["exists_after"] = victim.exists()
            else:
                entry["post_backup_change"] = require_returncode(
                    run_cli(env, logs, f"{tag}-post-backup-plan-task", env.plan_task_args()),
                    0,
                    f"{tag} post-backup change",
                )
            entry["snapshot_before_verify"] = snapshot(env)
            entry["backup_verify"] = run_cli(
                env, logs, f"{tag}-backup-verify", backup_verify_args(env, paths, "bkp")
            )
            entry["restore_target_after"] = target_state(paths.restore_database, paths.restore_cas)
            entry["snapshot_after_verify"] = snapshot(env)

            separate: dict[str, Any] = {}
            entry["separate_runtime"] = separate
            separate["plan_task"] = run_cli(
                env, logs, f"{tag}-separate-plan-task", env.plan_task_args()
            )
            fresh = HarnessRuntimeService(env.database)
            ports = CountingPorts()
            fresh_effect = build_effect_entry(env, fresh, ports)
            before = ports.count()
            attempt_record, _ = attempt(
                "EffectOrchestrator.execute[EXTERNAL_SEND]",
                lambda entry=fresh_effect: entry.execute(
                    effect_request(EffectKind.EXTERNAL_SEND, "after")
                ),
            )
            separate["effect"] = {
                "attempt": attempt_record,
                "port_calls_before": before,
                "port_calls_after": ports.count(),
                "runtime": "new HarnessRuntimeService and EffectOrchestrator after the failure",
            }
            open_workbench(env)
            separate["workbench_create_session"], _ = attempt(
                "WorkbenchGateway.create_session", create_call(env)
            )
            separate["workbench_runtime"] = "new build_services() composition after the failure"
            separate["snapshot_after"] = snapshot(env)
        finally:
            env.close()


def observe_operator_flow(
    record: dict[str, Any], *, repo: Path, workspace: Path, logs: RawLogs
) -> None:
    env = create_environment(workspace / "operator-flow", "source", repo, workbench=False)
    try:
        record["environment"] = env.describe()
        paths = backup_paths(env)
        record["backup_create"] = run_cli(
            env, logs, "operator-backup-create", backup_create_args(env, paths, "bkp-operator")
        )
        record["backup_verify"] = run_cli(
            env, logs, "operator-backup-verify", backup_verify_args(env, paths, "bkp-operator")
        )
        record["snapshot_after_verify"] = snapshot(env)
        record["drain"] = run_cli(env, logs, "operator-drain", drain_args(env, "dep-operator"))
        record["snapshot_after_drain"] = snapshot(env)
        record["content_comparison"] = {
            "source": reread(env.database, env.cas),
            "backup": reread(paths.backup_database, paths.backup_cas),
            "restored": reread(paths.restore_database, paths.restore_cas),
        }
        record["intake_after_drain"] = run_cli(
            env, logs, "operator-intake-after-drain", env.plan_task_args()
        )
        help_call = run_cli(
            env, logs, "operator-deploy-help", ["deploy", "--help"], expect_json=False
        )
        stdout_text = (logs.base / help_call["stdout"]["path"]).read_text(encoding="utf-8")
        match = re.search(r"\{([A-Za-z0-9_,-]+)\}", stdout_text)
        drained = record["drain"].get("stdout_json") or {}
        record["resume"] = {
            "decision": {
                "source": "harness deploy drain --check",
                "state": drained.get("state"),
                "meaning": "DEPLOY_VERDICT_NOT_RESUME",
            },
            "execution": "NOT_PERFORMED",
            "execution_reason": (
                "CLI/UIに受付再開の入口が無い。IntakeGate.resume()はApplication部品であり、"
                "照合付きのOperator再開は後続Task"
            ),
            "deploy_help": help_call,
            "deploy_subcommands_observed": [] if match is None else match.group(1).split(","),
        }
        resume = record["resume"]
        if "resume" in resume["deploy_subcommands_observed"]:
            options = [
                "--database",
                str(env.database),
                "--artifact-root",
                str(env.cas),
                "--repo-root",
                str(repo),
            ]
            resume["inspection"] = run_cli(
                env, logs, "operator-inspect", ["deploy", "inspect", *options]
            )
            review = resume["inspection"].get("stdout_json") or {}
            command = [
                "deploy",
                "resume",
                *options,
                "--review-hash",
                review.get("review_hash", "missing"),
                "--auth-session",
                f"local-uid:{os.getuid()}",
                "--reason",
                "maintenance-complete",
            ]
            resume["call"] = run_cli(env, logs, "operator-resume", command)
            resume["snapshot_after"] = snapshot(env)
            resume["audit_after"] = reread(env.database, env.cas)
            body = resume["call"].get("stdout_json") or {}
            resume["verify_ledger"] = run_cli(
                env,
                logs,
                "operator-resume-ledger",
                [
                    "verify-ledger",
                    "--database",
                    str(env.database),
                    "--stream-id",
                    body.get("operation_id", "missing"),
                ],
            )
            resume["replay"] = run_cli(env, logs, "operator-resume-replay", command)
            resume["intake_after"] = run_cli(
                env, logs, "operator-intake-after-resume", env.plan_task_args()
            )
            resume["execution"] = "PERFORMED"
            resume["execution_reason"] = (
                "Explicit local OS approval against the inspected state hash; see raw CLI results."
            )
    finally:
        env.close()


SCENARIOS: tuple[tuple[str, Callable[..., None]], ...] = (
    ("drain", observe_drain),
    ("restore_window", observe_restore_window),
    ("restore_start_conflicts", observe_restore_start_conflicts),
    ("failure_retention", observe_failure_retention),
    ("operator_flow", observe_operator_flow),
)


def implementation_modules() -> dict[str, str]:
    import harness.application.backup_restore_service as service_module
    import harness.application.intake_gate as gate_module
    import harness.infrastructure.runtime_facade as facade_module
    import harness.infrastructure.sqlite.operations_repository as operations_module
    import harness.presentation.local_ui.workbench_gateway as gateway_module

    modules = (service_module, gate_module, facade_module, operations_module, gateway_module)
    return {module.__name__: str(Path(str(module.__file__)).resolve()) for module in modules}


def observe_all(
    *,
    repo: Path,
    workspace: Path,
    logs: RawLogs,
    scenarios: tuple[tuple[str, Callable[..., None]], ...] = SCENARIOS,
) -> dict[str, Any]:
    workspace.mkdir(parents=True, exist_ok=False)
    observations: dict[str, Any] = {
        "contract": CONTRACT,
        "repo": str(repo),
        "workspace": str(workspace),
        "interpreter": sys.executable,
        "python_version": platform.python_version(),
        "child_pythonpath": str(repo / "src"),
        "implementation_modules": implementation_modules(),
        "synthetic_io": SYNTHETIC_IO,
        "started_at": utc_now(),
    }
    for name, function in scenarios:
        record: dict[str, Any] = {}
        observations[name] = record
        try:
            function(record, repo=repo, workspace=workspace, logs=logs)
        except ObservationHalted as exc:
            observations["halted"] = {"scenario": name, "reason": str(exc), "partial": exc.partial}
            break
    observations["finished_at"] = utc_now()
    return observations
