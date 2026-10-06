"""Synthetic effect attempts; never provider calls or user workspace writes."""

from __future__ import annotations

import io
import os
import socket
import subprocess
import sys
import threading

import pytest
import unit_execution_monitor as monitoring
from unit_execution_monitor import UnitEffectAttempt, UnitExecutionMonitor


def test_pure_call_and_memory_streams_are_observed():
    monitor = UnitExecutionMonitor()
    monitor.start()
    try:
        io.StringIO().write("synthetic")
        io.BytesIO().write(b"synthetic")
        assert sum((1, 2, 3)) == 6
    finally:
        report = monitor.finish()
    assert report["complete"] is True
    assert report["audit_probes"] == 2
    assert set(report["counts"].values()) == {0}
    assert monitoring._ACTIVE is None and sys.getprofile() is None


@pytest.mark.parametrize(
    "attempt, counter",
    [
        (
            lambda: subprocess.run(  # noqa: S603 - fixed synthetic process, blocked by monitor
                [sys.executable, "-c", "raise SystemExit(93)"], check=False
            ),
            "process_launches",
        ),
        (lambda: monitoring.WorkspaceWriter.commit(None, None), "workspace_commits"),
        (
            lambda: monitoring.SqliteEffectJournalRepository.create_prepared(None, None),
            "ledger_effect_attempts",
        ),
    ],
)
def test_effect_entry_is_counted_before_execution(attempt, counter):
    monitor = UnitExecutionMonitor()
    monitor.start()
    try:
        with pytest.raises(UnitEffectAttempt):
            attempt()
    finally:
        report = monitor.finish()
    assert report["counts"][counter] == 1
    assert report["counts"]["external_effects"] == 1
    assert monitoring._ACTIVE is None and sys.getprofile() is None


def test_socket_connect_is_denied_before_any_connection():
    with socket.socket() as sock:
        monitor = UnitExecutionMonitor()
        monitor.start()
        try:
            with pytest.raises(UnitEffectAttempt):
                sock.connect(("127.0.0.1", 9))
        finally:
            report = monitor.finish()
    assert report["counts"]["network_calls"] == 1


@pytest.mark.parametrize("method", ["open", "preopened", "fd", "truncate", "rename"])
def test_outside_scratch_mutation_is_denied(tmp_path, method):
    target = tmp_path / "canary"
    target.write_bytes(b"preserved")
    with target.open("r+b") as handle:
        monitor = UnitExecutionMonitor()
        monitor.start()
        try:
            with pytest.raises(UnitEffectAttempt):
                if method == "open":
                    target.write_bytes(b"changed")
                elif method == "preopened":
                    handle.write(b"changed")
                elif method == "fd":
                    os.write(handle.fileno(), b"changed")
                elif method == "truncate":
                    os.truncate(target, 0)
                else:
                    target.rename(tmp_path / "renamed")
        finally:
            report = monitor.finish()
    assert target.read_bytes() == b"preserved"
    assert report["counts"]["external_effects"] or report["violations"]
    assert str(target) not in str(report)


def test_scratch_writes_are_measured_separately(tmp_path):
    monitor = UnitExecutionMonitor((tmp_path,))
    monitor.start()
    try:
        (tmp_path / "synthetic").write_text("fixture")
    finally:
        report = monitor.finish()
    assert report["complete"] is True
    assert report["fixture_write_attempts"] > 0
    assert report["counts"]["external_effects"] == 0


@pytest.mark.parametrize(
    "attempt, reason",
    [
        (lambda: sys.setprofile(None), "monitor_replacement"),
        (lambda: threading.Thread(target=lambda: None).start(), "unobserved_thread"),
        (lambda: sys.audit("ctypes.dlopen", "synthetic"), "unobserved_native_execution"),
    ],
)
def test_unobservable_execution_invalidates_report(attempt, reason):
    monitor = UnitExecutionMonitor()
    monitor.start()
    try:
        with pytest.raises(UnitEffectAttempt):
            attempt()
    finally:
        report = monitor.finish()
    assert report["complete"] is False
    assert reason in report["violations"]
    assert monitoring._ACTIVE is None and sys.getprofile() is None


def test_child_report_is_required_after_driver_suspension():
    monitor = UnitExecutionMonitor()
    monitor.start()
    monitor.suspend_driver()
    monitor.resume_driver(None)
    report = monitor.finish()
    assert report["complete"] is False
    assert "child_observation_incomplete" in report["violations"]


def test_audit_start_failure_cleans_up(monkeypatch):
    def reject_probe(event, *args):
        if event == "harness.unit_monitor.probe":
            raise RuntimeError("synthetic startup failure")

    monkeypatch.setattr(sys, "audit", reject_probe)
    monitor = UnitExecutionMonitor()
    with pytest.raises(RuntimeError, match="startup failure"):
        monitor.start()
    assert monitoring._ACTIVE is None and sys.getprofile() is None
