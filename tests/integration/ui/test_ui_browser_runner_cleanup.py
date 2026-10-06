"""GH-01 Runnerの起動途中の失敗で、起動したProcessを残さないことの回帰試験。

`tools/run_ui_browser_check.py` の `Browser`・`UiProcess` と、`tools/browser_cdp_bridge.cjs`
の起動timeout等へ失敗を注入する。どの経路でも次を確かめる。

* 直接起動した子Processが終了し、**回収（wait）済み**である（zombieとして残らない）。
* 子が起動した孫Process（Chromium相当）も止まっている。
* ログのFile Handleが閉じている。
* 投げられる例外は**元の失敗**であり、後始末の結果は例外へ構造化して付く。
* **signalは所有を保証できる宛先にだけ送る。** 全試験で `os.kill` / `os.killpg` を監査し、
  送信の瞬間に宛先が「この試験Processの未回収の子（zombieを含む）」でなければ失敗とする
  （その signal は実際には送らない）。回収済みのPID/PGIDは番号が再利用され得る。

偽のBridge・UIサーバー・Chromiumは合成の実Processで、実ブラウザーも実Providerも使わない。
Bridge（Node）の試験は Node.js 22以上を必要とする。無い環境ではSkipせず失敗させる
（Skipすると起動timeoutの後始末が未検証のまま残る）。
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import time
import urllib.error
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.integration

#: 監査で包む前の本物。試験自身の後始末（身元を確かめた合成Processだけ）に使う。
_RAW_KILL = os.kill
_RAW_KILLPG = os.killpg
#: 偽の補助Process（Group外へ出る子孫）の実行体があるDirectory。
_PYTHON_DIR = Path(os.path.realpath(sys.executable)).parent

REPO = Path(__file__).resolve().parents[3]
BRIDGE = REPO / "tools" / "browser_cdp_bridge.cjs"


def _load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "gh01_run_ui_browser_check", REPO / "tools" / "run_ui_browser_check.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = _load_runner()

# 孫Process: SIGTERMを無視して居座る。Group全体へのSIGKILLでしか止まらない。
_STUBBORN = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)"

FAKE_BRIDGE = textwrap.dedent(
    f"""
    import json, os, signal, subprocess, sys, time
    mode, state = sys.argv[1], sys.argv[2]
    os.makedirs(state, exist_ok=True)
    grand = subprocess.Popen([sys.executable, "-c", {_STUBBORN!r}, state])
    record = {{"child": os.getpid(), "grandchild": grand.pid}}
    if mode == "escape-silent":
        # Groupの外へ逃げた子孫がstdoutのPipeを持ったまま残る（profileは引数に持たない）。
        marker = os.path.join(os.path.dirname(state), "escaped-marker")
        escaped = subprocess.Popen(
            [sys.executable, "-c", {_STUBBORN!r}, marker], start_new_session=True
        )
        record["escaped"], record["escaped_token"] = escaped.pid, marker
    if mode in ("escape-profile", "escape-profile-brief"):
        # Groupの外へ出てprofileを引数に持つ子孫（Chromiumのcrashpad handler相当）。
        # Pipeは持たない。briefはすぐ自ら終わり、もう一方は居座る。
        body = "import time; time.sleep(0.5)" if mode.endswith("brief") else {_STUBBORN!r}
        escaped = subprocess.Popen(
            [sys.executable, "-c", body, state],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        record["escaped"] = escaped.pid
    with open(os.path.join(state, "pids.json"), "w") as sink:
        json.dump(record, sink)
    if mode == "escape-silent":
        sys.exit(4)
    if mode == "ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    out = sys.stdout
    if mode == "fatal":
        out.write(json.dumps({{"fatal": "CHROMIUM_START_TIMEOUT"}}) + "\\n"); out.flush()
    elif mode == "garbage":
        out.write("not json\\n"); out.flush()
    elif mode == "not-ready":
        out.write(json.dumps({{"hello": 1}}) + "\\n"); out.flush()
    elif mode == "exit":
        sys.exit(4)
    elif mode in ("ok", "slow", "escape-profile", "escape-profile-brief"):
        out.write(json.dumps({{"ready": True}}) + "\\n"); out.flush()
        for line in sys.stdin:
            command = json.loads(line)
            if mode == "slow":
                time.sleep(1.0)
            if command["op"] == "quit":
                grand.kill(); grand.wait()
                out.write(json.dumps({{"id": command["id"], "ok": True, "result": {{}}}}) + "\\n")
                out.flush()
                sys.exit(0)
            reply = {{"id": command["id"], "ok": True, "result": command["op"]}}
            out.write(json.dumps(reply) + "\\n")
            out.flush()
        sys.exit(0)
    time.sleep(600)
    """
)

FAKE_UI = textwrap.dedent(
    f"""
    import json, os, signal, subprocess, sys, time
    from http.server import BaseHTTPRequestHandler, HTTPServer
    mode, state = sys.argv[1], sys.argv[2]
    os.makedirs(state, exist_ok=True)
    grand = subprocess.Popen([sys.executable, "-c", {_STUBBORN!r}, state])
    with open(os.path.join(state, "pids.json"), "w") as sink:
        json.dump({{"child": os.getpid(), "grandchild": grand.pid}}, sink)
    if mode.endswith("ignore-int"):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    if mode == "exit-early":
        sys.exit(3)
    if mode == "no-url":
        time.sleep(600)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if mode == "http-500":
                status, body = 500, b"{{}}"
            elif mode.startswith("no-token"):
                status, body = 200, b"<html><head></head></html>"
            else:
                status = 200
                body = ('<meta name="harness-session" content="' + "a" * 64 + '">').encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    notice = "ローカルUI: http://127.0.0.1:%d\\n" % server.server_address[1]
    sys.stderr.buffer.write(notice.encode("utf-8")); sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    grand.kill(); grand.wait()
    sys.exit(0)
    """
)


# -- 観測 -------------------------------------------------------------------------


def _state(pid: int) -> str | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    return text.rsplit(")", 1)[1].split()[0]


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    """止まった（存在しない、または他者のzombie）ことを確かめる。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _state(pid) in (None, "Z", "X"):
            return True
        time.sleep(0.05)
    return False


def _reaped(pid: int) -> bool:
    """この試験Processの子として**回収済み**であること。zombieのままならFalse。"""
    return _state(pid) is None


def _open_paths() -> set[str]:
    paths = set()
    for fd in os.listdir("/proc/self/fd"):
        with contextlib.suppress(OSError):
            paths.add(os.readlink(f"/proc/self/fd/{fd}"))
    return paths


def _pids(state: Path) -> dict[str, int]:
    deadline = time.monotonic() + 10
    path = state / "pids.json"
    while time.monotonic() < deadline:
        with contextlib.suppress(FileNotFoundError, json.JSONDecodeError):
            loaded: dict[str, int] = json.loads(path.read_text())
            return loaded
        time.sleep(0.05)
    raise AssertionError("fake process did not record its pids")


def _assert_released(state: Path, log: Path) -> None:
    pids = _pids(state)
    assert _reaped(pids["child"]), f"child {pids['child']} left running or unreaped"
    assert _wait_dead(pids["grandchild"]), f"grandchild {pids['grandchild']} left running"
    assert str(log) not in _open_paths(), "log handle left open"


def _unreaped_child(pid: int) -> bool:
    """この試験Processの未回収の子（zombieを含む）か。番号の所有を保証できる唯一の場合。"""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return int(text.rsplit(")", 1)[1].split()[1]) == os.getpid()


def _is_synthetic(pid: int, token: str) -> bool:
    """合成の孫Processであることを、起動時に渡したToken（state Path）で確かめる。"""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except (FileNotFoundError, ProcessLookupError):
        # /proc may disappear after open but before read (ESRCH). Identity is not
        # established, so do not signal. Other read failures still propagate.
        return False
    return token.encode() in cmdline and _STUBBORN.encode() in cmdline


@pytest.mark.parametrize(
    "gone_error",
    [
        FileNotFoundError(2, "synthetic vanished path"),
        ProcessLookupError(3, "synthetic vanished task"),
    ],
)
def test_cleanup_guard_does_not_signal_an_unidentifiable_vanished_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gone_error: OSError
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    pid = 42424242
    (state / "pids.json").write_text(json.dumps({"grandchild": pid}))
    proc_path = Path(f"/proc/{pid}/cmdline")
    original_read = Path.read_bytes
    delivered: list[tuple[int, int]] = []

    def disappearing(path: Path) -> bytes:
        if path == proc_path:
            raise gone_error
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", disappearing)
    monkeypatch.setattr(
        sys.modules[__name__], "_RAW_KILL", lambda target, sig: delivered.append((target, sig))
    )
    with _group_guard(state):
        pass
    assert delivered == []


@pytest.mark.parametrize(
    "read_error",
    [PermissionError(13, "synthetic denied read"), OSError(5, "synthetic I/O failure")],
)
def test_synthetic_identity_check_does_not_hide_unexpected_read_errors(
    monkeypatch: pytest.MonkeyPatch, read_error: OSError
) -> None:
    original_read = Path.read_bytes
    proc_path = Path("/proc/42424242/cmdline")

    def failed_read(path: Path) -> bytes:
        if path == proc_path:
            raise read_error
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", failed_read)
    with pytest.raises(OSError) as raised:
        _is_synthetic(42424242, "synthetic-state")
    assert raised.value is read_error


@contextlib.contextmanager
def _group_guard(state: Path) -> Iterator[None]:
    """試験が失敗しても合成Processを残さない（観測の後で掃除する）。

    ここでも所有を保証できない宛先へは送らない。子は「未回収の自分の子」のとき
    だけGroupごと止め、孫は起動時のTokenで身元を確かめたものだけ止める。
    """
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError, json.JSONDecodeError):
            pids = json.loads((state / "pids.json").read_text())
            child = pids.get("child")
            if child is not None and _unreaped_child(child):
                with contextlib.suppress(ProcessLookupError):
                    _RAW_KILLPG(child, signal.SIGKILL)
                with contextlib.suppress(ChildProcessError):
                    os.waitpid(child, 0)
            for key, token in (
                ("grandchild", str(state)),
                ("escaped", pids.get("escaped_token", str(state))),
            ):
                pid = pids.get(key)
                if pid is not None and _is_synthetic(pid, token):
                    with contextlib.suppress(ProcessLookupError):
                        _RAW_KILL(pid, signal.SIGKILL)


class SignalAudit:
    def __init__(self) -> None:
        self.delivered: list[tuple[str, int, int]] = []
        self.violations: list[tuple[str, int, int]] = []

    def wrap(self, kind: str, raw: Any) -> Any:
        def send(pid: int, sig: int) -> None:
            if not _unreaped_child(pid):
                # 所有を保証できない宛先。**送らずに**違反として残す。
                self.violations.append((kind, pid, int(sig)))
                return
            self.delivered.append((kind, pid, int(sig)))
            raw(pid, sig)

        return send


@pytest.fixture(autouse=True)
def signal_audit(monkeypatch: pytest.MonkeyPatch) -> Iterator[SignalAudit]:
    """全試験で、Runnerが送るsignalの宛先が未回収の自分の子であることを確かめる。"""
    audit = SignalAudit()
    monkeypatch.setattr(os, "kill", audit.wrap("kill", _RAW_KILL))
    monkeypatch.setattr(os, "killpg", audit.wrap("killpg", _RAW_KILLPG))
    yield audit
    assert audit.violations == [], f"signal sent to a pid/pgid not owned: {audit.violations}"


def _write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


# -- Browser（Python側） ------------------------------------------------------------


def _browser(tmp_path: Path, mode: str, **overrides: Any) -> Any:
    fake = _write(tmp_path, "fake_bridge.py", FAKE_BRIDGE)
    options: dict[str, Any] = {"ready_timeout": 1.5, "stop_grace": 1.0, "helper_dir": _PYTHON_DIR}
    options.update(overrides)
    return runner.Browser(
        sys.executable, mode, tmp_path / "state", tmp_path / "bridge.log", bridge=fake, **options
    )


def test_browser_spawn_failure_closes_the_log(tmp_path: Path) -> None:
    log = tmp_path / "bridge.log"
    with pytest.raises(FileNotFoundError):
        runner.Browser(str(tmp_path / "absent-node"), "chromium", tmp_path / "profile", log)
    assert str(log) not in _open_paths()


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("fatal", "CHROMIUM_START_TIMEOUT"),
        ("garbage", "non-JSON"),
        ("not-ready", "did not become ready"),
        ("exit", "^bridge exited rc=4 without a response$"),
        ("silent", "bridge response timeout"),
        ("ignore-term", "bridge response timeout"),
    ],
)
def test_browser_startup_failure_reaps_bridge_and_its_children(
    tmp_path: Path, mode: str, message: str
) -> None:
    state = tmp_path / "state"
    with _group_guard(state):
        with pytest.raises(runner.BridgeError, match=message) as raised:
            _browser(tmp_path, mode)
        assert getattr(raised.value, "__notes__", []) == []
        if mode == "garbage":
            assert isinstance(raised.value.__cause__, json.JSONDecodeError)
        _assert_released(state, tmp_path / "bridge.log")


def test_browser_normal_close_reaps_everything(tmp_path: Path) -> None:
    state = tmp_path / "state"
    with _group_guard(state):
        browser = _browser(tmp_path, "ok")
        assert browser.call("version") == "version"
        browser.close()
        _assert_released(state, tmp_path / "bridge.log")


def _synthetic_unconfirmed(*_args: Any, **_kwargs: Any) -> None:
    raise runner.ProcessCleanupUnconfirmed("synthetic cleanup failure", pgid=0, remaining=[4242])


def test_browser_cleanup_failure_keeps_the_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """後始末（Groupの停止）が失敗しても、元の起動失敗を投げ、失敗は構造化して付く。"""
    monkeypatch.setattr(runner.OwnedProcessGroup, "stop", _synthetic_unconfirmed)
    state = tmp_path / "state"
    with _group_guard(state):
        with pytest.raises(runner.BridgeError) as raised:
            _browser(tmp_path, "fatal")
        assert str(raised.value) == "CHROMIUM_START_TIMEOUT"
        assert raised.value.__notes__ == [
            "cleanup failed: ProcessCleanupUnconfirmed: synthetic cleanup failure"
            " (pgid=0, remaining=[4242])"
        ]
        outcome = runner.cleanup_outcome(raised.value)
        assert outcome is not None and outcome["status"] == "UNCONFIRMED"
        assert outcome["failures"] == [
            {
                "type": "ProcessCleanupUnconfirmed",
                "message": "synthetic cleanup failure (pgid=0, remaining=[4242])",
                "pgid": 0,
                "remaining": [4242],
            }
        ]
        assert str(tmp_path / "bridge.log") not in _open_paths()


def test_browser_confirmed_cleanup_is_recorded_on_the_start_failure(tmp_path: Path) -> None:
    state = tmp_path / "state"
    with _group_guard(state):
        with pytest.raises(runner.BridgeError) as raised:
            _browser(tmp_path, "fatal")
        assert runner.cleanup_outcome(raised.value) == {"status": "CONFIRMED", "failures": []}


def test_browser_spawn_failure_is_recorded_as_not_started(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError) as raised:
        runner.Browser(str(tmp_path / "absent-node"), "c", tmp_path / "p", tmp_path / "b.log")
    assert runner.cleanup_outcome(raised.value) == {"status": "NOT_STARTED", "failures": []}


def test_version_probe_failure_after_ready_closes_the_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`main()` は準備完了後の `version` 失敗でもBrowserを閉じてUNVERIFIEDにする。"""
    fake = _write(tmp_path, "fake_bridge.py", FAKE_BRIDGE)
    state = tmp_path / "out" / "profile"
    created: list[Any] = []
    original = runner.Browser

    def build(node: str, chromium: str, profile: Path, log: Path) -> Any:
        browser = original(sys.executable, "ok", profile, log, bridge=fake, stop_grace=1.0)
        created.append(browser)

        real_call = browser.call

        def fail(op: str, **payload: Any) -> Any:
            if op == "version":
                raise runner.BridgeError(f"{op}: synthetic probe failure")
            return real_call(op, **payload)

        browser.call = fail
        return browser

    monkeypatch.setattr(runner, "Browser", build)
    chromium = _write(tmp_path, "chromium", "")
    chromium.chmod(0o755)
    with _group_guard(state):
        code = runner.main(
            ["--out", str(tmp_path / "out"), "--chromium", str(chromium), "--node", sys.executable]
        )
        assert code == runner.EXIT_UNAVAILABLE
        document = json.loads((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
        assert document["overall"] == "UNVERIFIED"
        assert document["unavailable_reason"] == "BROWSER_START_FAILED"
        assert document["start_failure"]["type"] == "BridgeError"
        assert document["start_failure"]["message"] == "version: synthetic probe failure"
        assert document["process_cleanup"] == {"status": "CONFIRMED", "failures": []}
        assert {check["status"] for check in document["checks"]} == {"UNVERIFIED"}
        assert len(created) == 1
        _assert_released(state, tmp_path / "out" / "bridge.log")


# -- UiProcess --------------------------------------------------------------------


def _ui(tmp_path: Path, mode: str, **overrides: Any) -> Any:
    fake = _write(tmp_path, "fake_ui.py", FAKE_UI)
    options: dict[str, Any] = {"startup_timeout": 10.0, "stop_grace": 1.0}
    options.update(overrides)
    return runner.UiProcess.start(
        tmp_path,
        tmp_path / "state.sqlite3",
        tmp_path / "cas",
        operator=False,
        canary="synthetic-canary",
        command=[sys.executable, str(fake), mode, str(tmp_path / "state")],
        **options,
    )


@pytest.mark.parametrize(
    ("mode", "expected", "message", "overrides"),
    [
        ("exit-early", RuntimeError, "ui exited early rc=3", {}),
        ("no-url", RuntimeError, "did not report its URL", {"startup_timeout": 1.0}),
        ("http-500", urllib.error.HTTPError, "500", {}),
        ("no-token", RuntimeError, "session token meta was not served", {}),
        ("no-token-ignore-int", RuntimeError, "session token meta was not served", {}),
    ],
)
def test_ui_startup_failure_stops_and_reaps_the_server(
    tmp_path: Path,
    mode: str,
    expected: type[BaseException],
    message: str,
    overrides: dict[str, Any],
) -> None:
    state = tmp_path / "state"
    with _group_guard(state):
        with pytest.raises(expected, match=message) as raised:
            _ui(tmp_path, mode, **overrides)
        assert getattr(raised.value, "__notes__", []) == []
        _assert_released(state, tmp_path / "server.log")


def test_ui_interrupted_startup_is_cleaned_up_and_re_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """URL待ちの最中の中断（KeyboardInterrupt）でもServerを残さない。"""
    state = tmp_path / "state"

    def interrupted(_process: Any, _log: Path, _timeout: float) -> str:
        _pids(state)  # 子が起動し、孫も作った後で中断する
        raise KeyboardInterrupt

    monkeypatch.setattr(runner.UiProcess, "_await_url", staticmethod(interrupted))
    with _group_guard(state):
        with pytest.raises(KeyboardInterrupt) as raised:
            _ui(tmp_path, "no-url")
        assert getattr(raised.value, "__notes__", []) == []
        _assert_released(state, tmp_path / "server.log")


def test_ui_normal_start_and_stop_reaps_everything(tmp_path: Path) -> None:
    state = tmp_path / "state"
    with _group_guard(state):
        server = _ui(tmp_path, "ok")
        assert re.fullmatch(r"http://127\.0\.0\.1:\d+", server.url)
        assert server.token == "a" * 64
        assert server.stop() == 0
        _assert_released(state, tmp_path / "server.log")


def test_ui_cleanup_failure_keeps_the_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner.OwnedProcessGroup, "stop", _synthetic_unconfirmed)
    with _group_guard(tmp_path / "state"):
        with pytest.raises(RuntimeError, match="session token meta was not served") as raised:
            _ui(tmp_path, "no-token")
        assert raised.value.__notes__ == [
            "cleanup failed: ProcessCleanupUnconfirmed: synthetic cleanup failure"
            " (pgid=0, remaining=[4242])"
        ]
        outcome = runner.cleanup_outcome(raised.value)
        assert outcome is not None and outcome["status"] == "UNCONFIRMED"
        assert [failure["remaining"] for failure in outcome["failures"]] == [[4242]]


# -- Bridge（Node側）の起動timeout ----------------------------------------------------

FAKE_CHROMIUM = textwrap.dedent(
    """
    import json, os, signal, socket, sys, time
    mode = os.path.basename(sys.argv[0]).removeprefix("chromium-")
    profile = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--user-data-dir="))
    os.makedirs(profile, exist_ok=True)
    with open(os.path.join(profile, "pids.json"), "w") as sink:
        json.dump({"child": os.getpid()}, sink)
    if mode == "ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if mode == "exit-early":
        sys.exit(5)
    if mode == "refused":
        sys.stderr.write("DevTools listening on ws://127.0.0.1:9/devtools/browser/x\\n")
        sys.stderr.flush()
    if mode == "hang-upgrade":
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        port = listener.getsockname()[1]
        sys.stderr.write("DevTools listening on ws://127.0.0.1:%d/devtools/browser/x\\n" % port)
        sys.stderr.flush()
        held = []
        while True:
            held.append(listener.accept()[0])
    time.sleep(600)
    """
)


def _node() -> str:
    node = shutil.which("node")
    assert node is not None, (
        "node が無い。Bridgeの起動timeoutと後始末を実測できない。"
        "Skipすると起動途中のChromiumが残る経路が未検証のまま残る（不変条件#16）。"
    )
    version = subprocess.run(  # noqa: S603 - PATH上のnodeの版確認だけ
        [node, "--version"], check=True, capture_output=True, text=True, timeout=30
    ).stdout
    major = int(version.lstrip("v").split(".")[0])
    assert major >= 22, f"Node.js 22以上が必要（組込みWebSocket）: {version.strip()}"
    return node


def _fake_chromium(tmp_path: Path, mode: str) -> Path:
    path = tmp_path / f"chromium-{mode}"
    path.write_text(f"#!{sys.executable}\n" + FAKE_CHROMIUM, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.mark.parametrize(
    ("mode", "fatal"),
    [
        ("silent", "CHROMIUM_START_TIMEOUT"),
        ("ignore-term", "CHROMIUM_START_TIMEOUT"),
        ("exit-early", "CHROMIUM_EXITED: 5"),
        ("refused", "CDP_CONNECT_FAILED"),
        ("hang-upgrade", "CDP_CONNECT_TIMEOUT"),
    ],
)
def test_bridge_stops_its_chromium_before_reporting_the_startup_failure(
    tmp_path: Path, mode: str, fatal: str
) -> None:
    node = _node()
    profile = tmp_path / "profile"
    started = time.monotonic()
    completed = subprocess.run(  # noqa: S603 - repository script and synthetic executable
        [node, str(BRIDGE), str(_fake_chromium(tmp_path, mode)), str(profile), "800"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        start_new_session=True,
    )
    lines = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    assert lines == [{"fatal": fatal}], completed.stdout
    assert completed.returncode == 3
    pid = json.loads((profile / "pids.json").read_text())["child"]
    # Bridgeは終了前にChromiumを止め、exitを観測している（孤児として残していない）。
    assert _wait_dead(pid, timeout=0.5), f"fake chromium {pid} survived the bridge"
    assert time.monotonic() - started < 30


def test_bridge_rejects_a_missing_launch_timeout(tmp_path: Path) -> None:
    completed = subprocess.run(  # noqa: S603 - repository script
        [_node(), str(BRIDGE), str(_fake_chromium(tmp_path, "silent")), str(tmp_path / "p")],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 2
    assert json.loads(completed.stdout)["fatal"].startswith("USAGE:")
    assert not (tmp_path / "p").exists()


def test_browser_reports_the_bridge_launch_timeout_and_releases_everything(
    tmp_path: Path,
) -> None:
    """Python側から実Bridgeを使い、Chromium起動timeoutの理由がそのまま届くこと。"""
    profile = tmp_path / "profile"
    log = tmp_path / "bridge.log"
    with _group_guard(profile):
        with pytest.raises(runner.BridgeError, match="^CHROMIUM_START_TIMEOUT$"):
            runner.Browser(
                _node(),
                str(_fake_chromium(tmp_path, "ignore-term")),
                profile,
                log,
                launch_timeout_ms=800,
                ready_timeout=30,
            )
        pid = json.loads((profile / "pids.json").read_text())["child"]
        assert _wait_dead(pid, timeout=0.5)
        assert str(log) not in _open_paths()


# -- 所有を保証できる間だけsignalを送る（OwnedProcessGroup） -------------------------

LEADER = textwrap.dedent(
    f"""
    import json, os, signal, subprocess, sys, time
    mode, state = sys.argv[1], sys.argv[2]
    os.makedirs(state, exist_ok=True)
    record = {{"child": os.getpid()}}
    if mode != "exit-alone":
        grand = subprocess.Popen([sys.executable, "-c", {_STUBBORN!r}, state])
        record["grandchild"] = grand.pid
    with open(os.path.join(state, "pids.json"), "w") as sink:
        json.dump(record, sink)
    if mode in ("exit", "exit-alone"):
        sys.exit(7)
    if mode == "ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(600)
    """
)


def _leader(tmp_path: Path, mode: str) -> tuple[Any, Path]:
    script = _write(tmp_path, "leader.py", LEADER)
    state = tmp_path / "state"
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter and synthetic script
        [sys.executable, str(script), mode, str(state)], start_new_session=True
    )
    return runner.OwnedProcessGroup(process), state


def _reap_externally(process: subprocess.Popen[Any], how: str) -> None:
    """Runnerの外で子を回収する（`poll()` も回収することを含めて再現する）。"""
    if how == "wait":
        process.wait(timeout=10)
    elif how == "poll":
        deadline = time.monotonic() + 10
        while process.poll() is None:
            assert time.monotonic() < deadline
            time.sleep(0.05)
    else:
        os.waitpid(process.pid, 0)


def test_early_exit_still_stops_descendants_through_the_owned_group(
    tmp_path: Path, signal_audit: SignalAudit
) -> None:
    group, state = _leader(tmp_path, "exit")
    with _group_guard(state):
        pids = _pids(state)
        assert group.wait_exited(10)
        # 終了の観測は回収しない。zombieの間はPGIDが自分のものであり続ける。
        assert _unreaped_child(pids["child"])
        assert group.stop(interrupt=signal.SIGTERM, grace=1.0) == 7
        assert _reaped(pids["child"])
        assert _wait_dead(pids["grandchild"])
        # 子は既に終わっていたので停止要求は送らず、Group宛てのSIGKILLだけ。
        assert signal_audit.delivered
        assert {entry[0] for entry in signal_audit.delivered} == {"killpg"}
        assert {entry[1:] for entry in signal_audit.delivered} == {(pids["child"], signal.SIGKILL)}


@pytest.mark.parametrize("how", ["wait", "poll", "waitpid"])
def test_reaped_leader_is_never_signalled_and_leftovers_are_reported(
    tmp_path: Path, signal_audit: SignalAudit, how: str
) -> None:
    group, state = _leader(tmp_path, "exit")
    with _group_guard(state):
        pids = _pids(state)
        _reap_externally(group.process, how)
        with pytest.raises(runner.ProcessCleanupUnconfirmed) as raised:
            group.stop(interrupt=signal.SIGTERM, grace=1.0)
        assert raised.value.remaining == [pids["grandchild"]]
        assert signal_audit.delivered == []  # 番号の所有を保証できないので1つも送らない
        assert not group.owned


@pytest.mark.parametrize(("how", "code"), [("wait", 7), ("poll", 7), ("waitpid", -1)])
def test_reaped_leader_without_descendants_stops_quietly(
    tmp_path: Path, signal_audit: SignalAudit, how: str, code: int
) -> None:
    group, state = _leader(tmp_path, "exit-alone")
    with _group_guard(state):
        _pids(state)
        _reap_externally(group.process, how)
        assert group.stop(interrupt=signal.SIGTERM, grace=1.0) == code
        assert signal_audit.delivered == []


def test_second_stop_sends_nothing(tmp_path: Path, signal_audit: SignalAudit) -> None:
    group, state = _leader(tmp_path, "run")
    with _group_guard(state):
        pids = _pids(state)
        first = group.stop(interrupt=signal.SIGTERM, grace=5.0)
        assert first == -signal.SIGTERM
        sent = list(signal_audit.delivered)
        assert sent[0] == ("kill", pids["child"], signal.SIGTERM)
        assert group.stop(interrupt=signal.SIGTERM, grace=5.0) == first
        assert signal_audit.delivered == sent
        assert _reaped(pids["child"])
        assert _wait_dead(pids["grandchild"])


def test_ignored_interrupt_escalates_to_a_group_kill(
    tmp_path: Path, signal_audit: SignalAudit
) -> None:
    group, state = _leader(tmp_path, "ignore-term")
    with _group_guard(state):
        pids = _pids(state)
        assert group.stop(interrupt=signal.SIGTERM, grace=0.5) == -signal.SIGKILL
        assert signal_audit.delivered[0] == ("kill", pids["child"], signal.SIGTERM)
        assert ("killpg", pids["child"], signal.SIGKILL) in signal_audit.delivered
        assert _reaped(pids["child"])
        assert _wait_dead(pids["grandchild"])


def test_unconfirmed_group_keeps_the_leader_unreaped_for_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signal_audit: SignalAudit
) -> None:
    group, state = _leader(tmp_path, "run")
    with _group_guard(state):
        pids = _pids(state)
        real = runner.scan_group
        monkeypatch.setattr(runner, "scan_group", lambda _pgid: runner.ProcScan(matches=[424242]))
        with pytest.raises(runner.ProcessCleanupUnconfirmed) as raised:
            group.stop(interrupt=None, grace=0.1, kill_timeout=0.3)
        assert raised.value.remaining == [424242]
        # 確認できないまま回収しない。Groupを宛先にできる状態（所有）を保つ。
        assert group.owned
        assert _unreaped_child(pids["child"])
        monkeypatch.setattr(runner, "scan_group", real)
        assert group.stop(interrupt=None, grace=0.1) == -signal.SIGKILL
        assert _reaped(pids["child"])


def test_browser_second_close_sends_nothing(tmp_path: Path, signal_audit: SignalAudit) -> None:
    state = tmp_path / "state"
    with _group_guard(state):
        browser = _browser(tmp_path, "ok")
        browser.close()
        sent = list(signal_audit.delivered)
        browser.close()
        assert signal_audit.delivered == sent
        _assert_released(state, tmp_path / "bridge.log")


def test_ui_second_stop_sends_nothing(tmp_path: Path, signal_audit: SignalAudit) -> None:
    state = tmp_path / "state"
    with _group_guard(state):
        server = _ui(tmp_path, "ok")
        code = server.stop()
        sent = list(signal_audit.delivered)
        assert server.stop() == code
        assert signal_audit.delivered == sent
        _assert_released(state, tmp_path / "server.log")


def test_pipe_held_outside_the_group_is_reported_instead_of_hanging(tmp_path: Path) -> None:
    """Group外へ逃げたProcessがstdoutを持っていても止まらず、回収未確認として残す。"""
    state = tmp_path / "state"
    with _group_guard(state):
        started = time.monotonic()
        with pytest.raises(runner.BridgeError) as raised:
            _browser(tmp_path, "escape-silent", ready_timeout=10)
        assert str(raised.value) == "bridge exited rc=4 without a response"
        assert time.monotonic() - started < 30
        outcome = runner.cleanup_outcome(raised.value)
        assert outcome is not None and outcome["status"] == "UNCONFIRMED"
        assert [failure["type"] for failure in outcome["failures"]] == ["ProcessCleanupUnconfirmed"]
        assert "held open by a process outside the group" in outcome["failures"][0]["message"]
        pids = _pids(state)
        assert _reaped(pids["child"])
        assert _wait_dead(pids["grandchild"])
        assert str(tmp_path / "bridge.log") not in _open_paths()


def test_runner_only_signals_and_reaps_through_the_owned_group() -> None:
    """Runnerは `poll()` / `send_signal()` / 直接の `os.kill` 等で回収・送信しない。"""
    import ast

    source = (REPO / "tools" / "run_ui_browser_check.py").read_text(encoding="utf-8")
    offenders: list[str] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.classes: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.classes.append(node.name)
            self.generic_visit(node)
            self.classes.pop()

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            inside = "OwnedProcessGroup" in self.classes
            if isinstance(func, ast.Attribute):
                receiver = ast.unparse(func.value)
                name = func.attr
                forbidden = name in {"poll", "send_signal", "terminate"} or (
                    receiver == "os" and name in {"kill", "killpg", "waitpid"}
                )
                reaping_wait = name in {"wait", "kill"} and receiver.endswith("process")
                if (forbidden or reaping_wait) and not inside:
                    offenders.append(f"{node.lineno}: {receiver}.{name}")
                if name in {"poll", "send_signal", "terminate"}:
                    offenders.append(f"{node.lineno}: {receiver}.{name} (even inside)")
            self.generic_visit(node)

    Visitor().visit(ast.parse(source))
    assert offenders == []


# -- main(): 終了値と最終JSON -------------------------------------------------------


def _stop_then_fail(monkeypatch: pytest.MonkeyPatch, cls: Any, method: str = "stop") -> None:
    """本物の停止を行った**後で**、確認できなかったことにする（合成Processは残さない）。"""
    real = getattr(cls, method)

    def failing(self: Any, *args: Any, **kwargs: Any) -> Any:
        real(self, *args, **kwargs)
        raise runner.ProcessCleanupUnconfirmed(
            "synthetic: descendants not confirmed", pgid=0, remaining=[4242]
        )

    monkeypatch.setattr(cls, method, failing)


def _run_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    bridge_mode: str = "ok",
    scenarios: dict[str, Any] | None = None,
    failing_probe: bool = False,
    chromium_present: bool = True,
) -> tuple[int, dict[str, Any]]:
    fake = _write(tmp_path, "fake_bridge.py", FAKE_BRIDGE)
    original = runner.Browser

    def build(_node: str, _chromium: str, profile: Path, log: Path) -> Any:
        browser = original(
            sys.executable,
            bridge_mode,
            profile,
            log,
            bridge=fake,
            ready_timeout=5,
            stop_grace=1.0,
            outside_grace=0.5,
            helper_dir=_PYTHON_DIR,
        )
        if failing_probe:
            real_call = browser.call

            def call(op: str, **payload: Any) -> Any:
                if op == "version":
                    raise runner.BridgeError(f"{op}: synthetic probe failure")
                return real_call(op, **payload)

            browser.call = call
        return browser

    monkeypatch.setattr(runner, "Browser", build)
    chromium = tmp_path / "chromium"
    if chromium_present:
        _write(tmp_path, "chromium", "").chmod(0o755)
    argv = ["--out", str(tmp_path / "out"), "--chromium", str(chromium), "--node", sys.executable]
    monkeypatch.setattr(runner, "SCENARIOS", scenarios or {"fake": lambda _scenario: None})
    for name in scenarios or {"fake": None}:
        argv += ["--scenario", name]
    code = runner.main(argv)
    document = json.loads((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
    return code, document


def test_main_start_failure_with_confirmed_cleanup_is_environment_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _group_guard(tmp_path / "out" / "profile"):
        code, document = _run_main(tmp_path, monkeypatch, bridge_mode="fatal")
    assert code == runner.EXIT_UNAVAILABLE == 3
    assert document["overall"] == "UNVERIFIED"
    assert document["unavailable_reason"] == "BROWSER_START_FAILED"
    assert document["start_failure"] == {"type": "BridgeError", "message": "CHROMIUM_START_TIMEOUT"}
    assert document["process_cleanup"] == {"status": "CONFIRMED", "failures": []}
    assert {check["status"] for check in document["checks"]} == {"UNVERIFIED"}


def test_main_start_failure_with_unconfirmed_cleanup_keeps_both_and_exits_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stop_then_fail(monkeypatch, runner.OwnedProcessGroup)
    with _group_guard(tmp_path / "out" / "profile"):
        code, document = _run_main(tmp_path, monkeypatch, bridge_mode="fatal")
    assert code == runner.EXIT_CLEANUP_UNCONFIRMED == 4
    assert document["overall"] == "CLEANUP_UNCONFIRMED"
    assert document["unavailable_reason"] == "BROWSER_START_FAILED"
    # 元の起動失敗はそのまま。後始末の失敗は別の構造で残る（文字列化で消えない）。
    assert document["start_failure"]["type"] == "BridgeError"
    assert document["start_failure"]["message"] == "CHROMIUM_START_TIMEOUT"
    assert document["start_failure"]["notes"] == [
        "cleanup failed: ProcessCleanupUnconfirmed: synthetic: descendants not confirmed"
        " (pgid=0, remaining=[4242])"
    ]
    assert document["process_cleanup"] == {
        "status": "UNCONFIRMED",
        "failures": [
            {
                "type": "ProcessCleanupUnconfirmed",
                "message": "synthetic: descendants not confirmed (pgid=0, remaining=[4242])",
                "pgid": 0,
                "remaining": [4242],
            }
        ],
    }
    assert {check["status"] for check in document["checks"]} == {"UNVERIFIED"}


def test_main_probe_failure_with_unconfirmed_close_exits_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stop_then_fail(monkeypatch, runner.OwnedProcessGroup)
    with _group_guard(tmp_path / "out" / "profile"):
        code, document = _run_main(tmp_path, monkeypatch, failing_probe=True)
    assert code == 4
    assert document["overall"] == "CLEANUP_UNCONFIRMED"
    assert document["start_failure"]["message"] == "version: synthetic probe failure"
    assert document["process_cleanup"]["status"] == "UNCONFIRMED"
    assert document["process_cleanup"]["failures"][0]["remaining"] == [4242]


def test_main_chromium_missing_is_not_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, document = _run_main(tmp_path, monkeypatch, chromium_present=False)
    assert code == 3
    assert document["overall"] == "UNVERIFIED"
    assert document["unavailable_reason"] == "CHROMIUM_EXECUTABLE_NOT_FOUND"
    assert document["start_failure"] is None
    assert document["process_cleanup"] == {"status": "NOT_STARTED", "failures": []}


def _passing(scenario: Any) -> None:
    scenario.report.record("B-01", "synthetic pass", "SYNTHETIC", True)


def _failing(scenario: Any) -> None:
    scenario.report.record("B-01", "synthetic fail", "SYNTHETIC", False)


def test_main_normal_run_confirms_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _group_guard(tmp_path / "out" / "profile"):
        code, document = _run_main(tmp_path, monkeypatch, scenarios={"fake": _passing})
    assert code == 0
    assert document["overall"] == "PASS"
    assert document["process_cleanup"] == {"status": "CONFIRMED", "failures": []}
    _assert_released(tmp_path / "out" / "profile", tmp_path / "out" / "bridge.log")


def test_main_failed_check_with_confirmed_cleanup_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _group_guard(tmp_path / "out" / "profile"):
        code, document = _run_main(tmp_path, monkeypatch, scenarios={"fake": _failing})
    assert code == 1
    assert document["overall"] == "FAIL"
    assert document["process_cleanup"]["status"] == "CONFIRMED"


def test_main_unconfirmed_browser_close_overrides_pass_and_keeps_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stop_then_fail(monkeypatch, runner.OwnedProcessGroup)
    with _group_guard(tmp_path / "out" / "profile"):
        code, document = _run_main(tmp_path, monkeypatch, scenarios={"fake": _passing})
    assert code == 4
    assert document["overall"] == "CLEANUP_UNCONFIRMED"
    assert [check["status"] for check in document["checks"]] == ["PASS", "PASS"]
    assert [failure["label"] for failure in document["process_cleanup"]["failures"]] == [
        "browser.close"
    ]


def test_main_scenario_server_stop_failure_is_recorded_without_masking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_ui = _write(tmp_path, "fake_ui.py", FAKE_UI)
    work = tmp_path / "ui"
    work.mkdir()

    def scenario(scenario: Any) -> None:
        server = runner.UiProcess.start(
            work,
            work / "db",
            work / "cas",
            operator=False,
            canary="c",
            command=[sys.executable, str(fake_ui), "ok", str(work / "state")],
            stop_grace=1.0,
        )
        try:
            scenario.report.record("B-01", "synthetic pass", "SYNTHETIC", True)
        finally:
            code = scenario.report.release("fake:ui.stop", server.stop)
            scenario.report.record("B-08", "stop code", "SYNTHETIC", code == 0, code=code)

    _stop_then_fail(monkeypatch, runner.UiProcess)
    with _group_guard(work / "state"), _group_guard(tmp_path / "out" / "profile"):
        code, document = _run_main(tmp_path, monkeypatch, scenarios={"fake": scenario})
    assert code == 4
    assert document["overall"] == "CLEANUP_UNCONFIRMED"
    statuses = {check["name"]: check["status"] for check in document["checks"]}
    assert statuses["synthetic pass"] == "PASS"
    assert statuses["stop code"] == "FAIL"  # 停止を確認できなかったので終了値も無い
    assert [failure["label"] for failure in document["process_cleanup"]["failures"]] == [
        "fake:ui.stop"
    ]
    _assert_released(work / "state", work / "server.log")


def test_main_scenario_start_failure_carries_its_cleanup_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_ui = _write(tmp_path, "fake_ui.py", FAKE_UI)
    work = tmp_path / "ui"
    work.mkdir()

    def scenario(_scenario: Any) -> None:
        runner.UiProcess.start(
            work,
            work / "db",
            work / "cas",
            operator=False,
            canary="c",
            command=[sys.executable, str(fake_ui), "no-token", str(work / "state")],
            stop_grace=1.0,
        )

    real_stop = runner.OwnedProcessGroup.stop
    ui_groups: list[int] = []

    def stop(self: Any, **kwargs: Any) -> Any:
        code = real_stop(self, **kwargs)
        if kwargs.get("interrupt") == signal.SIGINT:  # UIサーバーのGroupだけ失敗させる
            ui_groups.append(self.pgid)
            raise runner.ProcessCleanupUnconfirmed("synthetic ui", pgid=self.pgid, remaining=[1])
        return code

    monkeypatch.setattr(runner.OwnedProcessGroup, "stop", stop)
    with _group_guard(work / "state"), _group_guard(tmp_path / "out" / "profile"):
        code, document = _run_main(tmp_path, monkeypatch, scenarios={"fake": scenario})
    assert code == 4
    [failed] = [check for check in document["checks"] if check["check_id"] == "fake"]
    assert failed["status"] == "FAIL"
    assert failed["details"]["type"] == "RuntimeError"
    assert failed["details"]["message"] == "session token meta was not served"
    assert failed["details"]["cleanup"]["status"] == "UNCONFIRMED"
    assert [f["label"] for f in document["process_cleanup"]["failures"]] == ["fake scenario fake"]
    assert len(ui_groups) == 1
    _assert_released(work / "state", work / "server.log")


def test_a_timed_out_read_is_continued_by_the_next_read(tmp_path: Path) -> None:
    """読取りThreadは1本だけ。timeoutした読取りの応答を次の読取りが取りこぼさない。"""
    state = tmp_path / "state"
    with _group_guard(state):
        browser = _browser(tmp_path, "slow", ready_timeout=5)
        try:
            stdin = browser._process.stdin
            stdin.write(json.dumps({"id": 1, "op": "probe"}) + "\n")
            stdin.flush()
            with pytest.raises(runner.BridgeError, match="bridge response timeout"):
                browser._readline(0.2)
            assert browser._readline(5) == {"id": 1, "ok": True, "result": "probe"}
            assert browser._pending is None  # 引き継いだ1本が完了し、新しい読取りは無い
        finally:
            browser.close()
        _assert_released(state, tmp_path / "bridge.log")


# -- Groupの外へ出た子孫（Chromiumのcrashpad handler相当） ----------------------------


def test_outside_process_using_the_profile_is_awaited_not_signalled(
    tmp_path: Path, signal_audit: SignalAudit
) -> None:
    state = tmp_path / "state"
    with _group_guard(state):
        browser = _browser(tmp_path, "escape-profile-brief", outside_grace=5.0)
        browser.close()  # 自ら終わるのを待って確認できれば成功
        pids = _pids(state)
        assert _wait_dead(pids["escaped"], timeout=0.1)
        assert all(entry[1] != pids["escaped"] for entry in signal_audit.delivered)
        _assert_released(state, tmp_path / "bridge.log")


def test_outside_process_that_stays_is_reported_and_left_unsignalled(
    tmp_path: Path, signal_audit: SignalAudit
) -> None:
    state = tmp_path / "state"
    with _group_guard(state):
        browser = _browser(tmp_path, "escape-profile", outside_grace=0.5)
        with pytest.raises(runner.ProcessCleanupUnconfirmed) as raised:
            browser.close()
        pids = _pids(state)
        assert raised.value.remaining == [pids["escaped"]]
        assert "outside the group still use the browser profile" in str(raised.value)
        assert _reaped(pids["child"])
        assert _wait_dead(pids["grandchild"])
        # 所有を保証できないので止めていない（身元を確かめた試験側の後始末で止める）。
        assert _state(pids["escaped"]) not in (None, "Z", "X")
        assert all(entry[1] != pids["escaped"] for entry in signal_audit.delivered)
        assert str(tmp_path / "bridge.log") not in _open_paths()


def test_main_outside_process_that_stays_exits_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "out" / "profile"
    with _group_guard(state):
        code, document = _run_main(
            tmp_path, monkeypatch, bridge_mode="escape-profile", scenarios={"fake": _passing}
        )
        escaped = _pids(state)["escaped"]
    assert code == 4
    assert document["overall"] == "CLEANUP_UNCONFIRMED"
    [failure] = document["process_cleanup"]["failures"]
    assert failure["label"] == "browser.close"
    assert failure["type"] == "ProcessCleanupUnconfirmed"
    assert failure["remaining"] == [escaped]


def test_outside_check_ignores_unrelated_processes_that_mention_the_profile(
    tmp_path: Path,
) -> None:
    """引数にprofile Pathを含むだけの無関係なProcess（利用者のShell等）を取り違えない。"""
    profile = tmp_path / "profile"
    unrelated = subprocess.Popen(  # noqa: S603 - fixed interpreter, synthetic argument
        [sys.executable, "-c", "import time; time.sleep(30)", str(profile)],
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while str(profile).encode() not in Path(f"/proc/{unrelated.pid}/cmdline").read_bytes():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        # Chromium（補助Process）のDirectoryの実行体でなければ数えない。
        assert runner.processes_referencing(profile, tmp_path / "chromium-dir") == []
        # 同じ判定で、実行体のDirectoryが一致すれば数える（検出そのものは働く）。
        assert runner.processes_referencing(profile, _PYTHON_DIR) == [unrelated.pid]
    finally:
        _RAW_KILL(unrelated.pid, signal.SIGKILL)
        unrelated.wait(timeout=10)


# -- /proc の読取り失敗: 消滅・権限不足・I/O障害・壊れたstat ---------------------------

_OUR_UID = os.getuid()
_OTHER_UID = _OUR_UID + 1000


def _status(*uids: int) -> bytes:
    return ("Name:\tx\nUid:\t" + "\t".join(str(uid) for uid in uids) + "\n").encode()


def _stat(pid: int, state: str = "S", pgid: int = 1) -> bytes:
    return f"{pid} (name with) spaces) {state} 1 {pgid} {pgid} 0".encode()


class FakeProc:
    """疑似の /proc。値が例外ならその読取りで投げる。`present` はディレクトリの有無。"""

    def __init__(self, table: dict[int, dict[str, Any]]) -> None:
        self.table = table

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runner, "_proc_list", lambda: [*map(str, self.table), "self"])
        monkeypatch.setattr(runner, "_proc_read", self.read)
        monkeypatch.setattr(runner, "_proc_readlink", self.readlink)
        monkeypatch.setattr(runner, "_proc_present", self.present)

    def _value(self, pid: str, name: str) -> Any:
        value = self.table[int(pid)].get(name)
        if isinstance(value, BaseException):
            raise value
        if value is None:
            raise FileNotFoundError(2, "No such file", f"/proc/{pid}/{name}")
        return value

    def read(self, pid: str, name: str) -> bytes:
        value: bytes = self._value(pid, name)
        return value

    def readlink(self, pid: str, name: str) -> str:
        value: str = self._value(pid, name)
        return value

    def present(self, pid: str) -> bool:
        value = self.table[int(pid)].get("present", True)
        if isinstance(value, BaseException):
            raise value
        return bool(value)


def _eio(name: str) -> OSError:
    return OSError(5, "Input/output error", name)


def _enoent(name: str) -> FileNotFoundError:
    return FileNotFoundError(2, "No such file or directory", name)


PGID = 777


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        # 読取り中に消滅（ENOENT/ESRCH、かつ /proc/<pid> も無い）→ 対象外。判定不能ではない。
        ({"stat": _enoent("stat"), "present": False}, None),
        ({"stat": ProcessLookupError(3, "No such process"), "present": False}, None),
        ({"stat": b"", "present": False}, None),
        # 在るのに読めない、存在そのものが分からない → 判定不能。
        ({"stat": _enoent("stat"), "present": True}, "MISSING_WHILE_PRESENT"),
        ({"stat": _enoent("stat"), "present": PermissionError(13, "denied")}, "PRESENCE_UNKNOWN"),
        # 権限不足: 他の利用者と**読めて**確認できれば対象外。できなければ判定不能。
        ({"stat": PermissionError(13, "denied"), "status": _status(*[_OTHER_UID] * 4)}, None),
        (
            {"stat": PermissionError(13, "denied"), "status": PermissionError(13, "denied")},
            "PERMISSION_DENIED",
        ),
        # 実Uidが自分（setuidで実効Uidだけ違う子孫）→ 他の利用者とはみなさない。
        (
            {
                "stat": PermissionError(13, "denied"),
                "status": _status(_OUR_UID, _OTHER_UID, _OTHER_UID, _OTHER_UID),
            },
            "PERMISSION_DENIED",
        ),
        ({"stat": PermissionError(13, "denied"), "status": b"Name:\tx\n"}, "PERMISSION_DENIED"),
        # I/O障害 → 判定不能。
        ({"stat": _eio("stat")}, "IO_ERROR"),
        # 壊れたstat（在るまま）→ 判定不能。
        ({"stat": b"garbage without parenthesis"}, "MALFORMED_STAT"),
        ({"stat": b"12 (x) S 1"}, "MALFORMED_STAT"),
        ({"stat": b"12 (x) S 1 notint 0"}, "MALFORMED_STAT"),
        ({"stat": b""}, "MALFORMED_STAT"),
    ],
)
def test_group_scan_separates_vanished_other_user_and_unreadable(
    monkeypatch: pytest.MonkeyPatch, entry: dict[str, Any], reason: str | None
) -> None:
    table = {
        100: {"stat": _stat(100, "S", PGID)},  # 生存メンバー
        101: {"stat": _stat(101, "Z", PGID)},  # zombie（停止済みと読めた）
        102: {"stat": _stat(102, "S", 1)},  # 別Group
        200: entry,
    }
    FakeProc(table).install(monkeypatch)
    scan = runner.scan_group(PGID)
    assert scan.matches == [100]
    if reason is None:
        assert scan.problems == []
        assert scan.conclusive
    else:
        assert [(problem["pid"], problem["reason"]) for problem in scan.problems] == [(200, reason)]
        assert not scan.conclusive
        with pytest.raises(runner.ProcessCleanupUnconfirmed) as raised:
            runner.live_group_members(PGID)
        assert raised.value.unreadable == scan.problems
    if "status" in entry and reason is None:
        assert scan.other_users == [200]


def test_io_error_detail_keeps_errno(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeProc({5: {"stat": _eio("stat")}}).install(monkeypatch)
    [problem] = runner.scan_group(PGID).problems
    assert problem == {
        "pid": 5,
        "read": "stat",
        "reason": "IO_ERROR",
        "error": "OSError: [Errno 5] Input/output error: 'stat'",
        "errno": 5,
    }


def test_proc_listing_failure_is_unconfirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> list[str]:
        raise PermissionError(13, "denied", "/proc")

    monkeypatch.setattr(runner, "_proc_list", fail)
    with pytest.raises(runner.ProcessCleanupUnconfirmed) as raised:
        runner.scan_group(PGID)
    assert raised.value.unreadable[0]["reason"] == "LIST_FAILED"


HELPERS = Path("/opt/chromium-synthetic")
PROFILE = Path("/nonexistent/gh01-synthetic/profile")  # 疑似 /proc の中だけで使うPath


@pytest.mark.parametrize(
    ("entry", "outcome"),
    [
        # 引数を読めてPathを含まない → 対象外（exeが読めなくても）。
        ({"cmdline": b"bash\0-l\0", "exe": PermissionError(13, "denied")}, "excluded"),
        # 引数が読めない: 他の利用者と確認できれば対象外、できなければ判定不能。
        (
            {"cmdline": PermissionError(13, "denied"), "status": _status(*[_OTHER_UID] * 4)},
            "excluded",
        ),
        (
            {"cmdline": PermissionError(13, "denied"), "status": _status(*[_OUR_UID] * 4)},
            "PERMISSION_DENIED",
        ),
        ({"cmdline": _eio("cmdline")}, "IO_ERROR"),
        ({"cmdline": _enoent("cmdline"), "present": False}, "excluded"),
        # Pathを含む: zombieと読めれば停止済み。exeが読めなければ判定不能。
        ({"cmdline": str(PROFILE).encode(), "stat": _stat(1, "Z")}, "excluded"),
        (
            {
                "cmdline": str(PROFILE).encode(),
                "stat": _stat(1),
                "exe": PermissionError(13, "d"),
                "status": _status(*[_OUR_UID] * 4),
            },
            "PERMISSION_DENIED",
        ),
        (
            {
                "cmdline": str(PROFILE).encode(),
                "stat": _stat(1),
                "exe": _enoent("exe"),
                "present": True,
            },
            "MISSING_WHILE_PRESENT",
        ),
        ({"cmdline": str(PROFILE).encode(), "stat": b"broken"}, "MALFORMED_STAT"),
        ({"cmdline": str(PROFILE).encode(), "stat": _stat(1), "exe": "/usr/bin/vi"}, "excluded"),
        (
            {
                "cmdline": str(PROFILE).encode(),
                "stat": _stat(1),
                "exe": str(HELPERS / "chrome_crashpad_handler"),
            },
            "match",
        ),
    ],
)
def test_referencing_scan_does_not_treat_unreadable_as_ended(
    monkeypatch: pytest.MonkeyPatch, entry: dict[str, Any], outcome: str
) -> None:
    FakeProc({300: entry}).install(monkeypatch)
    scan = runner.scan_referencing(PROFILE, HELPERS)
    if outcome == "excluded":
        assert (scan.matches, scan.problems) == ([], [])
    elif outcome == "match":
        assert (scan.matches, scan.problems) == ([300], [])
    else:
        assert scan.matches == []
        assert [problem["reason"] for problem in scan.problems] == [outcome]
        with pytest.raises(runner.ProcessCleanupUnconfirmed):
            runner.processes_referencing(PROFILE, HELPERS)


def _inject_read_failure(
    monkeypatch: pytest.MonkeyPatch, target: int, name: str, error: OSError
) -> None:
    """実際の /proc を読みつつ、`target` の `name` だけを失敗させる。"""
    real = runner._proc_read

    def read(pid: str, what: str) -> bytes:
        if int(pid) == target and what == name:
            raise error
        return real(pid, what)

    monkeypatch.setattr(runner, "_proc_read", read)


def test_group_stop_with_an_unreadable_member_keeps_the_leader_and_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signal_audit: SignalAudit
) -> None:
    """読めないProcessが1つでもあれば停止を確認済みにしない（回収せず所有を保つ）。"""
    group, state = _leader(tmp_path, "run")
    with _group_guard(state):
        pids = _pids(state)
        bystander = subprocess.Popen(  # noqa: S603 - fixed interpreter, synthetic
            [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
        )
        try:
            real = runner._proc_read
            _inject_read_failure(monkeypatch, bystander.pid, "stat", _eio("stat"))
            with pytest.raises(runner.ProcessCleanupUnconfirmed) as raised:
                group.stop(interrupt=None, grace=0.1, kill_timeout=0.5)
            assert raised.value.remaining == []  # 自分のGroupは空と読めた
            assert [(u["pid"], u["reason"]) for u in raised.value.unreadable] == [
                (bystander.pid, "IO_ERROR")
            ]
            assert group.owned and _unreaped_child(pids["child"])
            # 無関係なProcess（所有していない）へは送っていない。
            assert all(entry[1] == pids["child"] for entry in signal_audit.delivered)
            monkeypatch.setattr(runner, "_proc_read", real)
            assert group.stop(interrupt=None, grace=0.1) == -signal.SIGKILL
            assert _reaped(pids["child"])
        finally:
            _RAW_KILL(bystander.pid, signal.SIGKILL)
            bystander.wait(timeout=10)


@pytest.mark.parametrize(
    ("name", "error", "reason"),
    [
        ("cmdline", PermissionError(13, "Permission denied"), "PERMISSION_DENIED"),
        ("cmdline", OSError(5, "Input/output error"), "IO_ERROR"),
    ],
)
def test_main_unreadable_outside_process_is_cleanup_unconfirmed_in_the_final_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, error: OSError, reason: str
) -> None:
    bystander = subprocess.Popen(  # noqa: S603 - fixed interpreter, synthetic
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    try:
        _inject_read_failure(monkeypatch, bystander.pid, name, error)
        with _group_guard(tmp_path / "out" / "profile"):
            code, document = _run_main(tmp_path, monkeypatch, scenarios={"fake": _passing})
    finally:
        _RAW_KILL(bystander.pid, signal.SIGKILL)
        bystander.wait(timeout=10)
    assert code == runner.EXIT_CLEANUP_UNCONFIRMED == 4
    assert document["overall"] == "CLEANUP_UNCONFIRMED"
    assert [check["status"] for check in document["checks"]] == ["PASS", "PASS"]
    [failure] = document["process_cleanup"]["failures"]
    assert failure["label"] == "browser.close"
    assert failure["remaining"] == []
    assert [(u["pid"], u["read"], u["reason"]) for u in failure["unreadable"]] == [
        (bystander.pid, name, reason)
    ]
    _assert_released(tmp_path / "out" / "profile", tmp_path / "out" / "bridge.log")


def test_main_unreadable_group_member_is_cleanup_unconfirmed_in_the_final_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """起動失敗の後始末でGroupの停止を読めなかった場合も、4と記録の両方が残る。"""
    bystander = subprocess.Popen(  # noqa: S603 - fixed interpreter, synthetic
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    real_stop = runner.OwnedProcessGroup.stop

    def quick_stop(self: Any, **kwargs: Any) -> Any:
        kwargs["kill_timeout"] = 0.5
        return real_stop(self, **kwargs)

    monkeypatch.setattr(runner.OwnedProcessGroup, "stop", quick_stop)
    try:
        _inject_read_failure(monkeypatch, bystander.pid, "stat", _eio("stat"))
        with _group_guard(tmp_path / "out" / "profile"):
            code, document = _run_main(tmp_path, monkeypatch, bridge_mode="fatal")
    finally:
        _RAW_KILL(bystander.pid, signal.SIGKILL)
        bystander.wait(timeout=10)
    assert code == 4
    assert document["overall"] == "CLEANUP_UNCONFIRMED"
    assert document["start_failure"]["message"] == "CHROMIUM_START_TIMEOUT"
    assert document["process_cleanup"]["status"] == "UNCONFIRMED"
    [failure] = document["process_cleanup"]["failures"]
    assert failure["type"] == "ProcessCleanupUnconfirmed"
    assert [(u["pid"], u["reason"]) for u in failure["unreadable"]] == [(bystander.pid, "IO_ERROR")]
