"""Runner の Process 境界。**偽 CLI を実 Process として起動して測る。**

関数 Mock では argv・env・cwd・stdin・終了 Code・出力上限・timeout・孫 Process の
どれも踏めない。ここは全部、本物の `subprocess` 経路で確かめる。
"""

from __future__ import annotations

import errno
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from harness.domain.hashing import hash_canonical
from harness.infrastructure.provider.cli_runner import SubprocessCliRunner
from harness.ports.cli_workbench import CliLaunchSpec, CliOutcome

from .conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "tests" / "support"))
from owned_child_cleanup import CleanupUnconfirmed, cleanup_record, own_leaked_child, start_ticks
from workbench_fixtures import fake_argv_prefix, write_fake_cli

pytestmark = pytest.mark.integration


def _spec(tmp_path: Path, *, mode: str, argv_tail: tuple[str, ...] = ()) -> CliLaunchSpec:
    script = write_fake_cli(
        tmp_path / "bin",
        mode=mode,
        provider="codex",
        leaked_child_record=tmp_path / "child.json" if mode == "LEAK_CHILD" else None,
    )
    cwd = tmp_path / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    argv = tuple(fake_argv_prefix(script)) + argv_tail
    projection = {"argv": list(argv), "cwd": str(cwd)}
    return CliLaunchSpec(
        provider_id="codex",
        model_id="fake-model",
        argv=argv,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"},
        cwd=str(cwd),
        runtime_hash=hash_canonical(projection, artifact_type="test-runtime", schema_major=1),
        runtime_projection=projection,
    )


def _run(spec: CliLaunchSpec, payload: bytes, **kwargs: int) -> object:
    options = {
        "timeout_seconds": 20,
        "max_stdout_bytes": 512 * 1024,
        "max_stderr_bytes": 64 * 1024,
    }
    options.update(kwargs)
    return SubprocessCliRunner().run(spec, stdin_payload=payload, **options)  # type: ignore[arg-type]


def test_normal_run_returns_stdout_and_exit_code(tmp_path: Path) -> None:
    payload = json.dumps({"current_file_text": "print(1)\n"}).encode("utf-8")
    result = _run(_spec(tmp_path, mode="OK"), payload)
    assert result.outcome is CliOutcome.COMPLETED
    assert result.exit_code == 0
    assert b"replacement_text" in result.stdout
    assert not result.stdout_truncated


def test_payload_travels_on_stdin_and_the_environment_is_not_inherited(tmp_path: Path) -> None:
    """本文は argv にも env にも入らない。継承環境も渡らない。"""
    os.environ["NODE_OPTIONS"] = "--require /tmp/evil.js"
    os.environ["HTTPS_PROXY"] = "http://attacker.invalid"
    try:
        payload = json.dumps({"secret_marker": "PAYLOAD-ONLY-ON-STDIN"}).encode("utf-8")
        result = _run(_spec(tmp_path, mode="OBSERVE_ARGV", argv_tail=("--model", "m")), payload)
        observed = json.loads(result.stdout.decode("utf-8"))
    finally:
        del os.environ["NODE_OPTIONS"]
        del os.environ["HTTPS_PROXY"]
    assert observed["stdin_bytes"] == len(payload)
    assert "PAYLOAD-ONLY-ON-STDIN" not in json.dumps(observed["argv"])
    assert "PAYLOAD-ONLY-ON-STDIN" not in json.dumps(observed["env"])
    assert "NODE_OPTIONS" not in observed["env"]
    assert "HTTPS_PROXY" not in observed["env"]
    # `FAKE_CLI_*` は偽 CLI 自身が起動後に自分で入れた印であって、継承ではない。
    inherited = {name for name in observed["env"] if not name.startswith("FAKE_CLI_")}
    assert inherited <= {"HOME", "PATH", "LC_ALL", "PWD", "LC_CTYPE"}
    assert observed["cwd"] == str(tmp_path / "cwd")


def test_timeout_is_reported_as_an_unfinished_process(tmp_path: Path) -> None:
    started = time.monotonic()
    result = _run(_spec(tmp_path, mode="HANG"), b"{}", timeout_seconds=2)
    assert result.outcome is CliOutcome.TIMEOUT
    assert time.monotonic() - started < 30


def test_non_zero_exit_is_observed_not_guessed(tmp_path: Path) -> None:
    result = _run(_spec(tmp_path, mode="NON_ZERO"), b'{"current_file_text": "x"}')
    assert result.outcome is CliOutcome.COMPLETED
    assert result.exit_code == 3


def test_stdout_limit_stops_the_read(tmp_path: Path) -> None:
    result = _run(_spec(tmp_path, mode="FLOOD_STDOUT"), b"{}", max_stdout_bytes=200_000)
    assert result.outcome is CliOutcome.OUTPUT_LIMIT_EXCEEDED
    assert result.stdout_truncated
    assert len(result.stdout) <= 200_000


def test_stderr_limit_stops_the_read_without_returning_its_bytes(tmp_path: Path) -> None:
    result = _run(_spec(tmp_path, mode="FLOOD_STDERR"), b"{}", max_stderr_bytes=100_000)
    assert result.outcome is CliOutcome.OUTPUT_LIMIT_EXCEEDED
    assert result.stderr_truncated
    assert result.stderr_bytes_observed > 100_000


def test_stderr_content_never_reaches_the_caller(tmp_path: Path) -> None:
    """stderr に Canary を書いても、Runner は長さしか返さない。"""
    result = _run(_spec(tmp_path, mode="STDERR_NOISE"), b'{"current_file_text": "x"}')
    assert result.outcome is CliOutcome.COMPLETED
    assert result.stderr_bytes_observed > 0
    assert not hasattr(result, "stderr")
    assert b"CANARY" not in result.stdout


def test_spawn_failure_is_the_only_case_that_proves_nothing_was_sent(tmp_path: Path) -> None:
    spec = _spec(tmp_path, mode="OK")
    missing = CliLaunchSpec(
        provider_id=spec.provider_id,
        model_id=spec.model_id,
        argv=(str(tmp_path / "does-not-exist"),),
        env=dict(spec.env),
        cwd=spec.cwd,
        runtime_hash=spec.runtime_hash,
        runtime_projection=spec.runtime_projection,
    )
    result = _run(missing, b"{}")
    assert result.outcome is CliOutcome.SPAWN_FAILED
    assert result.exit_code is None


def test_a_leftover_grandchild_does_not_hang_the_runner(tmp_path: Path) -> None:
    """親が終わっても孫が Pipe を掴んだままの形。**読むのをやめて不明で返す。**"""
    with own_leaked_child(tmp_path / "child.json"):
        started = time.monotonic()
        result = _run(_spec(tmp_path, mode="LEAK_CHILD"), b"{}", timeout_seconds=20)
        elapsed = time.monotonic() - started
        assert result.outcome in {CliOutcome.UNKNOWN, CliOutcome.COMPLETED}
        assert elapsed < 18, "孫を待ち続けてはならない"


def test_runner_rejects_impossible_limits(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _run(_spec(tmp_path, mode="OK"), b"{}", timeout_seconds=0)


def _assert_grandchild_stopped(proc: Path) -> None:
    """ENOENT/ESRCHは消滅。PermissionError/EIOなどは判定不能として伝播する。

    proc_single_showは取得したTaskが消滅している場合にESRCHを返す。ファイルを
    開いた後の消滅もあり得るので、FileNotFoundErrorだけでは終了競合を扱えない。
    この確認はSignalを送らず、読めたProcessには従来どおりZombieを要求する。
    """
    try:
        text = proc.read_text()
    except (FileNotFoundError, ProcessLookupError):
        return
    assert text[text.rfind(")") + 2 :].split()[0] == "Z"


@pytest.mark.parametrize("exit_path", ["normal", "assertion", "exception"])
def test_own_grandchild_is_stopped_on_every_test_exit(tmp_path: Path, exit_path: str) -> None:
    record_path = tmp_path / "child.json"
    expected = AssertionError if exit_path == "assertion" else RuntimeError

    def exercise() -> None:
        with own_leaked_child(record_path):
            _run(_spec(tmp_path, mode="LEAK_CHILD"), b"{}", timeout_seconds=20)
            record = json.loads(record_path.read_text())
            assert start_ticks(record["pid"]) == record["start_ticks"]
            if exit_path == "assertion":
                raise AssertionError("synthetic assertion failure")
            if exit_path == "exception":
                raise RuntimeError("synthetic mid-test error")

    if exit_path == "normal":
        exercise()
    else:
        with pytest.raises(expected):
            exercise()
    record = json.loads(record_path.read_text())
    proc = Path(f"/proc/{record['pid']}/stat")
    _assert_grandchild_stopped(proc)


@pytest.mark.parametrize("field", ["uid", "start_ticks"])
def test_cleanup_does_not_signal_an_identity_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    record = {"pid": os.getpid(), "uid": os.getuid(), "start_ticks": start_ticks(os.getpid())}
    record[field] += 1
    path = tmp_path / "child.json"
    path.write_text(json.dumps(record))
    calls: list[int] = []
    monkeypatch.setattr(signal, "pidfd_send_signal", lambda fd, signum: calls.append(fd))
    with pytest.raises(CleanupUnconfirmed):
        cleanup_record(path)
    assert calls == []


@pytest.mark.parametrize(
    "error_number", [errno.ENOENT, errno.ESRCH, errno.EACCES, errno.EPERM, errno.EIO]
)
def test_stopped_process_read_distinguishes_disappearance_from_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_number: int
) -> None:
    proc = tmp_path / "synthetic-proc-stat"

    def read_error(self: Path, *args: object, **kwargs: object) -> str:
        raise OSError(error_number, "synthetic proc read failure")

    monkeypatch.setattr(Path, "read_text", read_error)
    if error_number in {errno.ENOENT, errno.ESRCH}:
        _assert_grandchild_stopped(proc)
    else:
        with pytest.raises(OSError) as caught:
            _assert_grandchild_stopped(proc)
        assert caught.value.errno == error_number


@pytest.mark.parametrize("state", ["S", "R", "D", "Z"])
def test_readable_process_still_must_be_non_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    proc = tmp_path / "synthetic-proc-stat"
    monkeypatch.setattr(
        Path, "read_text", lambda self, *args, **kwargs: f"1 (synthetic) {state} 1 1 1"
    )
    if state == "Z":
        _assert_grandchild_stopped(proc)
    else:
        with pytest.raises(AssertionError):
            _assert_grandchild_stopped(proc)
