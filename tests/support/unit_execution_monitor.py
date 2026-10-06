"""Observe a Python test-call boundary; this is not an OS sandbox.

Audit/profile hooks count attempted effects before allowing them to execute.
Unknown native execution and hook replacement invalidate the observation. No
arguments, paths, credentials or payloads are copied into the report.
"""

from __future__ import annotations

import io
import os
import socket
import sys
import threading
from pathlib import Path
from typing import Any

from harness.infrastructure.filesystem.workspace_writer import WorkspaceWriter
from harness.infrastructure.sqlite.effect_journal_repository import SqliteEffectJournalRepository

_ACTIVE: UnitExecutionMonitor | None = None
_INSTALLED = False


class UnitEffectAttempt(RuntimeError):
    pass


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if _ACTIVE is not None:
        _ACTIVE.audit(event, args)


class UnitExecutionMonitor:
    def __init__(self, scratch_roots: tuple[Path, ...] = ()) -> None:
        self.scratch_roots = tuple(path.resolve() for path in scratch_roots)
        self.counts = {
            name: 0
            for name in (
                "network_calls",
                "process_launches",
                "workspace_commits",
                "external_effects",
                "ledger_effect_attempts",
            )
        }
        self.fixture_write_attempts = 0
        self.child_reports: list[dict[str, Any]] = []
        self.driver_process_launches = 0
        self.violations: list[str] = []
        self.probes = 0
        self.closed = False
        self.owner_thread = threading.get_ident()
        self.changing_profile = False
        self.profile_failed = False
        self.callback = self.profile
        self.targets = {
            WorkspaceWriter.commit.__code__: "workspace_commits",
            WorkspaceWriter._replace_and_observe.__code__: "workspace_commits",
            SqliteEffectJournalRepository.create_prepared.__code__: "ledger_effect_attempts",
            threading.Thread.start.__code__: "unobserved_thread",
        }

    def start(self) -> None:
        global _ACTIVE, _INSTALLED
        if _ACTIVE is not None or sys.getprofile() is not None:
            raise RuntimeError("UNIT_MONITOR_ALREADY_ACTIVE")
        if not _INSTALLED:
            sys.addaudithook(_audit)
            _INSTALLED = True
        _ACTIVE = self
        self.changing_profile = True
        try:
            sys.setprofile(self.callback)
            sys.audit("harness.unit_monitor.probe")
            if self.probes != 1:
                raise RuntimeError("UNIT_AUDIT_HOOK_UNAVAILABLE")
        except BaseException:
            sys.setprofile(None)
            _ACTIVE = None
            self.closed = True
            raise
        finally:
            self.changing_profile = False

    def deny(self, kind: str) -> None:
        if kind in self.counts:
            self.counts[kind] += 1
            if kind != "external_effects":
                self.counts["external_effects"] += 1
        else:
            self.violations.append(kind)
        raise UnitEffectAttempt("UNIT_EFFECT_ATTEMPT: " + kind)

    def audit(self, event: str, args: tuple[Any, ...]) -> None:
        if threading.get_ident() != self.owner_thread:
            self.violations.append("foreign_thread")
        if event == "harness.unit_monitor.probe":
            self.probes += 1
        elif event == "sys.setprofile" and not self.changing_profile:
            # CPython clears a profile callback which raises. Keep the original
            # rejection and invalidate coverage instead of masking that error.
            if self.profile_failed:
                self.violations.append("profile_hook_lost")
            else:
                self.deny("monitor_replacement")
        elif event in {
            "subprocess.Popen",
            "os.system",
            "os.exec",
            "os.posix_spawn",
            "os.fork",
            "os.forkpty",
        }:
            self.deny("process_launches")
        elif event in {"socket.connect", "socket.sendto", "socket.sendmsg"}:
            self.deny("network_calls")
        elif event in {"ctypes.dlopen", "ctypes.dlsym", "ctypes.call_function"}:
            self.deny("unobserved_native_execution")
        elif event == "open" and len(args) >= 3:
            flags = args[2]
            if isinstance(flags, int) and flags & (
                os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC
            ):
                self.write_path(args[0])
        elif event == "sqlite3.connect":
            if args[0] != ":memory:":
                self.write_path(args[0])
        elif event in {
            "os.remove",
            "os.rmdir",
            "os.mkdir",
            "os.chmod",
            "os.chown",
            "os.utime",
            "os.truncate",
        }:
            # Relative dir_fd operations cannot be resolved from cwd alone.
            if len(args) > 1 and not isinstance(args[0], int) and not Path(args[0]).is_absolute():
                self.deny("unresolved_relative_mutation")
            self.write_path(args[0])
        elif event in {"os.rename", "os.link"}:
            if not all(Path(value).is_absolute() for value in args[:2]):
                self.deny("unresolved_relative_mutation")
            self.write_path(args[0])
            self.write_path(args[1])
        elif event == "os.symlink":
            if not Path(args[1]).is_absolute():
                self.deny("unresolved_relative_mutation")
            self.write_path(args[1])

    def write_path(self, value: Any) -> None:
        if isinstance(value, int):
            try:
                value = os.readlink(f"/proc/self/fd/{value}")
            except OSError:
                self.deny("unresolved_file_descriptor")
        if not isinstance(value, str | bytes | os.PathLike):
            self.deny("unknown_write_path")
        path = Path(os.fsdecode(value)).resolve()
        if any(path == root or root in path.parents for root in self.scratch_roots):
            self.fixture_write_attempts += 1
            return
        self.deny("external_effects")

    def profile(self, frame: Any, event: str, arg: Any) -> None:
        try:
            self._profile_event(frame, event, arg)
        except BaseException:
            self.profile_failed = True
            self.violations.append("profile_callback_failed")
            raise

    def _profile_event(self, frame: Any, event: str, arg: Any) -> None:
        if event == "call" and frame.f_code in self.targets:
            self.deny(self.targets[frame.f_code])
        elif event == "c_call":
            owner = getattr(arg, "__self__", None)
            name = getattr(arg, "__name__", "")
            if isinstance(owner, socket.socket) and name in {
                "send",
                "sendall",
                "sendto",
                "sendmsg",
                "connect",
                "connect_ex",
            }:
                self.deny("network_calls")
            if type(owner) in (io.StringIO, io.BytesIO):
                return  # Exact in-memory builtins cannot write a filesystem object.
            if isinstance(owner, io.IOBase) and name in {"write", "writelines", "truncate"}:
                self.write_path(getattr(owner, "name", None))
            if arg in (os.write, os.writev, os.pwrite, os.pwritev):
                self.deny("unobserved_fd_write")

    def suspend_driver(self) -> None:
        """Only the fixed test-driver helper may run outside the SUT scope."""
        global _ACTIVE
        if _ACTIVE is not self or sys.getprofile() is not self.callback:
            self.deny("driver_scope_invalid")
        self.changing_profile = True
        try:
            sys.setprofile(None)
            _ACTIVE = None
        finally:
            self.changing_profile = False

    def resume_driver(self, child: dict[str, Any] | None) -> None:
        global _ACTIVE
        self.driver_process_launches += 1
        if child is None or child.get("complete") is not True:
            self.violations.append("child_observation_incomplete")
        elif child.get("contract") != "unit-execution-monitor/1" or any(
            type(child.get("counts", {}).get(key)) is not int or child["counts"][key] < 0
            for key in self.counts
        ):
            self.violations.append("child_observation_invalid")
        else:
            self.child_reports.append(child)
            for key in self.counts:
                self.counts[key] += child["counts"][key]
        if _ACTIVE is not None or sys.getprofile() is not None:
            self.violations.append("driver_hook_changed")
        _ACTIVE = self
        self.changing_profile = True
        try:
            sys.setprofile(self.callback)
        finally:
            self.changing_profile = False

    def finish(self) -> dict[str, Any]:
        global _ACTIVE
        if self.closed or _ACTIVE is not self:
            raise RuntimeError("UNIT_MONITOR_NOT_ACTIVE")
        if sys.getprofile() is not self.callback:
            self.violations.append("profile_hook_lost")
        try:
            sys.audit("harness.unit_monitor.probe")
        finally:
            self.changing_profile = True
            try:
                sys.setprofile(None)
            finally:
                _ACTIVE = None
                self.closed = True
                self.changing_profile = False
        return {
            "contract": "unit-execution-monitor/1",
            "scope": "pytest_python_test_function",
            "complete": not self.violations and self.probes == 2,
            "counts": dict(self.counts),
            "fixture_write_attempts": self.fixture_write_attempts,
            "violations": list(self.violations),
            "audit_probes": self.probes,
            "driver_process_launches": self.driver_process_launches,
            "child_reports": self.child_reports,
            "limits": [
                "Python audit/profile and named production effect entrypoints",
                "not an OS sandbox; no arbitrary native-code containment claim",
                "fixture setup/teardown excluded; in-call scratch writes counted separately",
            ],
        }
