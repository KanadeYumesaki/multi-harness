#!/usr/bin/env python3
"""GH-01: ローカルUIの「受付と復旧」とWorkbenchを実ブラウザーで検査する専用Runner。

## 何を検査するか

* 受付と復旧: **本番のCLI入口** `python -m harness.presentation.cli ui` を子Processで起動し、
  合成したSQLite DB／CASへ接続する。画面は実Chromiumで開き、マウス・キーボードで操作する。
  各操作の後にHTTP応答・DB（ConnectionFactory経由の読取り）・CAS・監査Receiptを突き合わせる。
* Workbench: 本番の `build_services` / `LocalUiApi` / `start_server` を同一Processで組み、
  偽CLI（実Process）と境界測定の代役（tests/support/workbench_fixtures.StubBoundaryProbe）
  を使う。**実Providerは呼ばない。** この経路は「実browser + 本番HTTP + 境界fixture」と
  ラベルを分けて記録する。

## 何をしないか

* 実Provider・外部Network・利用者のDB/CAS/Workspaceに触れない。出力先は新規Directoryだけ。
* 本番のFilesystem境界・承認・Origin/Session検査を無効化しない。検査を通すためのFlagを持たない。
* ブラウザーや実行体が無い環境で成功を名乗らない。検査は `UNVERIFIED` とし終了Code 3 を返す。
* ここでのPASSは開発時の回帰確認であり、Runtime GO・正式WSL証跡・Release Evidenceではない。

## 終了Code

0: 全検査PASS / 1: 1件以上FAIL / 2: 引数・出力先の誤り / 3: 実行環境不足（全件UNVERIFIED）/
4: 起動したProcessの停止・回収を確認できなかった（CLEANUP_UNCONFIRMED。0/1/3より優先）

`result.json` の `process_cleanup` は、起動したProcess Groupの停止・回収の結果を
`CONFIRMED` / `UNCONFIRMED` / `NOT_STARTED` と失敗の内容で残す。ブラウザーの起動に
失敗した場合は `start_failure` に元の失敗を、`process_cleanup` に後始末の結果を分けて残す。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
BRIDGE = REPO / "tools" / "browser_cdp_bridge.cjs"
RUNNER_CONTRACT = "gh01-ui-browser-check/1"
STAMP = "2026-09-24T00:00:00Z"
XSS_TEXT = '<img src=x onerror="window.__gh01_xss=1"><script>window.__gh01_xss=2</script>'
TRAVERSAL_TEXT = "../../../../etc/passwd"
EXIT_PASS, EXIT_FAIL, EXIT_USAGE, EXIT_UNAVAILABLE = 0, 1, 2, 3
EXIT_CLEANUP_UNCONFIRMED = 4
PATH_RECOVERY = "REAL_BROWSER+PRODUCTION_CLI_HTTP+SYNTHETIC_DB_CAS"
PATH_WORKBENCH = "REAL_BROWSER+PRODUCTION_HTTP+FAKE_CLI+STUB_BOUNDARY_PROBE"
PATH_CLI_ONLY = "PRODUCTION_CLI_ENTRY(no browser)"
PATH_HTTP_ONLY = "PRODUCTION_CLI_HTTP(no browser)"

ALL_CHECKS: tuple[tuple[str, str], ...] = (
    ("B-13", "用途選択・ファイル不要の文章生成・保存・会話引継ぎ"),
    ("B-12", "最近の履歴の明示選択と重要事項の保持"),
    ("B-11", "公式ChatGPTログイン・HTTP生成の統制を合成応答で確認"),
    ("B-01", "明示operator sessionなしは参照専用。画面・直接POSTとも作用なし"),
    ("B-02", "無効なoperator sessionは起動時に拒否し保存先を作らない"),
    ("B-03", "整理はチェック必須。Receipt保存・DRAINING維持・失敗コピー保持・Provider起動0"),
    ("B-04", "再開は新しいreviewと別承認だけ。整理時のhash/承認の流用を拒否"),
    ("B-05", "拒否状態では理由を表示し、操作を有効化しない"),
    ("B-06", "Origin不一致・sessionなし・未知field・古いhashをサーバーが拒否し保存状態不変"),
    ("B-07", "二重クリック・複数タブ・遅延/切断で重複効果なし、自動再送なし"),
    ("B-08", "XSS/トラバーサル/長文をHTMLとして解釈せず、秘密値を画面・ログへ出さない"),
    ("B-09", "WorkbenchのProvider/モデル/推論値・payload確認・送信/差分承認・順次編集"),
    ("B-10", "console error・狭幅・操作後focus・拒否理由の表示・スクリーンショット"),
)


# -- 結果の記録 ---------------------------------------------------------------


@dataclass
class Check:
    check_id: str
    name: str
    path: str
    status: str
    details: dict[str, Any]


@dataclass
class Report:
    out: Path
    checks: list[Check] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    #: 停止・回収を確認できなかった後始末。1件でもあれば全体は CLEANUP_UNCONFIRMED。
    cleanup_failures: list[dict[str, Any]] = field(default_factory=list)

    def record(self, check_id: str, name: str, path: str, ok: bool, **details: Any) -> bool:
        self.checks.append(Check(check_id, name, path, "PASS" if ok else "FAIL", details))
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {check_id} {name}", flush=True)
        return ok

    def error(self, check_id: str, name: str, path: str, error: BaseException) -> None:
        # 例外は握り潰さない。FAILとして種別と位置を残し、全体の終了Codeへ反映する。
        details: dict[str, Any] = {
            **describe_error(error),
            "exception": type(error).__name__,
            "trace": traceback.format_exc(limit=6)[-2000:],
        }
        outcome = cleanup_outcome(error)
        if outcome is not None:
            details["cleanup"] = outcome
            if outcome["status"] == "UNCONFIRMED":
                for failure in outcome["failures"]:
                    self.cleanup_failures.append({"label": f"{check_id} {name}", **failure})
        if isinstance(error, ProcessCleanupUnconfirmed):
            self.cleanup_failures.append({"label": f"{check_id} {name}", **describe_error(error)})
        self.checks.append(Check(check_id, name, path, "FAIL", details))
        print(f"[FAIL] {check_id} {name}: {type(error).__name__}: {error}", flush=True)

    def release(self, label: str, action: Callable[[], Any]) -> Any:
        """後始末を実行する。失敗は構造化して残し、全体を CLEANUP_UNCONFIRMED にする。

        検査の結果や先に起きた失敗を上書きしないよう、通常の例外は投げ直さない。
        KeyboardInterrupt等は記録してから投げ直す。
        """
        try:
            return action()
        except Exception as error:
            self._cleanup_failed(label, error)
            return None
        except BaseException as error:
            self._cleanup_failed(label, error)
            raise

    def _cleanup_failed(self, label: str, error: BaseException) -> None:
        self.cleanup_failures.append({"label": label, **describe_error(error)})
        print(f"[CLEANUP_UNCONFIRMED] {label}: {type(error).__name__}: {error}", flush=True)


# -- 子Processの所有と後始末 -------------------------------------------------------


class ProcessCleanupUnconfirmed(RuntimeError):
    """起動したProcess Groupの停止・回収を確認できなかった。**環境不足とは別の状態。**

    `unreadable` は、停止したかどうかを判定できなかった `/proc` の読取り（権限不足・
    I/O障害・壊れたstat等）。読めなかったことを「停止済み」「対象外」の根拠にしない。
    """

    def __init__(
        self,
        message: str,
        *,
        pgid: int,
        remaining: list[int],
        unreadable: list[dict[str, Any]] | None = None,
    ) -> None:
        self.unreadable = list(unreadable or [])
        suffix = f", unreadable={len(self.unreadable)}" if self.unreadable else ""
        super().__init__(f"{message} (pgid={pgid}, remaining={remaining}{suffix})")
        self.pgid = pgid
        self.remaining = remaining


# -- /proc の読取り（観測だけ。signalは送らない） -------------------------------------
#
# 読取りの失敗を次の3つに分ける。
#
# * VANISHED: `/proc/<pid>` そのものが無くなった（読む間に終わって回収された）。対象外。
# * 他の利用者と確認できた: `status` の Uid（実・実効・保存・FS）がどれも自分と違う。対象外。
#   読めないことだけでは他の利用者とみなさない。
# * それ以外（権限不足・I/O障害・壊れたstat・在るのに読めない）: **判定不能。**
#   停止を確認できなかったものとして記録し、呼出し元は回収未確認（終了Code 4）にする。


def _proc_list() -> list[str]:
    return os.listdir("/proc")


def _proc_read(pid: str, name: str) -> bytes:
    return Path(f"/proc/{pid}/{name}").read_bytes()


def _proc_readlink(pid: str, name: str) -> str:
    return os.readlink(f"/proc/{pid}/{name}")


def _proc_present(pid: str) -> bool:
    """`/proc/<pid>` が在るか。無いことが確かなときだけFalse。他の失敗はそのまま投げる。"""
    try:
        os.stat(f"/proc/{pid}")
    except FileNotFoundError:
        return False
    return True


_VANISHED = "VANISHED"


@dataclass
class ProcScan:
    """1回の走査結果。`problems` が空でなければ、この走査では停止を確認できない。"""

    matches: list[int] = field(default_factory=list)
    problems: list[dict[str, Any]] = field(default_factory=list)
    other_users: list[int] = field(default_factory=list)

    @property
    def conclusive(self) -> bool:
        return not self.problems


def _problem(pid: str, what: str, reason: str, error: BaseException | None) -> dict[str, Any]:
    record: dict[str, Any] = {"pid": int(pid), "read": what, "reason": reason}
    if error is not None:
        record["error"] = f"{type(error).__name__}: {str(error)[:200]}"
        errno = getattr(error, "errno", None)
        if errno is not None:
            record["errno"] = errno
    return record


def _classify(pid: str, what: str, error: OSError) -> str | dict[str, Any]:
    """読取りの失敗を VANISHED か、判定不能の記録かに分ける。"""
    if isinstance(error, FileNotFoundError | ProcessLookupError):
        try:
            present = _proc_present(pid)
        except OSError as again:
            return _problem(pid, what, "PRESENCE_UNKNOWN", again)
        # 無くなっていれば消滅。在るのに読めない（kernel threadのexe等）は判定不能。
        return _VANISHED if not present else _problem(pid, what, "MISSING_WHILE_PRESENT", error)
    if isinstance(error, PermissionError):
        return _problem(pid, what, "PERMISSION_DENIED", error)
    return _problem(pid, what, "IO_ERROR", error)


def _parse_stat(raw: bytes) -> tuple[str, int]:
    """`/proc/<pid>/stat` から (状態, PGID)。形が崩れていれば ValueError。"""
    text = raw.decode("utf-8", errors="replace")
    if ")" not in text:
        raise ValueError("no closing parenthesis in stat")
    fields = text.rsplit(")", 1)[1].split()
    if len(fields) < 3 or len(fields[0]) != 1:
        raise ValueError("too few fields in stat")
    return fields[0], int(fields[2])


def _other_user(pid: str) -> bool:
    """実・実効・保存・FSのUidがどれも自分と違うと**読めて**確認できたときだけTrue。"""
    try:
        status = _proc_read(pid, "status").decode("utf-8", errors="replace")
    except OSError:
        return False
    for line in status.splitlines():
        if line.startswith("Uid:"):
            try:
                uids = {int(value) for value in line.split()[1:5]}
            except ValueError:
                return False
            return len(uids) > 0 and os.getuid() not in uids
    return False


def _record_failure(pid: str, what: str, error: OSError, scan: ProcScan) -> None:
    """読取りの失敗を、消滅（何もしない）・他の利用者（対象外）・判定不能に振り分ける。"""
    outcome = _classify(pid, what, error)
    if isinstance(outcome, str):  # _VANISHED
        return
    if isinstance(error, PermissionError) and _other_user(pid):
        scan.other_users.append(int(pid))
        return
    scan.problems.append(outcome)


def _read_stat(pid: str, scan: ProcScan) -> tuple[str, int] | None:
    """状態とPGID。対象外・消滅・判定不能ならNone（判定不能は scan へ記録）。"""
    try:
        raw = _proc_read(pid, "stat")
    except OSError as error:
        _record_failure(pid, "stat", error, scan)
        return None
    try:
        return _parse_stat(raw)
    except ValueError as error:
        # 読む間に終わると空や途中で切れた内容になり得る。消滅を確かめてから判定不能にする。
        try:
            present = _proc_present(pid)
        except OSError as again:
            scan.problems.append(_problem(pid, "stat", "PRESENCE_UNKNOWN", again))
            return None
        if present:
            scan.problems.append(_problem(pid, "stat", "MALFORMED_STAT", error))
        return None


def _list_proc(pgid: int) -> list[str]:
    try:
        return [entry for entry in _proc_list() if entry.isdigit()]
    except OSError as error:
        raise ProcessCleanupUnconfirmed(
            "cannot enumerate /proc to confirm processes stopped",
            pgid=pgid,
            remaining=[],
            unreadable=[
                {
                    "pid": 0,
                    "read": "/proc",
                    "reason": "LIST_FAILED",
                    "error": f"{type(error).__name__}: {error}",
                }
            ],
        ) from error


def scan_group(pgid: int) -> ProcScan:
    """PGIDに属し、まだ止まっていない（zombieでない）Process。判定不能は `problems` へ。"""
    scan = ProcScan()
    for pid in _list_proc(pgid):
        observed = _read_stat(pid, scan)
        if observed is None:
            continue
        state, group = observed
        if group == pgid and state not in ("Z", "X"):
            scan.matches.append(int(pid))
    scan.matches.sort()
    return scan


def scan_referencing(path: Path, executable_dir: Path, *, pgid: int = 0) -> ProcScan:
    """`executable_dir` の実行体で、引数に `path` を含み、まだ止まっていないProcess。

    Process Groupの外へ出た子孫（例: Chromiumのcrashpad handlerはsetsidする）には、
    Group宛ての停止も `scan_group()` も届かない。身元は「Chromiumと同じDirectoryの実行体」
    かつ「起動時に渡した一意のprofile Pathを引数に持つ」の両方で確かめる。引数を**読めて**
    Pathを含まないと分かったものだけを対象外にする。
    """
    needle = str(path).encode()
    scan = ProcScan()
    for pid in _list_proc(pgid):
        if int(pid) == os.getpid():
            continue
        try:
            cmdline = _proc_read(pid, "cmdline")
        except OSError as error:
            _record_failure(pid, "cmdline", error, scan)
            continue
        if needle not in cmdline:
            continue  # 読めて、Pathを含まないと確認できた
        observed = _read_stat(pid, scan)
        if observed is None:
            continue
        if observed[0] in ("Z", "X"):
            continue  # 終了済み（zombie）と読めて確認できた
        try:
            executable = Path(_proc_readlink(pid, "exe"))
        except OSError as error:
            _record_failure(pid, "exe", error, scan)
            continue
        if executable.parent == executable_dir:
            scan.matches.append(int(pid))
    scan.matches.sort()
    return scan


def live_group_members(pgid: int) -> list[int]:
    """PGIDの生存メンバー。判定不能な読取りがあれば `ProcessCleanupUnconfirmed`。"""
    scan = scan_group(pgid)
    if not scan.conclusive:
        raise ProcessCleanupUnconfirmed(
            "cannot confirm the group stopped: /proc could not be read",
            pgid=pgid,
            remaining=scan.matches,
            unreadable=scan.problems,
        )
    return scan.matches


def processes_referencing(path: Path, executable_dir: Path) -> list[int]:
    """`scan_referencing()` の一覧。判定不能な読取りがあれば `ProcessCleanupUnconfirmed`。"""
    scan = scan_referencing(path, executable_dir)
    if not scan.conclusive:
        raise ProcessCleanupUnconfirmed(
            "cannot confirm processes using the profile ended: /proc could not be read",
            pgid=0,
            remaining=scan.matches,
            unreadable=scan.problems,
        )
    return scan.matches


class OwnedProcessGroup:
    """`start_new_session=True` で起動した子（Group leader）と、そのProcess Group。

    ## 所有を保証できる間だけsignalを送る

    子を回収（wait）するまで、子のPIDは（zombieでも）再利用されない。だから**未回収の
    間だけ**、子のPID宛て・PGID（= 子のPID）宛てのsignalが自分のGroupにしか届かないと
    保証できる。回収した後は同じ番号が別のProcessやGroupに使われ得るので、一切送らない。

    * 終了は `waitid(WEXITED | WNOHANG | WNOWAIT)` で観測し、回収しない。
      `Popen.poll()` / `wait()` / `send_signal()`（内部でpollする）は回収してしまうので
      使わない。回収は `stop()` の最後の1回だけ。
    * 子が先に終わっても、zombieの間にGroup全体へSIGKILLして残った子孫を止める。
    * 子孫が止まったことは `/proc` のPGIDで確かめる。確かめられなければ回収せず
      （Groupを宛先にできる状態を保ったまま）`ProcessCleanupUnconfirmed` を投げる。
    * 回収済み（2回目のstop・他所での回収）なら何も送らない。子孫が残っていれば
      止めずに `ProcessCleanupUnconfirmed` を投げる。
    """

    def __init__(self, process: subprocess.Popen[Any]) -> None:
        self.process = process
        self.pgid = process.pid
        self._foreign_reap = False
        self._observed_code: int | None = None

    @property
    def owned(self) -> bool:
        """未回収で、PID/PGIDへのsignalが自分のGroupにしか届かないこと。"""
        return self.process.returncode is None and not self._foreign_reap

    def exited(self) -> bool:
        """子が終了したか。**回収しない。**"""
        if not self.owned:
            return True
        try:
            result = os.waitid(os.P_PID, self.pgid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            # 自分の知らない所で回収された。以後は番号の所有を保証できない。
            self._foreign_reap = True
            return True
        if result is None:
            return False
        exited_normally = result.si_code == os.CLD_EXITED
        self._observed_code = result.si_status if exited_normally else -result.si_status
        return True

    def exit_code(self) -> int | None:
        """終了値（signal終了は負値）。未終了・不明はNone。回収しない。"""
        if self.process.returncode is not None:
            return self.process.returncode
        return self._observed_code if self.exited() else None

    def wait_exited(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not self.exited():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    def stop(self, *, interrupt: int | None, grace: float, kill_timeout: float = 10.0) -> int:
        """止め、子孫の停止を確かめてから1回だけ回収する。何度呼んでもよい。"""
        # 他所での回収（ECHILD）を先に検出する。以後の判断はこの観測に基づく。
        self.exited()
        if not self.owned:
            scan = scan_group(self.pgid)
            if scan.matches or not scan.conclusive:
                raise ProcessCleanupUnconfirmed(
                    "leader already reaped; remaining group members were not signalled",
                    pgid=self.pgid,
                    remaining=scan.matches,
                    unreadable=scan.problems,
                )
            return self._returncode()
        if interrupt is not None and not self.exited():
            # 正常な終了経路（本番CLIのSIGINT等）を先に試す。
            self._send(interrupt, group=False)
            self.wait_exited(grace)
        scan = self._kill_group_until_empty(kill_timeout)
        if scan.matches or not scan.conclusive or not self.exited():
            raise ProcessCleanupUnconfirmed(
                "group stop not confirmed after SIGKILL; leader kept unreaped",
                pgid=self.pgid,
                remaining=scan.matches,
                unreadable=scan.problems,
            )
        self.process.wait()  # 最後に1回だけ回収する
        return self._returncode()

    def _send(self, sig: int, *, group: bool) -> None:
        """**送る直前に**所有を確かめる。失っていれば送らずに回収未確認とする。"""
        self.exited()  # 他所での回収を検出する
        if not self.owned:
            scan = scan_group(self.pgid)
            raise ProcessCleanupUnconfirmed(
                "leader was reaped elsewhere; signal not sent",
                pgid=self.pgid,
                remaining=scan.matches,
                unreadable=scan.problems,
            )
        if group:
            os.killpg(self.pgid, sig)
        else:
            os.kill(self.pgid, sig)

    def _kill_group_until_empty(self, timeout: float) -> ProcScan:
        """Groupが空になったと**読めて確認できる**まで止め続ける。期限で最後の走査を返す。"""
        deadline = time.monotonic() + timeout
        while True:
            # 子がzombieでも未回収ならPGIDは自分のもの。所有は _send が毎回確かめる。
            with contextlib.suppress(ProcessLookupError):  # 送る相手がもう居ない
                self._send(signal.SIGKILL, group=True)
            scan = scan_group(self.pgid)
            if (scan.conclusive and not scan.matches) or time.monotonic() >= deadline:
                return scan
            time.sleep(0.05)

    def _returncode(self) -> int:
        code = self.process.returncode
        return int(code) if code is not None else -1


CLEANUP_ATTRIBUTE = "gh01_cleanup"


def describe_error(error: BaseException) -> dict[str, Any]:
    """例外を構造化して残す（型・文言・注記・原因・未回収のProcess）。"""
    record: dict[str, Any] = {"type": type(error).__name__, "message": str(error)[:500]}
    notes = getattr(error, "__notes__", None)
    if notes:
        record["notes"] = [str(note)[:500] for note in notes]
    if isinstance(error, ProcessCleanupUnconfirmed):
        record["pgid"] = error.pgid
        record["remaining"] = error.remaining
        if error.unreadable:
            record["unreadable"] = error.unreadable
    if error.__cause__ is not None:
        record["cause"] = {
            "type": type(error.__cause__).__name__,
            "message": str(error.__cause__)[:500],
        }
    return record


def cleanup_outcome(error: BaseException) -> dict[str, Any] | None:
    """起動失敗の例外に付けた後始末の結果。付いていなければNone。"""
    value = getattr(error, CLEANUP_ATTRIBUTE, None)
    return value if isinstance(value, dict) else None


def _set_cleanup(error: BaseException, status: str, failures: list[dict[str, Any]]) -> None:
    previous = cleanup_outcome(error)
    if previous is not None:
        failures = [*previous["failures"], *failures]
        if previous["status"] == "UNCONFIRMED":
            status = "UNCONFIRMED"
    setattr(error, CLEANUP_ATTRIBUTE, {"status": status, "failures": failures})


def mark_not_started(error: BaseException) -> None:
    """Processを起動する前に失敗した（後始末の対象が無い）ことを記録する。"""
    _set_cleanup(error, "NOT_STARTED", [])


def cleanup_after_failure(error: BaseException, cleanup: Callable[[], Any]) -> None:
    """失敗の後始末をする。**元の失敗を上書きしない。**

    後始末の結果は元の例外へ構造化して付ける（`cleanup_outcome()`）。成功なら
    CONFIRMED、失敗ならUNCONFIRMEDと失敗の内容。Tracebackにも見えるよう注記も残す。
    """
    try:
        cleanup()
    except BaseException as secondary:
        error.add_note(f"cleanup failed: {type(secondary).__name__}: {secondary}")
        _set_cleanup(error, "UNCONFIRMED", [describe_error(secondary)])
    else:
        _set_cleanup(error, "CONFIRMED", [])


# -- ブラウザー（CDPブリッジ） ----------------------------------------------------


class BridgeError(RuntimeError):
    pass


class Browser:
    """tools/browser_cdp_bridge.cjs と1行JSONで話す。"""

    def __init__(
        self,
        node: str,
        chromium: str,
        profile: Path,
        log: Path,
        *,
        bridge: Path = BRIDGE,
        ready_timeout: float = 60.0,
        launch_timeout_ms: int = 20000,
        stop_grace: float = 15.0,
        outside_grace: float = 10.0,
        helper_dir: Path | None = None,
    ) -> None:
        flags: list[str] = []
        if os.geteuid() == 0:
            # rootではChromiumのSandboxが起動を拒む。対象は127.0.0.1の自前画面と合成値だけ。
            flags.append("--no-sandbox")
        self._stop_grace = stop_grace
        self._profile = profile
        self._outside_grace = outside_grace
        #: Groupの外へ出る補助Process（crashpad handler等）の実行体があるDirectory。
        self._helper_dir = helper_dir or Path(os.path.realpath(chromium)).parent
        #: 読取り中のThreadと結果。**同時に1本だけ。** timeoutで未完了なら次の呼出しが引き継ぐ
        #: （2本がstdoutを取り合うと、応答の取り違えやcloseでの停止が起きる）。
        self._pending: tuple[threading.Thread, dict[str, Any], threading.Event] | None = None
        self._stderr = log.open("wb")
        try:
            self._process = subprocess.Popen(  # noqa: S603 - 明示された実行体とargv list
                [node, str(bridge), chromium, str(profile), str(launch_timeout_ms), *flags],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr,
                text=True,
                encoding="utf-8",
                # Bridgeと、Bridgeが起動するChromiumを1つのGroupにまとめて確実に止める。
                start_new_session=True,
            )
        except BaseException as error:
            self._stderr.close()
            mark_not_started(error)
            raise
        self._group = OwnedProcessGroup(self._process)
        self._next = 0
        try:
            first = self._readline(ready_timeout)
            if first.get("fatal"):
                raise BridgeError(str(first["fatal"]))
            if not first.get("ready"):
                raise BridgeError("bridge did not become ready")
        except BaseException as error:
            # timeout・fatal・不正出力のどれでも、Bridge/Chromium/Pipe/Logを手放してから
            # 元の失敗を投げ直す。後始末の結果は例外へ構造化して付く。
            cleanup_after_failure(error, self._release)
            raise

    def _release(self) -> None:
        """Bridge・Chromium・Pipe・Logを手放す。何度呼んでもよい。"""
        try:
            self._group.stop(interrupt=signal.SIGTERM, grace=self._stop_grace)
            self._confirm_outside_processes_ended()
        except BaseException:
            self._close_streams(strict=False)
            raise
        self._close_streams(strict=True)

    def _confirm_outside_processes_ended(self) -> None:
        """Groupの外でprofileを使うProcessが終わったことを確かめる。送信はしない。

        所有を保証できないので止めない。自ら終わるのを待ち、残れば回収未確認とする。
        """
        deadline = time.monotonic() + self._outside_grace
        pgid = self._group.pgid
        scan = scan_referencing(self._profile, self._helper_dir, pgid=pgid)
        while (scan.matches or not scan.conclusive) and time.monotonic() < deadline:
            time.sleep(0.1)
            scan = scan_referencing(self._profile, self._helper_dir, pgid=pgid)
        if scan.matches or not scan.conclusive:
            raise ProcessCleanupUnconfirmed(
                "processes outside the group still use the browser profile or could not be"
                " read; not signalled",
                pgid=pgid,
                remaining=scan.matches,
                unreadable=scan.problems,
            )

    def _close_streams(self, *, strict: bool) -> None:
        """Pipeとログを閉じる。

        読取りThreadが `readline()` で止まっている間にstdoutを閉じると、Lockの取り合いで
        こちらも止まる。Groupを止めて書き手が居なくなればEOFで抜けるので、少し待つ。
        それでも抜けなければ、Group外のProcessがPipeを持っている。閉じずに（止まらずに）
        回収未確認として報告する。
        """
        reader = self._pending[0] if self._pending is not None else None
        if reader is not None and reader.is_alive():
            reader.join(timeout=5)
        held = reader is not None and reader.is_alive()
        streams = [self._process.stdin] + ([] if held else [self._process.stdout])
        for stream in streams:
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        self._stderr.close()
        if held and strict:
            raise ProcessCleanupUnconfirmed(
                "bridge stdout is still held open by a process outside the group",
                pgid=self._group.pgid,
                remaining=[],
            )

    def _readline(self, timeout: float) -> dict[str, Any]:
        stdout = self._process.stdout
        if stdout is None:
            raise BridgeError("bridge stdout is not a pipe")
        if self._pending is None:
            result: dict[str, Any] = {}
            done = threading.Event()

            def read() -> None:
                line = stdout.readline()
                result["line"] = line
                done.set()

            reader = threading.Thread(target=read, daemon=True)
            self._pending = (reader, result, done)
            reader.start()
        else:
            _reader, result, done = self._pending
        deadline = time.monotonic() + timeout
        while not done.wait(0.1):
            # Bridgeが先に終わっても、孫がPipeを保持しているとEOFが来ない。終了を
            # timeoutへすり替えず、終了として報告する（最後の1行は少しだけ待つ）。
            # 終了の観測は回収しない（OwnedProcessGroup.exited）。
            if self._group.exited() and not done.wait(0.5):
                raise BridgeError(f"bridge exited rc={self._group.exit_code()} without a response")
            if time.monotonic() >= deadline:
                raise BridgeError("bridge response timeout")
        self._pending = None
        line = result.get("line", "")
        if not line:
            self._group.wait_exited(1.0)
            raise BridgeError(f"bridge exited rc={self._group.exit_code()}")
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as error:
            raise BridgeError("bridge sent a non-JSON line") from error
        if not isinstance(parsed, dict):
            raise BridgeError("bridge sent a non-object line")
        return parsed

    def call(self, op: str, **payload: Any) -> Any:
        stdin = self._process.stdin
        if stdin is None:
            raise BridgeError("bridge stdin is not a pipe")
        self._next += 1
        stdin.write(json.dumps({"id": self._next, "op": op, **payload}) + "\n")
        stdin.flush()
        reply = self._readline(60)
        if reply.get("fatal"):
            raise BridgeError(str(reply["fatal"]))
        if not reply.get("ok"):
            raise BridgeError(f"{op}: {reply.get('error')}")
        return reply.get("result")

    def eval(self, tab: str, expr: str) -> Any:
        return self.call("eval", tab=tab, expr=expr)

    def wait(self, tab: str, expr: str, *, timeout: float = 15.0) -> Any:
        deadline = time.monotonic() + timeout
        value: Any = None
        while time.monotonic() < deadline:
            value = self.eval(tab, expr)
            if value:
                return value
            time.sleep(0.1)
        raise BridgeError(f"condition not met within {timeout}s: {expr[:120]}")

    def close(self) -> None:
        """Bridgeへ終了を頼み、Group全体を止めて回収する。2回目以降は何も送らない。"""
        try:
            if not self._group.exited():
                with contextlib.suppress(BridgeError, OSError):
                    self.call("quit")
                self._group.wait_exited(20)
        except BaseException as error:
            cleanup_after_failure(error, self._release)
            raise
        self._release()


OPS_STATE = """(() => {
  const g = (id) => document.getElementById(id);
  const a = document.activeElement;
  const panel = g("operations-panel");
  return {
    status: g("ops-status").textContent,
    error: g("ops-error").textContent,
    reconcileSummary: g("ops-reconcile-summary").textContent,
    resumeSummary: g("ops-resume-summary").textContent,
    reconcileReview: g("ops-reconcile-review").textContent,
    resumeReview: g("ops-resume-review").textContent,
    result: g("ops-result").textContent,
    reconcileApproveDisabled: g("ops-reconcile-approve").disabled,
    reconcileApproveChecked: g("ops-reconcile-approve").checked,
    reconcileDisabled: g("ops-reconcile").disabled,
    resumeApproveDisabled: g("ops-resume-approve").disabled,
    resumeApproveChecked: g("ops-resume-approve").checked,
    resumeDisabled: g("ops-resume").disabled,
    refreshDisabled: g("ops-refresh").disabled,
    active: a ? (a.id || a.tagName) : null,
    activeInPanel: !!a && panel.contains(a),
    injected: document.querySelectorAll(
      "#operations-panel img, #operations-panel script, #operations-panel iframe").length,
    xss: window.__gh01_xss || null,
    docOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    panelOverflow: panel.scrollWidth - panel.clientWidth,
    viewport: window.innerWidth
  };
})()"""

CONTRAST = """(() => {
  const parse = (c) => (c.match(/[\\d.]+/g) || []).map(Number);
  const lum = (rgb) => {
    const f = (v) => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * f(rgb[0]) + 0.7152 * f(rgb[1]) + 0.0722 * f(rgb[2]);
  };
  const background = (el) => {
    for (let n = el; n; n = n.parentElement) {
      const c = parse(getComputedStyle(n).backgroundColor);
      if (c.length === 3 || (c.length === 4 && c[3] > 0)) { return c; }
    }
    return [255, 255, 255];
  };
  const ratio = (el) => {
    const fg = lum(parse(getComputedStyle(el).color)), bg = lum(background(el));
    return (Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05);
  };
  const nodes = Array.from(document.querySelectorAll(
    "#operations-panel h3, #operations-panel article .note, #operations-panel article label," +
    " #operations-panel article p, #ops-status, #ops-error"));
  return nodes.filter((n) => n.textContent.trim()).map((n) => ({
    id: n.id || n.tagName, ratio: Math.round(ratio(n) * 100) / 100 }));
})()"""


# -- 合成環境と本番入口 -----------------------------------------------------------


def db_state(database: Path) -> dict[str, Any]:
    from harness.infrastructure.sqlite.connection_factory import ConnectionFactory

    connection = ConnectionFactory(database).connect()
    try:

        def scalar(query: str) -> Any:
            return connection.execute(query).fetchone()[0]

        grants = connection.execute(
            "SELECT status, COUNT(*) FROM approval_grant GROUP BY status ORDER BY status"
        ).fetchall()
        return {
            "mode": scalar("SELECT mode FROM operation_control"),
            "grants": {str(row[0]): int(row[1]) for row in grants},
            "admissions": scalar("SELECT COUNT(*) FROM operation_admission"),
            "owners": scalar("SELECT COUNT(*) FROM operation_admission_owner"),
            "restores": [
                str(row[0])
                for row in connection.execute(
                    "SELECT status FROM operation_restore ORDER BY rowid"
                ).fetchall()
            ],
            "cli_journal": scalar("SELECT COUNT(*) FROM cli_invocation_journal"),
            "ledger": scalar("SELECT COUNT(*) FROM event_ledger"),
            "workbench_sessions": scalar("SELECT COUNT(*) FROM workbench_session"),
        }
    finally:
        connection.close()


def tree_digest(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO / "src"), *filter(None, [os.environ.get("PYTHONPATH")])]
    )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def new_runtime(root: Path) -> tuple[Any, Path, Path]:
    from harness.infrastructure.runtime_facade import HarnessRuntimeService

    database, cas = root / "state" / "state.sqlite3", root / "cas"
    database.parent.mkdir(parents=True)
    runtime = HarnessRuntimeService(database)
    runtime.migrate(recorded_at=STAMP)
    runtime.intake_gate().stop()
    return runtime, database, cas


def orphan(database: Path, request_id: str) -> None:
    """実Processが予約中に異常終了した状態を作る（本番の予約経路を使う）。"""
    script = (
        "import os,sys\n"
        "from pathlib import Path\n"
        "from harness.infrastructure.runtime_facade import HarnessRuntimeService\n"
        "gate = HarnessRuntimeService(Path(sys.argv[1])).intake_gate()\n"
        "with gate.admission(sys.argv[2], kind='effect'):\n"
        "    os._exit(17)\n"
    )
    result = subprocess.run(  # noqa: S603 - 固定Script・合成DB・argv list
        [sys.executable, "-c", script, str(database), request_id],
        check=False,
        capture_output=True,
        timeout=30,
        env=child_env(),
    )
    if result.returncode != 17:
        raise RuntimeError(f"orphan fixture failed rc={result.returncode}")


@contextlib.contextmanager
def live_owner(database: Path) -> Iterator[None]:
    """予約を保持したまま生存するProcess。終了は stdin を閉じて行う。"""
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from harness.infrastructure.runtime_facade import HarnessRuntimeService\n"
        "gate = HarnessRuntimeService(Path(sys.argv[1])).intake_gate()\n"
        "with gate.admission('synthetic-live-owner', kind='effect'):\n"
        "    print('held', flush=True)\n"
        "    sys.stdin.read()\n"
    )
    process = subprocess.Popen(  # noqa: S603 - 固定Script・合成DB・argv list
        [sys.executable, "-c", script, str(database)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=child_env(),
        text=True,
        start_new_session=True,
    )

    group = OwnedProcessGroup(process)

    def end() -> None:
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        group.wait_exited(30)  # stdinを閉じれば自分で終わる。回収はstopの最後だけ
        group.stop(interrupt=signal.SIGTERM, grace=5)

    try:
        if process.stdout is None or process.stdout.readline().strip() != "held":
            raise RuntimeError("live owner fixture did not start")
        yield
    except BaseException as error:
        cleanup_after_failure(error, end)
        raise
    end()


UI_URL = re.compile(r"ローカルUI: (http://127\.0\.0\.1:\d+)")
SESSION_META = re.compile(r'name="harness-session" content="([0-9a-f]{64})"')


@dataclass
class UiProcess:
    """本番CLIの `ui` を子Processで起動する。"""

    process: subprocess.Popen[bytes]
    group: OwnedProcessGroup
    url: str
    log: Path
    token: str
    stop_grace: float = 30.0

    @classmethod
    def start(
        cls,
        root: Path,
        database: Path,
        cas: Path,
        *,
        operator: bool,
        canary: str,
        command: list[str] | None = None,
        startup_timeout: float = 60.0,
        stop_grace: float = 30.0,
    ) -> UiProcess:
        """起動し、URLとSession Tokenを確かめてから返す。

        途中で失敗したら（早期終了・URL未報告・HTTP確認失敗・Token欠落・中断）、
        起動したProcess Groupを止めて回収し、**元の失敗**を投げ直す。後始末の結果は
        例外へ構造化して付く（`cleanup_outcome()`）。
        `command` は失敗注入の回帰試験だけが使う差替えで、省略時は本番CLI入口。
        """
        argv = list(command) if command is not None else cls._production_argv(database, cas)
        if command is None and operator:
            argv += ["--operator-auth-session", f"local-uid:{os.getuid()}"]
        log = root / "server.log"
        env = child_env()
        env["PYTHONUNBUFFERED"] = "1"
        # 合成Canary。画面・HTTP応答・Logのどこにも出てはならない値。
        env["GH01_SYNTHETIC_CANARY"] = canary
        try:
            with log.open("wb") as sink:
                process = subprocess.Popen(  # noqa: S603 - 本番CLI入口・argv list
                    argv,
                    stdout=sink,
                    stderr=subprocess.STDOUT,
                    env=env,
                    cwd=root,
                    start_new_session=True,
                )
        except BaseException as error:
            mark_not_started(error)
            raise
        group = OwnedProcessGroup(process)
        try:
            url = cls._await_url(group, log, startup_timeout)
            with urllib.request.urlopen(url + "/", timeout=10) as response:  # noqa: S310
                html = response.read().decode("utf-8")
            token = SESSION_META.search(html)
            if token is None:
                raise RuntimeError("session token meta was not served")
        except BaseException as error:
            cleanup_after_failure(
                error, lambda: group.stop(interrupt=signal.SIGINT, grace=stop_grace)
            )
            raise
        return cls(
            process=process,
            group=group,
            url=url,
            log=log,
            token=token.group(1),
            stop_grace=stop_grace,
        )

    @staticmethod
    def _production_argv(database: Path, cas: Path) -> list[str]:
        return [
            sys.executable,
            "-m",
            "harness.presentation.cli",
            "ui",
            "--database",
            str(database),
            "--artifact-root",
            str(cas),
            "--repo-root",
            str(REPO),
        ]

    @staticmethod
    def _await_url(group: OwnedProcessGroup, log: Path, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while True:
            match = UI_URL.search(log.read_text("utf-8", errors="replace"))
            if match:
                return match.group(1)
            if group.exited():  # 回収しない観測
                raise RuntimeError(f"ui exited early rc={group.exit_code()}")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"ui did not report its URL within {timeout}s")
            time.sleep(0.1)

    def stop(self) -> int:
        """SIGINT（本番CLIの正常終了）→猶予→Group全体SIGKILL→回収。2回目は何も送らない。"""
        return self.group.stop(interrupt=signal.SIGINT, grace=self.stop_grace)


def http(
    base: str,
    path: str,
    body: Any = None,
    *,
    token: str | None,
    origin: str | None,
    raw: bytes | None = None,
) -> tuple[int, Any]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Harness-Session"] = token
    if origin is not None:
        headers["Origin"] = origin
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    request = urllib.request.Request(base + path, data=data, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - loopback
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def check_log(log_text: str, secrets_: list[str]) -> dict[str, Any]:
    """全行が「起動JSON」「URL案内」「[ui] Method Status」のどれかであることを見る。"""
    request = re.compile(r"\[ui\] (GET|POST) \d{3}")
    notice = re.compile(r"ローカルUI: http://127\.0\.0\.1:\d+")
    lines = [line for line in log_text.splitlines() if line.strip()]
    malformed = [
        line[:120]
        for line in lines
        if not (request.fullmatch(line) or notice.fullmatch(line) or line.startswith('{"'))
    ]
    leaked = [index for index, value in enumerate(secrets_) if value and value in log_text]
    return {
        "request_lines": sum(1 for line in lines if request.fullmatch(line)),
        "malformed": malformed[:5],
        "leaked_secret_index": leaked,
    }


# -- 共通の画面操作 -------------------------------------------------------------


def shot(
    browser: Browser, report: Report, tab: str, name: str, *, selector: str = "#operations-panel"
) -> str:
    """対象Panelを画面上端へ寄せ、表示範囲だけを撮る（全ページ撮影は証跡が重すぎる）。"""
    browser.eval(
        tab,
        f"document.querySelector({json.dumps(selector)}).scrollIntoView({{block: 'start'}})",
    )
    path = report.out / "screenshots" / f"{name}.png"
    browser.call("screenshot", tab=tab, path=str(path), full=False)
    report.screenshots.append(str(path.relative_to(report.out)))
    return str(path.relative_to(report.out))


def ops_ready(browser: Browser, tab: str) -> dict[str, Any]:
    browser.wait(
        tab,
        "document.getElementById('ops-status').textContent.startsWith('受付状態') && "
        "!document.getElementById('ops-refresh').disabled",
    )
    state: dict[str, Any] = browser.eval(tab, OPS_STATE)
    return state


def refresh(browser: Browser, tab: str) -> dict[str, Any]:
    browser.call("click", tab=tab, selector="#ops-refresh")
    time.sleep(0.2)
    return ops_ready(browser, tab)


def classify_events(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """network由来の4xx/切断（意図した拒否・切断）とScript側の異常を分ける。"""
    script, network = [], []
    for event in events:
        if event.get("kind") == "log" and event.get("source") == "network":
            network.append(event)
        elif event.get("level") in ("error", "assert") or event.get("kind") == "exception":
            script.append(event)
        elif event.get("kind") == "log" and event.get("level") in ("error", "warning"):
            script.append(event)
    return {"script_errors": script, "network_rejections": network}


def posts(observed: dict[str, Any], suffix: str) -> int:
    return sum(
        1
        for request in observed.get("requests", [])
        if request.get("method") == "POST" and str(request.get("path", "")).endswith(suffix)
    )


def force_click_disabled(browser: Browser, tab: str, name: str) -> dict[str, Any]:
    """CSS/disabledだけに頼らないことを見る。DOMで有効化して実マウスで押す。"""
    browser.eval(
        tab,
        f"""(() => {{
          const box = document.getElementById("ops-{name}-approve");
          const button = document.getElementById("ops-{name}");
          box.disabled = false; box.checked = true; button.disabled = false; return true;
        }})()""",
    )
    browser.call("click", tab=tab, selector=f"#ops-{name}")
    time.sleep(0.8)
    result: dict[str, Any] = browser.call("observations", tab=tab)
    return result


# -- シナリオ ---------------------------------------------------------------------


class Scenario:
    def __init__(self, report: Report, browser: Browser | None, root: Path) -> None:
        self.report = report
        self.browser = browser
        self.root = root
        self.console: dict[str, list[dict[str, Any]]] = {}

    def need_browser(self) -> Browser:
        if self.browser is None:
            raise BridgeError("browser is not running")
        return self.browser

    def keep_events(self, label: str, observed: dict[str, Any]) -> None:
        classified = classify_events(observed.get("events", []))
        self.console.setdefault(label + ":script", []).extend(classified["script_errors"])
        self.console.setdefault(label + ":network", []).extend(classified["network_rejections"])


def scenario_invalid_operator(report: Report, root: Path) -> None:
    """B-02: 無効・他人のoperator sessionを本番CLI入口が起動前に拒否する。"""
    outcomes = {}
    for label, identity in (
        ("malformed", "local-uid:invalid"),
        ("other-uid", f"local-uid:{os.getuid() + 1}"),
        ("wrong-scheme", "token:abc"),
    ):
        base = root / f"b02-{label}"
        base.mkdir(parents=True)
        target = base / "absent"
        argv = [
            sys.executable,
            "-m",
            "harness.presentation.cli",
            "ui",
            "--database",
            str(target / "state.sqlite3"),
            "--artifact-root",
            str(target / "cas"),
            "--repo-root",
            str(REPO),
            "--operator-auth-session",
            identity,
        ]
        result = subprocess.run(  # noqa: S603 - 本番CLI入口・argv list
            argv, check=False, capture_output=True, timeout=60, env=child_env(), cwd=base
        )
        outcomes[label] = {
            "returncode": result.returncode,
            "storage_created": target.exists(),
            "served_url": "ローカルUI:" in result.stderr.decode("utf-8", "replace"),
        }
    ok = all(
        item["returncode"] != 0 and not item["storage_created"] and not item["served_url"]
        for item in outcomes.values()
    )
    report.record("B-02", "無効なoperator sessionは起動前に拒否", PATH_CLI_ONLY, ok, **outcomes)


def scenario_read_only(scenario: Scenario) -> None:
    """B-01: 起動時の明示が無ければ参照専用。強制操作・直接POSTも作用なし。"""
    report, browser = scenario.report, scenario.need_browser()
    root = scenario.root / "b01-read-only"
    root.mkdir(parents=True)
    _runtime, database, cas = new_runtime(root)
    orphan(database, "synthetic-read-only-orphan")
    before, cas_before = db_state(database), tree_digest(cas)
    canary = "gh01-canary-" + secrets.token_hex(16)
    server = UiProcess.start(root, database, cas, operator=False, canary=canary)
    try:
        browser.call("open", tab="ro", url=server.url + "/", width=1280, height=900)
        state = ops_ready(browser, "ro")
        shot(browser, report, "ro", "b01-read-only-desktop")
        forced = {
            name: force_click_disabled(browser, "ro", name) for name in ("reconcile", "resume")
        }
        page_posts = sum(posts(forced[name], "/api/operations/" + name) for name in forced)
        review = http(server.url, "/api/operations", token=server.token, origin=None)[1]
        # 画面と同じOrigin・Tokenでの直接POST（サーバー側の拒否を見る）
        direct = {
            name: http(
                server.url,
                f"/api/operations/{name}",
                {"review_hash": review[name]["review_hash"], "approve": True},
                token=server.token,
                origin=server.url,
            )
            for name in ("reconcile", "resume")
        }
        for name in forced:
            scenario.keep_events("b01", forced[name])
        after, cas_after = db_state(database), tree_digest(cas)
        ok = (
            "参照専用" in state["status"]
            and state["reconcileApproveDisabled"]
            and state["resumeApproveDisabled"]
            and state["reconcileDisabled"]
            and state["resumeDisabled"]
            and page_posts == 0
            and all(
                status == 409 and body["error"].get("code") == "APPROVAL_REQUIRED"
                for status, body in direct.values()
            )
            and before == after
            and cas_before == cas_after
        )
        report.record(
            "B-01",
            "参照専用: 画面の強制操作・直接POSTとも作用なし",
            PATH_RECOVERY,
            ok,
            status_text=state["status"],
            forced_page_posts=page_posts,
            direct_post={
                name: [code, body["error"].get("code")] for name, (code, body) in direct.items()
            },
            db_unchanged=before == after,
            cas_unchanged=cas_before == cas_after,
            db_after=after,
        )
    finally:
        # タブを閉じられなくてもUIサーバーの停止へ必ず進む（Chromiumはbrowser.closeで止まる）。
        with contextlib.suppress(BridgeError):
            browser.call("close_tab", tab="ro")
        code = report.release("read-only:ui.stop", server.stop)
        log = check_log(server.log.read_text("utf-8"), [server.token, canary])
        report.record(
            "B-08",
            "参照専用サーバーのLogにToken/Canary無し・Method+Statusのみ",
            PATH_RECOVERY,
            code == 0 and not log["malformed"] and not log["leaked_secret_index"],
            server_exit=code,
            **log,
        )


def scenario_orphan(scenario: Scenario) -> None:
    """B-03/04/06/07/08/10: 孤立予約の整理→再確認→別承認で再開（複数タブ）。"""
    report, browser = scenario.report, scenario.need_browser()
    root = scenario.root / "r1-orphan"
    root.mkdir(parents=True)
    runtime, database, cas = new_runtime(root)
    hostile = XSS_TEXT + TRAVERSAL_TEXT + "長い診断" + "L" * 600
    orphan(database, hostile)
    canary = "gh01-canary-" + secrets.token_hex(16)
    server = UiProcess.start(root, database, cas, operator=True, canary=canary)
    tabs: list[str] = []
    try:
        # ---- B-06: 本番HTTPでの拒否（ブラウザー外から偽装できるHeaderも含む） ----
        status, review = http(server.url, "/api/operations", token=server.token, origin=None)
        reconcile_hash = review["reconcile"]["review_hash"]
        body = {"review_hash": reconcile_hash, "approve": True}
        start = db_state(database)
        faults = {
            "read-without-session": http(server.url, "/api/operations", token=None, origin=None),
            "origin-mismatch": http(
                server.url,
                "/api/operations/reconcile",
                body,
                token=server.token,
                origin="http://127.0.0.1:1",
            ),
            "no-session": http(
                server.url, "/api/operations/reconcile", body, token=None, origin=server.url
            ),
            "wrong-session": http(
                server.url, "/api/operations/reconcile", body, token="0" * 64, origin=server.url
            ),
            "unknown-field": http(
                server.url,
                "/api/operations/reconcile",
                {**body, "database": str(database), XSS_TEXT: 1},
                token=server.token,
                origin=server.url,
            ),
            "approve-not-true": http(
                server.url,
                "/api/operations/reconcile",
                {"review_hash": reconcile_hash, "approve": "true"},
                token=server.token,
                origin=server.url,
            ),
            "not-json": http(
                server.url,
                "/api/operations/reconcile",
                None,
                token=server.token,
                origin=server.url,
                raw=b"review_hash=x",
            ),
        }
        unchanged = db_state(database) == start
        orphan(database, "synthetic-second-orphan")  # 対象状態の変化で確認Hashを古くする
        changed = db_state(database)
        stale = http(
            server.url, "/api/operations/reconcile", body, token=server.token, origin=server.url
        )
        codes = {name: value[0] for name, value in faults.items()}
        report.record(
            "B-06",
            "Origin/session/未知field/非JSON/古いhashをHTTPで拒否し保存状態不変",
            PATH_HTTP_ONLY,
            status == 200
            and all(400 <= code < 500 for code in codes.values())
            and codes["origin-mismatch"] == 403
            and codes["no-session"] == 403
            and codes["read-without-session"] == 403
            and unchanged
            and stale[0] == 409
            and db_state(database) == changed
            and changed["grants"] == {},
            statuses=codes,
            stale_status=stale[0],
            stale_code=stale[1].get("error", {}).get("code"),
            unchanged_after_faults=unchanged,
            grants_after=db_state(database)["grants"],
        )

        # ---- 画面: 初期表示・未チェックでは送信しない ----
        browser.call("open", tab="A", url=server.url + "/", width=1280, height=900)
        tabs.append("A")
        state = ops_ready(browser, "A")
        shot(browser, report, "A", "r1-initial-desktop")
        browser.call("click", tab="A", selector="#ops-reconcile")
        time.sleep(0.5)
        unchecked = browser.call("observations", tab="A")
        scenario.keep_events("r1", unchecked)
        report.record(
            "B-08",
            "予約名のHTML/トラバーサル/長文をtextとして表示し実行しない",
            PATH_RECOVERY,
            state["injected"] == 0
            and state["xss"] is None
            and "<img src=x" in state["reconcileReview"]
            and TRAVERSAL_TEXT in state["reconcileReview"]
            and state["panelOverflow"] <= 0
            and state["docOverflow"] <= 0,
            injected_nodes=state["injected"],
            xss_flag=state["xss"],
            panel_overflow=state["panelOverflow"],
            doc_overflow=state["docOverflow"],
        )

        # ---- キーボードで承認→Enter。遅延中の二重クリック・Enterは1回だけ ----
        browser.eval("A", "document.getElementById('ops-reconcile-approve').focus()")
        browser.call("key", tab="A", key="Space")
        after_space = browser.eval("A", OPS_STATE)
        browser.call("key", tab="A", key="Tab")
        focus_after_tab = browser.eval("A", "document.activeElement.id")
        browser.call("intercept", tab="A", path="/api/operations/reconcile", mode="delay", ms=1500)
        browser.call("key", tab="A", key="Enter")
        time.sleep(0.2)
        during = browser.eval("A", OPS_STATE)
        browser.call("click", tab="A", selector="#ops-reconcile", count=2)
        browser.call("key", tab="A", key="Enter")
        browser.wait(
            "A", "document.getElementById('ops-status').textContent.includes('整理を記録')"
        )
        browser.call("clear_intercepts", tab="A")
        done = browser.eval("A", OPS_STATE)
        observed = browser.call("observations", tab="A")
        scenario.keep_events("r1", observed)
        reconciled = db_state(database)
        result = json.loads(done["result"])
        receipt_ok = runtime.verify_ledger(result["operation_id"])
        with runtime.operator_resume(artifact_root=cas, repo_root=REPO) as service:
            from harness.domain.hashing import ContentHash

            receipt = service.artifacts.verify(ContentHash.parse(result["receipt_hash"])).ok
        shot(browser, report, "A", "r1-after-reconcile-desktop")
        report.record(
            "B-03",
            "整理: チェック後だけ送信、Receipt保存、DRAINING維持、Provider起動0",
            PATH_RECOVERY,
            posts(unchecked, "/api/operations/reconcile") == 0
            and after_space["reconcileApproveChecked"]
            and not after_space["reconcileDisabled"]
            and focus_after_tab == "ops-reconcile"
            and result["mode"] == "DRAINING"
            and result["resumed"] is False
            and reconciled["mode"] == "DRAINING"
            and reconciled["admissions"] == 0
            and reconciled["owners"] == 0
            and reconciled["grants"] == {"CONSUMED": 1}
            and reconciled["cli_journal"] == changed["cli_journal"]
            and reconciled["workbench_sessions"] == 0
            and receipt_ok
            and receipt,
            unchecked_posts=posts(unchecked, "/api/operations/reconcile"),
            keyboard_checked=after_space["reconcileApproveChecked"],
            focus_after_tab=focus_after_tab,
            result_mode=result["mode"],
            resumed=result["resumed"],
            db=reconciled,
            ledger_valid=receipt_ok,
            receipt_verified=receipt,
        )
        report.record(
            "B-07",
            "遅延応答中の二重クリック・Enterでも整理POSTは1回",
            PATH_RECOVERY,
            posts(observed, "/api/operations/reconcile") == 1
            and during["reconcileDisabled"]
            and during["refreshDisabled"]
            and reconciled["grants"] == {"CONSUMED": 1},
            page_posts=posts(observed, "/api/operations/reconcile"),
            buttons_disabled_while_pending=during["reconcileDisabled"],
        )
        report.record(
            "B-10",
            "操作完了後もfocusが運用Panel内に残る（キーボード操作の継続）",
            PATH_RECOVERY,
            done["activeInPanel"] and done["active"] not in (None, "BODY"),
            active_after=done["active"],
        )
        report.record(
            "B-04",
            "整理後は古い確認内容を「検査通過」と表示し続けない（再確認を促す）",
            PATH_RECOVERY,
            not done["reconcileSummary"].startswith("検査通過")
            and not done["resumeSummary"].startswith("検査通過")
            and reconcile_hash not in done["reconcileReview"]
            and done["reconcileApproveDisabled"]
            and done["resumeApproveDisabled"],
            reconcile_summary=done["reconcileSummary"][:120],
            resume_summary=done["resumeSummary"][:120],
            stale_hash_still_shown=reconcile_hash in done["reconcileReview"],
        )

        # ---- 整理時のhash・承認の流用拒否 ----
        reuse_reconcile = http(
            server.url, "/api/operations/reconcile", body, token=server.token, origin=server.url
        )
        reuse_resume = http(
            server.url,
            "/api/operations/resume",
            {"review_hash": review["resume"]["review_hash"], "approve": True},
            token=server.token,
            origin=server.url,
        )
        reconcile_as_resume = http(
            server.url,
            "/api/operations/resume",
            {"review_hash": result["review"]["review_hash"], "approve": True},
            token=server.token,
            origin=server.url,
        )
        still = db_state(database)

        # ---- 再確認 → 複数タブで同時に再開 ----
        fresh = refresh(browser, "A")
        report.record(
            "B-10",
            "「現在の状態を確認」押下後もfocusが運用Panel内に残る",
            PATH_RECOVERY,
            fresh["activeInPanel"] and fresh["active"] not in (None, "BODY"),
            active_after=fresh["active"],
        )
        browser.call("open", tab="B", url=server.url + "/", width=390, height=844, dark=True)
        tabs.append("B")
        narrow = ops_ready(browser, "B")
        contrast = browser.eval("B", CONTRAST)
        shot(browser, report, "B", "r1-narrow-dark-before-resume")
        for tab in ("A", "B"):
            browser.call("click", tab=tab, selector="#ops-resume-approve")
        browser.call("intercept", tab="A", path="/api/operations/resume", mode="delay", ms=800)
        browser.call("click", tab="A", selector="#ops-resume")
        browser.call("click", tab="B", selector="#ops-resume")
        for tab in ("A", "B"):
            browser.wait(
                tab,
                "!document.getElementById('ops-refresh').disabled && "
                "document.getElementById('ops-result').textContent.length > 0",
            )
        end_a, end_b = browser.eval("A", OPS_STATE), browser.eval("B", OPS_STATE)
        obs_a, obs_b = browser.call("observations", tab="A"), browser.call("observations", tab="B")
        scenario.keep_events("r1", obs_a)
        scenario.keep_events("r1", obs_b)
        resumed = db_state(database)
        shot(browser, report, "B", "r1-narrow-dark-after-resume")
        shot(browser, report, "A", "r1-desktop-after-resume")
        outcome = sorted(
            [
                "success" if "再開しました" in item["status"] else "rejected"
                for item in (end_a, end_b)
            ]
        )
        rejected = end_b if "再開しました" in end_a["status"] else end_a
        report.record(
            "B-04",
            "再開は新しいreviewと別承認だけ。整理時のhash/承認の流用は409",
            PATH_RECOVERY,
            reuse_reconcile[0] == 409
            and reuse_resume[0] == 409
            and reconcile_as_resume[0] == 409
            and still == reconciled
            and fresh["resumeSummary"].startswith("検査通過")
            and fresh["reconcileApproveDisabled"]
            and resumed["mode"] == "OPEN"
            and resumed["grants"] == {"CONSUMED": 2},
            reuse_statuses=[reuse_reconcile[0], reuse_resume[0], reconcile_as_resume[0]],
            fresh_resume_summary=fresh["resumeSummary"][:80],
            db=resumed,
        )
        report.record(
            "B-07",
            "複数タブで同時に再開しても効果は1回、他方は読める拒否理由",
            PATH_RECOVERY,
            outcome == ["rejected", "success"]
            and posts(obs_a, "/api/operations/resume") == 1
            and posts(obs_b, "/api/operations/resume") == 1
            and resumed["grants"] == {"CONSUMED": 2}
            and "[" in rejected["error"]
            and "object Object" not in rejected["error"],
            outcomes=outcome,
            rejected_error=rejected["error"][:160],
        )
        low = [item for item in contrast if item["ratio"] < 4.5]
        report.record(
            "B-10",
            "狭幅(390px)・dark modeで横はみ出し無し、本文コントラスト4.5以上",
            PATH_RECOVERY,
            narrow["docOverflow"] <= 0
            and narrow["panelOverflow"] <= 0
            and end_b["docOverflow"] <= 0
            and not low,
            doc_overflow=narrow["docOverflow"],
            panel_overflow=narrow["panelOverflow"],
            low_contrast=low[:8],
            measured=len(contrast),
        )
        body_text = browser.eval("A", "document.body.innerText") + browser.eval(
            "B", "document.body.innerText"
        )
        report.record(
            "B-08",
            "画面本文にSession Token/Canaryを表示しない",
            PATH_RECOVERY,
            server.token not in body_text and canary not in body_text,
            token_in_text=server.token in body_text,
            canary_in_text=canary in body_text,
        )
    finally:
        for tab in tabs:
            with contextlib.suppress(BridgeError):
                browser.call("close_tab", tab=tab)
        code = report.release("orphan:ui.stop", server.stop)
        log = check_log(server.log.read_text("utf-8"), [server.token, canary])
        report.record(
            "B-08",
            "運用サーバーのLogにToken/Canary無し・Method+Statusのみ",
            PATH_RECOVERY,
            code == 0 and not log["malformed"] and not log["leaked_secret_index"],
            server_exit=code,
            **log,
        )


def scenario_failed_copy(scenario: Scenario) -> None:
    """B-03/07: 失敗コピーの整理（コピー保持・restore_verified=false）と結果不明の扱い。"""
    report, browser = scenario.report, scenario.need_browser()
    root = scenario.root / "r2-failed-copy"
    root.mkdir(parents=True)
    runtime, database, cas = new_runtime(root)
    cas.mkdir()
    backup = runtime.create_backup(
        source_cas=cas,
        destination_database=root / "backup.sqlite3",
        destination_cas=root / "backup-cas",
        backup_id="gh01-partial-copy",
        created_at=STAMP,
    )
    target = root / "partial-copy.sqlite3"
    service = runtime.backup_restore_service(
        restore_database=target, restore_cas=root / "partial-copy-cas"
    )

    def fail_after_copy(_guard: object) -> None:
        raise OSError("synthetic failure after database copy")

    with contextlib.suppress(OSError):
        service.restore_and_verify(
            backup=backup, backup_restore_id="gh01-partial", during_restore=fail_after_copy
        )
    if not target.is_file():
        raise RuntimeError("partial copy fixture did not produce a copy")
    partial = hashlib.sha256(target.read_bytes()).hexdigest()
    canary = "gh01-canary-" + secrets.token_hex(16)
    server = UiProcess.start(root, database, cas, operator=True, canary=canary)
    try:
        browser.call("open", tab="C", url=server.url + "/", width=1024, height=800)
        state = ops_ready(browser, "C")
        shot(browser, report, "C", "r2-failed-copy-review")
        browser.call("click", tab="C", selector="#ops-reconcile-approve")
        browser.call("click", tab="C", selector="#ops-reconcile")
        browser.wait(
            "C", "document.getElementById('ops-status').textContent.includes('整理を記録')"
        )
        result = json.loads(browser.eval("C", "document.getElementById('ops-result').textContent"))
        after = db_state(database)
        report.record(
            "B-03",
            "失敗コピーの整理: コピー保持・restore_verified=false・DRAINING",
            PATH_RECOVERY,
            state["reconcileSummary"].startswith("検査通過")
            and result["restore_verified"] is False
            and result["mode"] == "DRAINING"
            and after["restores"] == ["ABANDONED"]
            and after["mode"] == "DRAINING"
            and hashlib.sha256(target.read_bytes()).hexdigest() == partial
            and after["cli_journal"] == 0,
            restore_verified=result["restore_verified"],
            restores=after["restores"],
            copy_preserved=hashlib.sha256(target.read_bytes()).hexdigest() == partial,
        )
        # ---- 結果不明: サーバーは処理済み、応答だけ届かない ----
        refresh(browser, "C")
        browser.call("click", tab="C", selector="#ops-resume-approve")
        browser.call(
            "intercept", tab="C", path="/api/operations/resume", mode="fail_after_response"
        )
        browser.call("click", tab="C", selector="#ops-resume")
        browser.wait("C", "document.getElementById('ops-status').textContent.includes('結果不明')")
        unknown = browser.eval("C", OPS_STATE)
        time.sleep(2.0)  # 自動再送が無いことを待って確かめる
        browser.call("click", tab="C", selector="#ops-resume")
        browser.call("click", tab="C", selector="#ops-reconcile")
        time.sleep(0.5)
        observed = browser.call("observations", tab="C")
        scenario.keep_events("r2", observed)
        shot(browser, report, "C", "r2-result-unknown")
        server_side = db_state(database)
        checked = refresh(browser, "C")
        browser.call("clear_intercepts", tab="C")
        shot(browser, report, "C", "r2-after-recheck")
        report.record(
            "B-07",
            "通信切断は結果不明と表示し自動再送しない。再確認で実状態を表示",
            PATH_RECOVERY,
            posts(observed, "/api/operations/resume") == 1
            and any(item["mode"] == "fail_after_response" for item in observed["intercepted"])
            and unknown["resumeDisabled"]
            and server_side["mode"] == "OPEN"
            and server_side["grants"] == {"CONSUMED": 2}
            and "受付状態: OPEN" in checked["status"]
            and not checked["resumeSummary"].startswith("検査通過"),
            page_posts=posts(observed, "/api/operations/resume"),
            status_after_loss=unknown["status"][:80],
            db=server_side,
            status_after_recheck=checked["status"][:80],
        )
    finally:
        with contextlib.suppress(BridgeError):
            browser.call("close_tab", tab="C")
        code = report.release("failed-copy:ui.stop", server.stop)
        log = check_log(server.log.read_text("utf-8"), [server.token, canary])
        report.record(
            "B-08",
            "失敗コピー検査のLogにToken/Canary無し",
            PATH_RECOVERY,
            code == 0 and not log["malformed"] and not log["leaked_secret_index"],
            server_exit=code,
            **log,
        )


def _unsettled_journal(database: Path) -> None:
    from harness.infrastructure.sqlite.connection_factory import ConnectionFactory

    factory = ConnectionFactory(database)
    connection = factory.connect()
    digest = "sha256:" + "1" * 64
    try:
        with factory.begin_immediate(connection):
            connection.execute(
                "INSERT INTO cli_invocation_journal VALUES (?,?,?,?,?,?,?,?,?)",
                ("gh01", "gh01", digest, digest, digest, "EFFECT_UNKNOWN", None, 1, digest),
            )
    finally:
        connection.close()


def _issued_approval(runtime: Any, cas: Path) -> None:
    from harness.domain.hashing import ContentHash

    with runtime.operator_resume(artifact_root=cas, repo_root=REPO) as service:
        digest = ContentHash.parse("sha256:" + "1" * 64)
        grant, _ = service.authority.issue(
            run_id="gh01-pending",
            plan_content_hash=digest,
            execution_plan_hash=digest,
            scope=("x",),
            subject=f"local-uid:{os.getuid()}",
            now=STAMP,
            expires_at="2999-01-01T00:00:00Z",
        )
        with service.uow.begin_immediate():
            service.grants.issue(grant)


def _legacy(database: Path, statement: str) -> None:
    from harness.infrastructure.sqlite.connection_factory import ConnectionFactory

    factory = ConnectionFactory(database)
    connection = factory.connect()
    try:
        with factory.begin_immediate(connection):
            connection.execute(statement)
    finally:
        connection.close()


def _completed_resume(runtime: Any, cas: Path) -> None:
    with runtime.operator_resume(artifact_root=cas, repo_root=REPO) as service:
        service.resume(
            review_hash=service.inspect()["review_hash"],
            auth_session=f"local-uid:{os.getuid()}",
            reason="maintenance-complete",
        )
    runtime.intake_gate().stop()


def _tamper_ledger(database: Path) -> None:
    """合成DBだけで改ざんを模す（tests/integration/sqlite/test_event_ledger.pyと同じ手順）。

    Append-only Triggerを外すのは改ざん者の行為の再現であり、本番経路の変更ではない。
    """
    from harness.infrastructure.sqlite.connection_factory import ConnectionFactory, ConnectionRole

    connection = ConnectionFactory(database).connect(ConnectionRole.MIGRATION)
    try:
        connection.execute("DROP TRIGGER trg_event_ledger_no_update")
        connection.execute(
            "UPDATE event_ledger SET payload_hash=? WHERE rowid=(SELECT MAX(rowid) "
            "FROM event_ledger)",
            ("sha256:" + "e" * 64,),
        )
        connection.commit()
    finally:
        connection.close()


def scenario_blockers(scenario: Scenario) -> None:
    """B-05: 拒否状態では理由を表示し、画面の強制操作・直接POSTとも作用なし。"""
    report, browser = scenario.report, scenario.need_browser()
    variants: list[
        tuple[str, str, Callable[[Any, Path, Path], contextlib.AbstractContextManager[Any]]]
    ]
    variants = [
        ("live-owner", "RESERVATION_OWNER_ACTIVE_OR_UNKNOWN", lambda r, d, c: live_owner(d)),
        (
            "unsettled-journal",
            "UNSETTLED_JOURNALS_OR_APPROVALS",
            lambda r, d, c: _prepared(lambda: (orphan(d, "gh01-orphan"), _unsettled_journal(d))),
        ),
        (
            "issued-approval",
            "UNSETTLED_JOURNALS_OR_APPROVALS",
            lambda r, d, c: _prepared(lambda: _issued_approval(r, c)),
        ),
        (
            "legacy-reservation",
            "RESERVATION_OWNER_ACTIVE_OR_UNKNOWN",
            lambda r, d, c: _prepared(
                lambda: _legacy(d, "INSERT INTO operation_admission VALUES ('legacy','effect')")
            ),
        ),
        (
            "legacy-restoring",
            "LEGACY_RESTORE_WITHOUT_SOURCE_BINDING",
            lambda r, d, c: _prepared(
                lambda: _legacy(d, "UPDATE operation_control SET mode='RESTORING'")
            ),
        ),
        (
            "audit-cas-missing",
            "AUDIT_ARTIFACTS_UNVERIFIED",
            lambda r, d, c: _prepared(lambda: (_completed_resume(r, c), shutil.rmtree(c))),
        ),
        (
            "ledger-tampered",
            "LEDGER_VERIFICATION_FAILED",
            lambda r, d, c: _prepared(lambda: (_completed_resume(r, c), _tamper_ledger(d))),
        ),
    ]
    for index, (label, expected, prepare) in enumerate(variants):
        root = scenario.root / f"b05-{label}"
        root.mkdir(parents=True)
        runtime, database, cas = new_runtime(root)
        tab = f"b05-{index}"
        try:
            with prepare(runtime, database, cas):
                before, cas_before = db_state(database), tree_digest(cas)
                canary = "gh01-canary-" + secrets.token_hex(16)
                server = UiProcess.start(root, database, cas, operator=True, canary=canary)
                try:
                    width = 390 if index == 0 else 1280
                    browser.call("open", tab=tab, url=server.url + "/", width=width, height=900)
                    state = ops_ready(browser, tab)
                    if index in (0, len(variants) - 1):
                        shot(browser, report, tab, f"b05-{label}")
                    forced = {
                        name: force_click_disabled(browser, tab, name)
                        for name in ("reconcile", "resume")
                    }
                    for name in forced:
                        scenario.keep_events("b05", forced[name])
                    review = http(server.url, "/api/operations", token=server.token, origin=None)[1]
                    direct = {
                        name: http(
                            server.url,
                            f"/api/operations/{name}",
                            {"review_hash": review[name]["review_hash"], "approve": True},
                            token=server.token,
                            origin=server.url,
                        )[0]
                        for name in ("reconcile", "resume")
                    }
                    after, cas_after = db_state(database), tree_digest(cas)
                    summaries = state["reconcileSummary"] + " " + state["resumeSummary"]
                    page_posts = sum(
                        posts(forced[name], "/api/operations/" + name) for name in forced
                    )
                    report.record(
                        "B-05",
                        f"拒否状態 {label}: 理由表示・操作無効・作用なし",
                        PATH_RECOVERY,
                        expected in summaries
                        and state["reconcileApproveDisabled"]
                        and state["resumeApproveDisabled"]
                        and page_posts == 0
                        and all(400 <= code < 500 for code in direct.values())
                        and before == after
                        and cas_before == cas_after
                        and state["docOverflow"] <= 0,
                        expected_blocker=expected,
                        summaries=summaries[:240],
                        forced_page_posts=page_posts,
                        direct_status=direct,
                        db_unchanged=before == after,
                        cas_unchanged=cas_before == cas_after,
                    )
                finally:
                    with contextlib.suppress(BridgeError):
                        browser.call("close_tab", tab=tab)
                    report.release(f"blockers:{label}:ui.stop", server.stop)
        except Exception as error:  # FAILとして記録し全体をFAILにする
            report.error("B-05", f"拒否状態 {label}", PATH_RECOVERY, error)


@contextlib.contextmanager
def _prepared(action: Callable[[], Any]) -> Iterator[None]:
    action()
    yield


# -- Workbench -------------------------------------------------------------------


class CountingRunner:
    """本物の SubprocessCliRunner を包み、起動回数とargvだけを数える。"""

    def __init__(self) -> None:
        from harness.infrastructure.provider.cli_runner import SubprocessCliRunner

        self.inner = SubprocessCliRunner()
        self.calls: list[dict[str, Any]] = []

    def stop(self) -> None:
        self.inner.stop()

    def run(self, spec: Any, **kwargs: Any) -> Any:
        self.calls.append(
            {
                "provider_id": spec.provider_id,
                "model_id": spec.model_id,
                "reasoning_effort": spec.reasoning_effort,
                "argv_has_model": spec.model_id in spec.argv,
                "argv_has_effort": spec.reasoning_effort is not None
                and any(spec.reasoning_effort in item for item in spec.argv),
            }
        )
        return self.inner.run(spec, **kwargs)


WB_STATE = """(() => {
  const g = (id) => document.getElementById(id);
  return {
    confirmHidden: g("wb-confirm").hidden,
    diffHidden: g("wb-diff").hidden,
    payload: g("wb-payload").textContent,
    confirmRows: g("wb-confirm-rows").innerText,
    after: g("wb-after").textContent,
    applyResult: g("wb-apply-result").textContent,
    sendDisabled: g("wb-approve-send").disabled,
    applyDisabled: g("wb-approve-apply").disabled,
    nextDisabled: g("wb-next-target").disabled,
    queueHidden: g("wb-queue").hidden,
    queueStatus: g("wb-queue-status").textContent,
    history: Array.from(g("wb-session-rows").rows).map((row) => row.cells[1].textContent),
    formError: g("wb-form-error").textContent,
    sendError: g("wb-send-error").textContent,
    applyError: g("wb-apply-error").textContent,
    injected: document.querySelectorAll("#workbench-panel img, #workbench-panel script").length,
    xss: window.__gh01_xss || null,
    effortOptions: Array.from(g("wb-effort").options).map((o) => o.value),
    effortDisabled: g("wb-effort").disabled,
    models: Array.from(g("wb-model").options).map((o) => o.value),
    providers: Array.from(g("wb-provider").options).map((o) => o.value)
  };
})()"""


def workbench_layouts(browser: Browser, report: Report, tab: str) -> list[dict[str, Any]]:
    """Provider表と入力欄を各幅で実際に描画し、表をキーボードでスクロールする。"""
    checks: list[dict[str, Any]] = []
    browser.call("click", tab=tab, selector="#wb-provider-details > summary")
    browser.wait(tab, "document.getElementById('wb-provider-details').open")
    browser.call("click", tab=tab, selector="#legacy-settings-details > summary")
    browser.wait(tab, "document.getElementById('legacy-settings-details').open")
    for width in (390, 768, 1024, 1280):
        for dark in (False, True):
            browser.call("viewport", tab=tab, width=width, height=900, dark=dark)
            browser.wait(tab, f"window.innerWidth === {width}")
            metrics = browser.eval(
                tab,
                """(() => {
              const panel = document.getElementById('workbench-panel');
              const region = document.getElementById('wb-provider-table-region');
              const table = document.getElementById('wb-provider-rows').closest('table');
              const controls = ['wb-provider','wb-model','wb-effort','wb-target','wb-instruction']
                .map(id => {const e = document.getElementById(id), r = e.getBoundingClientRect();
                  return {id, left:r.left, right:r.right, width:r.width};});
              if (region) {region.scrollLeft = 0; region.focus({preventScroll:true});}
              return {docOverflow:document.documentElement.scrollWidth-window.innerWidth,
                panelOverflow:panel.scrollWidth-panel.clientWidth,
                regionPresent:!!region,
                regionOverflow:region ? region.scrollWidth-region.clientWidth : null,
                regionRight:region ? region.getBoundingClientRect().right : null,
                regionStyle:region ? getComputedStyle(region).overflowX : null,
                focused:!!region && document.activeElement===region,
                tableWidth:table.getBoundingClientRect().width,
                visibleRows:document.getElementById('wb-provider-rows').rows.length,
                controls};})()""",
            )
            keyboard = bool(metrics["regionPresent"])
            last_visible = False
            if metrics["regionPresent"]:
                # DOM focusだけでは背景Tabのnative keyが届かない。利用者と同じく前面でclick。
                browser.call("activate", tab=tab)
                browser.call("click", tab=tab, selector="#wb-provider-table-region")
                metrics["focused"] = browser.eval(
                    tab, "document.activeElement.id==='wb-provider-table-region'"
                )
                if metrics["regionOverflow"] > 1:
                    browser.call("key", tab=tab, key="ArrowRight")
                    browser.wait(
                        tab, "document.getElementById('wb-provider-table-region').scrollLeft>0"
                    )
                    keyboard = browser.eval(
                        tab, "document.getElementById('wb-provider-table-region').scrollLeft>0"
                    )
                last_visible = browser.eval(
                    tab,
                    """(() => {
                  const region=document.getElementById('wb-provider-table-region');
                  region.scrollLeft=region.scrollWidth;
                  const cell=document.getElementById('wb-provider-rows').rows[0].cells[7];
                  return cell.getBoundingClientRect().right <=
                    region.getBoundingClientRect().right+1;
                })()""",
                )
                browser.eval(
                    tab, "document.getElementById('wb-provider-table-region').scrollLeft=0"
                )
            status_metrics = browser.eval(
                tab,
                """(() => {
              const panel=document.getElementById('providers-panel');
              const region=document.getElementById('provider-status-table-region');
              return {panelOverflow:panel.scrollWidth-panel.clientWidth,
                regionPresent:!!region,
                regionOverflow:region ? region.scrollWidth-region.clientWidth : null,
                regionRight:region ? region.getBoundingClientRect().right : null,
                rows:document.getElementById('provider-rows').rows.length};})()""",
            )
            status_accessible = False
            if status_metrics["regionPresent"] and status_metrics["rows"] > 0:
                browser.call("activate", tab=tab)
                browser.call("click", tab=tab, selector="#provider-status-table-region")
                status_focused = browser.eval(
                    tab, "document.activeElement.id==='provider-status-table-region'"
                )
                if status_metrics["regionOverflow"] > 1:
                    browser.call("key", tab=tab, key="ArrowRight")
                    browser.wait(
                        tab, "document.getElementById('provider-status-table-region').scrollLeft>0"
                    )
                status_accessible = status_focused and browser.eval(
                    tab,
                    """(() => {
                  const region=document.getElementById('provider-status-table-region');
                  region.scrollLeft=region.scrollWidth;
                  const cells=document.getElementById('provider-rows').rows[0].cells;
                  return cells[cells.length-1].getBoundingClientRect().right <=
                    region.getBoundingClientRect().right+1;})()""",
                )
                browser.eval(
                    tab, "document.getElementById('provider-status-table-region').scrollLeft=0"
                )
            controls_ok = all(
                c["width"] > 0 and c["left"] >= 0 and c["right"] <= width + 1
                for c in metrics["controls"]
            )
            checks.append(
                {
                    "name": f"Workbench/Provider表 {width}px {'dark' if dark else 'light'}: "
                    "専用スクロール・最右列・入力欄が画面内",
                    "passed": metrics["docOverflow"] <= 1
                    and metrics["panelOverflow"] <= 1
                    and metrics["regionPresent"]
                    and metrics["regionRight"] <= width
                    and metrics["regionStyle"] == "auto"
                    and metrics["focused"]
                    and metrics["visibleRows"] > 0
                    and controls_ok
                    and keyboard
                    and last_visible
                    and status_accessible
                    and status_metrics["panelOverflow"] <= 1,
                    "width": width,
                    "dark": dark,
                    "metrics": metrics,
                    "keyboard_scroll": keyboard,
                    "last_column_accessible": last_visible,
                    "provider_status_metrics": status_metrics,
                    "provider_status_accessible": status_accessible,
                    "provider_status_fixture": "UI_LAYOUT_SYNTHETIC_READ_RESPONSE",
                }
            )
            shot(
                browser,
                report,
                tab,
                f"w1-provider-{width}-{'dark' if dark else 'light'}",
                selector="#workbench-panel",
            )
    browser.call("viewport", tab=tab, width=1280, height=900, dark=False)
    browser.call("click", tab=tab, selector="#legacy-settings-details > summary")
    browser.wait(tab, "!document.getElementById('legacy-settings-details').open")
    return checks


def workbench_local_context(browser: Browser, tab: str) -> dict[str, Any]:
    """Real UI/HTTP/local storage; copying a note must never spawn a provider."""
    browser.wait(tab, "document.getElementById('connection-overview').children.length > 0")
    initial = browser.eval(
        tab,
        """(() => ({
          realConnections: document.getElementById('connection-overview').children.length,
          legacyClosed: !document.getElementById('legacy-settings-details').open,
          oldSaveDisabled: document.getElementById('value-save').disabled
        }))()""",
    )
    browser.call("click", tab=tab, selector="#new-conversation")
    browser.wait(tab, "!document.getElementById('detail-panel').hidden")
    browser.wait(tab, "document.getElementById('role-select').value === 'USER_TASK'")
    before_snapshot = browser.eval(
        tab, "document.getElementById('context-preview-submit').disabled"
    )
    browser.eval(
        tab,
        "document.getElementById('message-text').value = "
        "'関数の目的がわかる説明コメントを追加してください。'",
    )
    browser.call("click", tab=tab, selector="#composer button[type=submit]")
    browser.wait(tab, "document.querySelectorAll('#message-rows tr').length === 1")
    browser.call("click", tab=tab, selector="#build-snapshot")
    browser.wait(tab, "document.getElementById('preview-snapshot').options.length > 0")
    browser.eval(
        tab,
        """(() => {
          const model = document.getElementById('wb-model');
          model.value='gpt-5.6-sol';
          model.dispatchEvent(new Event('change', {bubbles:true}));
        })()""",
    )
    browser.call("click", tab=tab, selector="#context-use-selection")
    browser.wait(tab, "document.getElementById('preview-model').value === 'gpt-5.6-sol'")
    browser.call("click", tab=tab, selector="#context-preview-submit")
    browser.wait(
        tab,
        "!document.getElementById('preview-result').hidden || "
        "document.getElementById('preview-error').textContent.length > 0",
    )
    context = browser.eval(
        tab,
        """(() => ({
          error:document.getElementById('preview-error').textContent,
          previewVisible:!document.getElementById('preview-result').hidden,
          selectedRows:document.querySelectorAll('#preview-selected tr').length,
          selectedProvider:document.getElementById('preview-provider').value
        }))()""",
    )
    browser.call("click", tab=tab, selector="#conversation-to-workbench")
    copied = browser.eval(tab, "document.getElementById('wb-instruction').value")
    browser.eval(tab, "document.getElementById('wb-instruction').value = '保全する入力済みの依頼'")
    browser.call("click", tab=tab, selector="#conversation-to-workbench")
    overwrite = browser.eval(
        tab,
        """(() => ({
          retained:document.getElementById('wb-instruction').value === '保全する入力済みの依頼',
          explained:document.getElementById('conversation-transfer-status').textContent.includes('上書き')
        }))()""",
    )
    return {
        **initial,
        "snapshot_required_before_preview": before_snapshot,
        "context": context,
        "copied": copied == "関数の目的がわかる説明コメントを追加してください。",
        "overwrite": overwrite,
    }


def scenario_workbench(scenario: Scenario) -> None:
    """B-09: 本番HTTP + 偽CLI（実Process）でWorkbenchの順次編集を画面から通す。"""
    report, browser = scenario.report, scenario.need_browser()
    sys.path.insert(0, str(REPO / "tests" / "support"))
    from workbench_fixtures import (  # type: ignore[import-not-found]
        StubBoundaryProbe,
        build_demo_worktree,
        fake_argv_prefix,
        write_fake_cli,
        write_runtime_profile,
    )

    from harness.presentation.local_ui.api import LocalUiApi, Response
    from harness.presentation.local_ui.composition import WorkbenchSetup, build_services
    from harness.presentation.local_ui.server import start_server

    class LayoutBoundaryProbe(StubBoundaryProbe):
        def verdict(self, provider_id: str) -> Any:
            if provider_id == "codex":
                return super().verdict(provider_id)
            from harness.ports.cli_workbench import BoundaryVerdict

            return BoundaryVerdict(
                satisfied=False,
                blocking_reasons=("SYNTHETIC_BOUNDARY_UNAVAILABLE_" + "scope" * 18,),
                details={"measured": False, "test_purpose": "long rejection layout"},
            )

    class LayoutApi(LocalUiApi):
        def _providers(self) -> Response:
            # 公開入力では参照用の旧SDK一覧は未設定。表示だけの合成応答を明示し、
            # 所有者の設定/履歴を持ち込まない。送信可否と作用経路は変更しない。
            original = super()._providers()
            payload = json.loads(original.body)
            payload["message"] = "表示検査用の合成Provider一覧です。送信には使いません。"
            payload["any_send_allowed"] = False
            payload["providers"] = [
                {
                    "provider_id": "synthetic-layout-readonly",
                    "route_class": "LAYOUT_ONLY",
                    "enabled": False,
                    "contract_state": "UNVERIFIED",
                    "endpoint_configured": False,
                    "model_configured": False,
                    "secret_ref_configured": False,
                    "api_status": "NOT_IMPLEMENTED",
                    "chat_send_allowed": False,
                    "note": "合成の長い拒否理由：" + "UNVERIFIED_LAYOUT_ONLY_" + "scope" * 18,
                }
            ]
            return Response(
                status=200,
                body=json.dumps(payload, ensure_ascii=False).encode(),
                content_type="application/json; charset=utf-8",
            )

    root = scenario.root / "w1-workbench"
    root.mkdir(parents=True)
    worktree = build_demo_worktree(root / "demo")
    originals = {name: (worktree / name).read_text("utf-8") for name in ("hello.py", "notes.md")}
    state_dir = root / "state"
    (root / "home").mkdir()
    profile = write_runtime_profile(
        root / "profile.json",
        executables={
            provider: fake_argv_prefix(
                write_fake_cli(root / "bin", provider=provider, name=f"fake-{provider}.py")
            )
            for provider in ("codex", "claude", "gemini")
        },
        home=root / "home",
        neutral_workdir=state_dir / "cwd",
        state_dir=state_dir,
        cli_runtime_root=root / "bin",
    )
    # Fresh public configuration: no original docs/decision, audit, Git history or HOME.
    public_repo = root / "public-source-inputs"
    for name in ("design-source", "schemas", "spec", "src/harness/masking/ucd"):
        shutil.copytree(REPO / name, public_repo / name)
    shutil.copy2(REPO / "registry-snapshot.json", public_repo / "registry-snapshot.json")
    snapshot = json.loads((public_repo / "registry-snapshot.json").read_bytes())
    design = "design-v" + snapshot["design_version"] + "-runtime-go.md"
    shutil.copy2(REPO / design, public_repo / design)
    runner = CountingRunner()
    services = build_services(
        repo_root=public_repo,
        database_path=root / "db" / "state.sqlite3",
        artifact_root=root / "cas",
        workbench=WorkbenchSetup(
            workspace=worktree,
            runtime_profile=profile,
            workspace_label="gh01-synthetic",
            auth_session="local-uid:" + str(os.getuid()),
            runner=runner,
            boundary_probe=LayoutBoundaryProbe(),
        ),
    )
    server = start_server(
        build_api=lambda origin, token, assets: LayoutApi(
            services, origin=origin, session_token=token, assets=assets
        ),
        port=0,
        max_body_bytes=services.max_body_bytes,
    )
    log = io.StringIO()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    instruction = "説明コメントを足してください。" + XSS_TEXT
    try:
        with contextlib.redirect_stdout(log):
            thread.start()
            browser.call("open", tab="W", url=server.url + "/", width=1280, height=900)
            browser.wait("W", "document.getElementById('wb-provider').options.length > 0")
            browser.wait("W", "document.getElementById('wb-target').options.length >= 2")
            browser.eval("W", "chooseWorkspacePurpose('file_edit')")
            # Cards are selection controls only: no generation, grant or persistence.
            cards_before = len(runner.calls)
            browser.call("click", tab="W", selector=".provider-codex")
            browser.wait(
                "W",
                "document.querySelector('.provider-codex').getAttribute('aria-pressed')==='true'",
            )
            disabled_card = browser.eval("W", "document.querySelector('.provider-claude').disabled")
            browser.call("click", tab="W", selector=".provider-claude")
            provider_after_disabled = browser.eval(
                "W", "document.getElementById('wb-provider').value"
            )
            browser.call("click", tab="W", selector='[data-nav-target="wb-history"]')
            browser.wait(
                "W",
                "document.querySelector('[data-nav-target=wb-history]').getAttribute('aria-current')==='page'",
            )
            browser.call("click", tab="W", selector='[data-nav-target="workbench-panel"]')
            unknown_projection = browser.eval(
                "W",
                """(() => {
                  renderWorkbenchProgress({state:'UNRECOGNIZED_UI_FIXTURE'});
                  const observed = {
                    complete:document.querySelectorAll('.workflow-step.is-complete').length,
                    current:document.querySelectorAll('.workflow-step[aria-current]').length,
                    warning:document.getElementById('wb-workflow-status').classList.contains('is-attention'),
                    label:document.getElementById('wb-workflow-status').textContent};
                  renderWorkbenchProgress({state:'DRAFTED'});
                  return observed;})()""",
            )
            card_calls_after = len(runner.calls)
            navigation_after = browser.eval("W", "window.location.hash")
            layout_checks = workbench_layouts(browser, report, "W")
            browser.eval(
                "W",
                """(() => {
                  const set = (id, value) => { const el = document.getElementById(id);
                    el.value = value; el.dispatchEvent(new Event("change", {bubbles: true})); };
                  set("wb-provider", "codex");
                  return true; })()""",
            )
            browser.wait(
                "W",
                "Array.from(document.getElementById('wb-model').options)"
                ".some((o) => o.value === 'gpt-5.6-sol')",
            )
            local_calls_before = len(runner.calls)
            local_context = workbench_local_context(browser, "W")
            local_calls_after = len(runner.calls)
            browser.eval(
                "W",
                f"""(() => {{
                  const model = document.getElementById("wb-model");
                  model.value = "gpt-5.6-sol";
                  model.dispatchEvent(new Event("change", {{bubbles: true}}));
                  const effort = document.getElementById("wb-effort");
                  effort.value = "high";
                  effort.dispatchEvent(new Event("change", {{bubbles: true}}));
                  const target = document.getElementById("wb-target");
                  Array.from(target.options).forEach((o) => {{
                    o.selected = o.value === "hello.py" || o.value === "notes.md"; }});
                  const text = document.getElementById("wb-instruction");
                  text.value = {json.dumps(instruction)};
                  text.dispatchEvent(new Event("input", {{bubbles: true}}));
                  return true; }})()""",
            )
            selected = browser.eval("W", WB_STATE)
            browser.call("click", tab="W", selector="#wb-preference-approve")
            browser.call("click", tab="W", selector="#wb-preference-save")
            browser.wait(
                "W",
                "document.getElementById('wb-preference-status').textContent.includes('設定を保存しました')",
            )
            preference_calls = len(runner.calls)
            browser.call("click", tab="W", selector="#wb-create")
            browser.wait("W", "!document.getElementById('wb-confirm').hidden")
            browser.wait("W", "document.getElementById('wb-payload').textContent.length > 0")
            first_confirm = browser.eval("W", WB_STATE)
            calls_before_send = len(runner.calls)
            shot(browser, report, "W", "w1-send-confirmation", selector="#wb-confirm")
            browser.call("click", tab="W", selector="#wb-approve-send")
            browser.wait("W", "!document.getElementById('wb-diff').hidden", timeout=90)
            first_diff = browser.eval("W", WB_STATE)
            untouched_before_apply = (worktree / "hello.py").read_text("utf-8") == originals[
                "hello.py"
            ]
            browser.eval(
                "W",
                """(() => {
              const original = wbApi;
              const source = wbSession.session_id;
              window.__heldHistory = false;
              window.__releaseHistory = null;
              wbApi = function(path, options) {
                const response = original(path, options);
                if (path === "/api/workbench/context-preview" &&
                    options && JSON.parse(options.body).parent_session_id === source &&
                    wbSession && wbSession.state === "APPLIED") {
                  return response.then(result => new Promise(resolve => {
                    window.__heldHistory = true;
                    window.__releaseHistory = () => { wbApi = original; resolve(result); };
                  }));
                }
                return response;
              };
              return true;
            })()""",
            )
            browser.call("click", tab="W", selector="#wb-approve-apply")
            browser.wait("W", "window.__heldHistory === true")
            waiting_history = browser.eval(
                "W", "document.getElementById('wb-next-target').disabled"
            )
            browser.eval("W", "window.__releaseHistory(); true")

            browser.wait(
                "W",
                "document.getElementById('wb-apply-result').textContent.includes('適用した')",
                timeout=60,
            )
            browser.wait("W", "!document.getElementById('wb-next-target').disabled", timeout=30)
            first_done = browser.eval("W", WB_STATE)
            shot(browser, report, "W", "w1-first-applied", selector="#wb-diff")
            hello_after = (worktree / "hello.py").read_text("utf-8")
            notes_untouched = (worktree / "notes.md").read_text("utf-8") == originals["notes.md"]
            browser.call("click", tab="W", selector="#wb-next-target")
            browser.wait(
                "W",
                "document.getElementById('wb-confirm-rows').innerText.includes('notes.md')",
            )
            browser.wait("W", "!document.getElementById('wb-approve-send').disabled")
            second_confirm = browser.eval("W", WB_STATE)
            calls_before_second = len(runner.calls)
            browser.call("click", tab="W", selector="#wb-approve-send")
            browser.wait(
                "W",
                "!document.getElementById('wb-diff').hidden && "
                "!document.getElementById('wb-approve-apply').disabled",
                timeout=90,
            )
            browser.call("click", tab="W", selector="#wb-approve-apply")
            browser.wait(
                "W",
                "document.getElementById('wb-apply-result').textContent.includes('適用した')",
                timeout=60,
            )
            browser.wait(
                "W",
                "Array.from(document.getElementById('wb-session-rows').rows)"
                ".filter((r) => r.cells[1].textContent === 'APPLIED').length === 2",
                timeout=30,
            )
            final = browser.eval("W", WB_STATE)
            shot(browser, report, "W", "w1-second-applied", selector="#wb-queue")
            observed = browser.call("observations", tab="W")
            scenario.keep_events("w1", observed)
            calls_before_reload = len(runner.calls)
            browser.call("reload", tab="W")
            browser.wait(
                "W",
                "document.getElementById('wb-preference-status').textContent.includes('保存した設定を読み込みました')",
            )
            restored_preference = browser.eval(
                "W",
                """(() => ({
                provider: document.getElementById('wb-provider').value,
                model: document.getElementById('wb-model').value,
                effort: document.getElementById('wb-effort').value,
                approval: document.getElementById('wb-preference-approve').checked
            }))()""",
            )
            unsupported = http(
                server.url,
                "/api/workbench/sessions",
                {
                    "provider_id": "codex",
                    "model_id": "gpt-5.6-sol",
                    "relative_path": "hello.py",
                    "instruction": "x",
                    "reasoning_effort": "ultra",
                },
                token=server.session_token,
                origin=server.url,
            )
        # Measurement lines are emitted outside the captured production HTTP log.
        report.record(
            "B-09",
            "会話の取得完了まで次ファイル操作を有効にしない",
            PATH_WORKBENCH,
            waiting_history,
            next_disabled_while_history_loading=waiting_history,
        )

        report.record(
            "B-09",
            "Local note, snapshot, context and explicit draft copy work without provider calls",
            PATH_WORKBENCH,
            local_context["realConnections"] > 0
            and local_context["legacyClosed"]
            and local_context["oldSaveDisabled"]
            and local_context["snapshot_required_before_preview"]
            and local_context["context"]["error"] == ""
            and local_context["context"]["previewVisible"]
            and local_context["context"]["selectedRows"] == 1
            and local_context["copied"]
            and local_context["overwrite"]["retained"]
            and local_context["overwrite"]["explained"]
            and local_calls_before == local_calls_after,
            observed=local_context,
            provider_calls_before=local_calls_before,
            provider_calls_after=local_calls_after,
        )
        report.record(
            "B-09",
            "AI cards select only verified CLI; blocked card cannot select or send",
            PATH_WORKBENCH,
            disabled_card
            and provider_after_disabled == "codex"
            and card_calls_after == cards_before,
            provider_after_disabled=provider_after_disabled,
            calls_before=cards_before,
            calls_after=card_calls_after,
        )
        report.record(
            "B-10",
            "Workspace anchor navigation stays local and keeps provider calls at zero",
            PATH_WORKBENCH,
            card_calls_after == cards_before,
            navigation=navigation_after,
            calls_before=cards_before,
            calls_after=card_calls_after,
        )
        report.record(
            "B-10",
            "Unknown presentation state is attention without invented completion",
            PATH_WORKBENCH,
            unknown_projection["complete"] == 0
            and unknown_projection["current"] == 0
            and unknown_projection["warning"]
            and unknown_projection["label"] == "状態を確認できません",
            observed=unknown_projection,
            fixture="UI_PRESENTATION_STATE_ONLY_NO_STORE_CHANGE",
        )
        for layout in layout_checks:
            name, passed = layout.pop("name"), layout.pop("passed")
            report.record("B-10", name, PATH_WORKBENCH, passed, **layout)
        report.record(
            "PUBLIC-SETTINGS",
            "旧Owner回答なしで設定を保存・再読込み。保存だけではCLI起動なし",
            PATH_WORKBENCH,
            preference_calls == 0
            and not (public_repo / "docs/decision").exists()
            and len(runner.calls) == calls_before_reload
            and restored_preference
            == {
                "provider": "codex",
                "model": "gpt-5.6-sol",
                "effort": "high",
                "approval": False,
            },
            preference_calls=preference_calls,
            restored=restored_preference,
            private_owner_packages_present=(public_repo / "docs/decision").exists(),
        )
        report.record(
            "B-09",
            "Provider/モデル/対応推論値の選択肢と、未対応推論値のサーバー拒否",
            PATH_WORKBENCH,
            "codex" in selected["providers"]
            and "gpt-5.6-sol" in selected["models"]
            and selected["effortOptions"] == ["", "low", "medium", "high", "xhigh", "max"]
            and not selected["effortDisabled"]
            and 400 <= unsupported[0] < 500,
            effort_options=selected["effortOptions"],
            unsupported_effort_status=unsupported[0],
        )
        report.record(
            "B-09",
            "完全payload確認までCLI起動0、送信承認後に1回・argvへモデル/推論値",
            PATH_WORKBENCH,
            calls_before_send == 0
            and "gpt-5.6-sol" in first_confirm["confirmRows"]
            and "high" in first_confirm["confirmRows"]
            and "hello.py" in first_confirm["confirmRows"]
            and "説明コメントを足してください。" in first_confirm["payload"]
            and len(runner.calls) == 2
            and all(call["argv_has_model"] and call["argv_has_effort"] for call in runner.calls)
            and calls_before_second == 1,
            calls=runner.calls,
            calls_before_send=calls_before_send,
        )
        report.record(
            "B-09",
            "差分は別承認で適用、1ファイルずつ順次（2件目は1件目の適用後）",
            PATH_WORKBENCH,
            untouched_before_apply
            and hello_after == first_diff["after"]
            and hello_after != originals["hello.py"]
            and notes_untouched
            and "notes.md" in second_confirm["confirmRows"]
            and (worktree / "notes.md").read_text("utf-8") != originals["notes.md"]
            and final["history"].count("APPLIED") == 2
            and not first_done["nextDisabled"],
            history=final["history"],
        )
        report.record(
            "B-08",
            "依頼文のHTMLをpayload/確認表でtextとして表示し実行しない",
            PATH_WORKBENCH,
            final["injected"] == 0
            and final["xss"] is None
            and "<img src=x" in first_confirm["payload"],
            injected_nodes=final["injected"],
            xss_flag=final["xss"],
        )
    finally:
        with contextlib.suppress(BridgeError):
            browser.call("close_tab", tab="W")
        server.shutdown()
        thread.join(timeout=10)
        services.close()
        text = log.getvalue()
        result = check_log(text, [server.session_token])
        report.record(
            "B-08",
            "WorkbenchサーバーのLogにToken無し・Method+Statusのみ",
            PATH_WORKBENCH,
            not result["malformed"] and not result["leaked_secret_index"],
            **result,
        )


# -- 入口 -------------------------------------------------------------------------


def git(args: list[str]) -> str:
    git_path = shutil.which("git") or "/usr/bin/git"
    result = subprocess.run(  # noqa: S603 - 固定のgit・argv list
        [git_path, *args], cwd=REPO, check=False, capture_output=True, text=True, timeout=60
    )
    return result.stdout.strip()


def environment() -> dict[str, Any]:
    release = Path("/proc/sys/kernel/osrelease")
    kernel = release.read_text("utf-8").strip() if release.exists() else platform.release()
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "wsl": "microsoft" in kernel.lower(),
        "euid_root": os.geteuid() == 0,
    }


def write_result(report: Report, meta: dict[str, Any], overall: str) -> Path:
    document = {
        "contract": RUNNER_CONTRACT,
        "overall": overall,
        "evidence_class": "DEVELOPMENT_REGRESSION_ONLY_NOT_RUNTIME_GO",
        **meta,
        "checks": [check.__dict__ for check in report.checks],
        "screenshots": [
            {"path": name, "sha256": hashlib.sha256((report.out / name).read_bytes()).hexdigest()}
            for name in report.screenshots
        ],
    }
    path = report.out / "result.json"
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def unavailable(
    report: Report, meta: dict[str, Any], reason: str, error: BaseException | None = None
) -> int:
    """検査を実行できなかった。元の失敗と後始末の結果を分けて残す。

    後始末が確認できなかった場合は、単なる環境不足（UNVERIFIED・3）にせず
    CLEANUP_UNCONFIRMED（4）とする。
    """
    if error is None:
        cleanup: dict[str, Any] = {"status": "NOT_STARTED", "failures": []}
    else:
        outcome = cleanup_outcome(error)
        cleanup = outcome or {
            # 後始末の結果が付いていない失敗は、止めたと確認できていない。
            "status": "UNCONFIRMED",
            "failures": [{"type": "CleanupNotRecorded", "message": "no cleanup outcome"}],
        }
    for check_id, name in ALL_CHECKS:
        report.checks.append(Check(check_id, name, "NOT_RUN", "UNVERIFIED", {"reason": reason}))
    unconfirmed = cleanup["status"] == "UNCONFIRMED"
    overall = "CLEANUP_UNCONFIRMED" if unconfirmed else "UNVERIFIED"
    document = {
        **meta,
        "unavailable_reason": reason,
        "start_failure": None if error is None else describe_error(error),
        "process_cleanup": cleanup,
    }
    write_result(report, document, overall)
    print(f"{overall}: {reason}", flush=True)
    return EXIT_CLEANUP_UNCONFIRMED if unconfirmed else EXIT_UNAVAILABLE


def finish(report: Report, meta: dict[str, Any]) -> int:
    """最終JSONを書き、終了Codeを決める。回収未確認はFAIL/PASSより優先する。"""
    failed = [check for check in report.checks if check.status != "PASS"]
    meta["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    covered = sorted({check.check_id for check in report.checks})
    meta["not_covered"] = [item for item, _ in ALL_CHECKS if item not in covered]
    unconfirmed = bool(report.cleanup_failures)
    meta["process_cleanup"] = {
        "status": "UNCONFIRMED" if unconfirmed else "CONFIRMED",
        "failures": report.cleanup_failures,
    }
    if unconfirmed:
        overall, code = "CLEANUP_UNCONFIRMED", EXIT_CLEANUP_UNCONFIRMED
    elif failed:
        overall, code = "FAIL", EXIT_FAIL
    else:
        overall, code = "PASS", EXIT_PASS
    path = write_result(report, meta, overall)
    print(
        f"result: {path} {overall} PASS={len(report.checks) - len(failed)} FAIL={len(failed)}"
        f" CLEANUP_UNCONFIRMED={len(report.cleanup_failures)}"
    )
    return code


def scenario_conversation_handoff(scenario: Scenario) -> None:
    """Real browser/HTTP/SQLite and fake CLI processes; no live provider calls."""
    report, browser = scenario.report, scenario.need_browser()
    sys.path.insert(0, str(REPO / "tests" / "support"))
    from workbench_fixtures import (  # type: ignore[import-not-found]
        StubBoundaryProbe,
        build_demo_worktree,
        fake_argv_prefix,
        write_fake_cli,
        write_runtime_profile,
    )

    from harness.presentation.local_ui.api import LocalUiApi
    from harness.presentation.local_ui.composition import WorkbenchSetup, build_services
    from harness.presentation.local_ui.server import start_server

    root = scenario.root / "h1-conversation"
    root.mkdir()
    worktree = build_demo_worktree(root / "demo")
    home = root / "home"
    home.mkdir()
    state = root / "state"
    profile = write_runtime_profile(
        root / "profile.json",
        executables={
            provider: fake_argv_prefix(
                write_fake_cli(root / "bin", provider=provider, name=f"fake-{provider}.py")
            )
            for provider in ("codex", "claude", "gemini")
        },
        home=home,
        neutral_workdir=state / "cwd",
        state_dir=state,
        cli_runtime_root=root / "bin",
    )
    public_repo = root / "public-source-inputs"
    for name in ("design-source", "schemas", "spec", "src/harness/masking/ucd"):
        shutil.copytree(REPO / name, public_repo / name)
    shutil.copy2(REPO / "registry-snapshot.json", public_repo / "registry-snapshot.json")
    snapshot = json.loads((public_repo / "registry-snapshot.json").read_bytes())
    design = "design-v" + snapshot["design_version"] + "-runtime-go.md"
    shutil.copy2(REPO / design, public_repo / design)
    runner = CountingRunner()
    services = build_services(
        repo_root=public_repo,
        database_path=root / "db" / "state.sqlite3",
        artifact_root=root / "cas",
        workbench=WorkbenchSetup(
            workspace=worktree,
            runtime_profile=profile,
            workspace_label="handoff-synthetic",
            auth_session="local-uid:" + str(os.getuid()),
            runner=runner,
            boundary_probe=StubBoundaryProbe(),
        ),
    )
    server = start_server(
        build_api=lambda origin, token, assets: LocalUiApi(
            services, origin=origin, session_token=token, assets=assets
        ),
        port=0,
        max_body_bytes=services.max_body_bytes,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    log = io.StringIO()
    requests: list[dict[str, Any]] = []
    latest: str | None = None
    try:
        with contextlib.redirect_stdout(log):
            thread.start()
            browser.call("open", tab="H", url=server.url + "/", width=1280, height=900)
            browser.wait("H", "document.getElementById('wb-target').options.length >= 2")
            browser.eval("H", "chooseWorkspacePurpose('file_edit')")
            browser.call("click", tab="H", selector="#new-conversation")
            browser.wait("H", "!document.getElementById('detail-panel').hidden")
            browser.eval(
                "H",
                "document.getElementById('message-text').value = '最初の目的を保持してください。'",
            )
            browser.call("click", tab="H", selector="#composer button[type=submit]")
            browser.wait("H", "document.querySelectorAll('#message-rows tr').length === 1")
            browser.call("click", tab="H", selector="#conversation-handoff-workbench")
            before_send = len(runner.calls)
            for index, provider in enumerate(("codex", "claude", "gemini")):
                browser.call("click", tab="H", selector=".provider-" + provider)
                browser.eval(
                    "H",
                    """(() => {
                    const model = document.getElementById('wb-model');
                    model.value='__custom__';
                    model.dispatchEvent(new Event('change',{bubbles:true}));
                    document.getElementById('wb-model-custom').value='fake-model';
                    const target=document.getElementById('wb-target');
                    Array.from(target.options).forEach(o=>{o.selected=o.value==='hello.py';});
                    })()""",
                )
                browser.eval(
                    "H",
                    "document.getElementById('wb-instruction').value = "
                    + json.dumps(f"第{index + 1}の依頼です。先行の会話を保持してください。"),
                )
                browser.call("click", tab="H", selector="#wb-create")
                browser.wait(
                    "H",
                    "document.getElementById('wb-payload').textContent.includes("
                    + json.dumps(f"第{index + 1}の依頼")
                    + ")",
                )
                browser.wait("H", "!document.getElementById('wb-approve-send').disabled")
                payload = json.loads(
                    browser.eval("H", "document.getElementById('wb-payload').textContent")
                )
                requests.append(payload)
                browser.call("click", tab="H", selector="#wb-approve-send")
                browser.wait(
                    "H",
                    "!document.getElementById('wb-diff').hidden && "
                    "!document.getElementById('wb-approve-apply').disabled",
                    timeout=90,
                )
                browser.call("click", tab="H", selector="#wb-approve-apply")
                browser.wait(
                    "H",
                    "document.getElementById('wb-apply-result').textContent.includes('適用した')",
                    timeout=60,
                )
                browser.wait(
                    "H",
                    "document.getElementById('wb-handoff-status').textContent.includes('省略なし')",
                )
                latest = browser.eval("H", "document.getElementById('wb-thread-source').value")
            browser.call("click", tab="H", selector="#wb-conversation-details > summary")
            browser.wait(
                "H",
                "document.querySelectorAll('#wb-conversation-timeline .handoff-turn').length === 4",
            )
            shot(browser, report, "H", "h1-full-history", selector="#wb-form")
            rendered = browser.eval(
                "H", "document.getElementById('wb-conversation-timeline').textContent"
            )
            calls = len(runner.calls)
            browser.call("reload", tab="H")
            browser.wait("H", "document.getElementById('wb-thread-source').options.length >= 4")
            browser.eval(
                "H",
                "(() => {const s=document.getElementById('wb-thread-source');s.value="
                + json.dumps(latest)
                + ";s.dispatchEvent(new Event('change',{bubbles:true}));})()",
            )
            browser.wait(
                "H",
                "document.querySelectorAll('#wb-conversation-timeline .handoff-turn').length === 4",
            )
            restored = browser.eval("H", "document.getElementById('wb-handoff-status').textContent")
            browser.eval(
                "H",
                """(() => {
                  const model=document.getElementById('wb-model');
                  model.value='__custom__';
                  model.dispatchEvent(new Event('change',{bubbles:true}));
                  document.getElementById('wb-model-custom').value='fake-model';
                  Array.from(document.getElementById('wb-target').options).forEach(
                    o=>{o.selected=o.value==='hello.py';});
                  document.getElementById('wb-history-count').value='1';
                  document.getElementById('wb-history-mode').value='recent';
                  document.getElementById('wb-history-mode').dispatchEvent(
                      new Event('change',{bubbles:true}));
                  document.getElementById('wb-important-notes').value='公開関数の引数を変えない。';
                  document.getElementById('wb-instruction').value='履歴を最近1件に選んだ依頼です。';
                  return true;
                })()""",
            )
            browser.wait(
                "H",
                "document.getElementById('wb-handoff-status').textContent.includes('除外 2件')",
            )
            recent_preview = browser.eval(
                "H", "document.getElementById('wb-handoff-status').textContent"
            )
            browser.call("click", tab="H", selector="#wb-create")
            browser.wait(
                "H",
                "document.getElementById('wb-payload').textContent.includes('履歴を最近1件')",
            )
            recent_payload = json.loads(
                browser.eval("H", "document.getElementById('wb-payload').textContent")
            )
            history = recent_payload["conversation_history"]
            report.record(
                "B-12",
                "最近1件を明示選択し、最初の制約・重要事項を残して承認前の生成は0",
                PATH_WORKBENCH,
                len(history["turns"]) == 1
                and history["turns"][0]["provider_id"] == "gemini"
                and history["local_messages"][0]["body"] == "最初の目的を保持してください。"
                and recent_payload["important_notes"] == "公開関数の引数を変えない。"
                and history["selection"]["excluded_count"] == 2
                and len(runner.calls) == calls,
                preview=recent_preview,
                selection=history["selection"],
                before=calls,
                after=len(runner.calls),
            )
            shot(browser, report, "H", "h1-selected-history", selector="#wb-form")
            browser.call("click", tab="H", selector="#wb-new-conversation")
            browser.wait(
                "H",
                "document.getElementById('wb-handoff-status').textContent.includes('新しい会話')",
            )
            separated = browser.eval(
                "H",
                "document.getElementById('wb-thread-source').value === '' && "
                "document.getElementById('wb-conversation-timeline').children.length===0",
            )
            observed = browser.call("observations", tab="H")
            scenario.keep_events("h1", observed)
        report.record(
            "B-09",
            "Codex→Claude→Gemini:全会話と適用観測を自動で引き継ぐ",
            PATH_WORKBENCH,
            before_send == 0
            and len(requests) == 3
            and all(r["conversation_history"]["omitted_messages"] == 0 for r in requests)
            and [len(r["conversation_history"]["turns"]) for r in requests] == [0, 1, 2]
            and [t["provider_id"] for t in requests[-1]["conversation_history"]["turns"]]
            == ["codex", "claude"]
            and requests[-1]["conversation_history"]["local_messages"][0]["body"]
            == "最初の目的を保持してください。"
            and all(t["proposal_applied"] for t in requests[-1]["conversation_history"]["turns"])
            and all(
                t["verification"]["receipt_observation"]
                for t in requests[-1]["conversation_history"]["turns"]
            )
            and [c["provider_id"] for c in runner.calls] == ["codex", "claude", "gemini"],
            providers=[c["provider_id"] for c in runner.calls],
            turn_counts=[len(r["conversation_history"]["turns"]) for r in requests],
            execution_kind="REAL_PROCESS_FAKE_CLI_STUB_BOUNDARY_NO_LIVE_MODELS",
        )
        report.record(
            "B-09",
            "再読込み後の会話継続と新会話の分離",
            PATH_WORKBENCH,
            "第1の依頼" in rendered
            and "第2の依頼" in rendered
            and "第3の依頼" in rendered
            and "3依頼" in restored
            and separated
            and len(runner.calls) == calls,
            restored=restored,
            new_conversation_empty=separated,
            calls_before=calls,
            calls_after=len(runner.calls),
        )
        report.record(
            "B-09",
            "会話本文と生成結果の表示からコードを実行しない",
            PATH_WORKBENCH,
            not any(
                e.get("kind") == "exception"
                or (e.get("kind") == "console" and e.get("level") == "error")
                for e in observed.get("events", [])
            )
            and posts(observed, "/api/workbench/sessions") == 4,
            observations=observed,
        )
    except Exception as error:
        report.error("B-09", "会話全体のProvider切替", PATH_WORKBENCH, error)
        with contextlib.suppress(BridgeError):
            shot(browser, report, "H", "h1-failure")
    finally:
        (report.out / "h1-production-http.log").write_text(log.getvalue(), encoding="utf-8")
        with contextlib.suppress(BridgeError):
            browser.call("close_tab", tab="H")
        report.release("handoff:server.shutdown", server.shutdown)
        thread.join(timeout=10)
        report.release("handoff:services.close", services.close)


def scenario_chatgpt(scenario: Scenario) -> None:
    """Real browser, callback HTTP, verified fixture JWT and SSE; no live account/inference."""
    report, browser = scenario.report, scenario.need_browser()
    sys.path.insert(0, str(REPO / "tests" / "support"))
    from chatgpt_fixtures import NOW, FixtureTransport, callback
    from workbench_fixtures import build_demo_worktree

    from harness.presentation.local_ui.api import LocalUiApi
    from harness.presentation.local_ui.composition import WorkbenchSetup, build_services
    from harness.presentation.local_ui.server import start_server

    root = scenario.root / "g1-chatgpt"
    root.mkdir()
    worktree = build_demo_worktree(root / "demo")
    before = (worktree / "hello.py").read_bytes()
    inputs = root / "source-inputs"
    for name in ("design-source", "schemas", "spec", "src/harness/masking/ucd"):
        shutil.copytree(REPO / name, inputs / name)
    shutil.copy2(REPO / "registry-snapshot.json", inputs / "registry-snapshot.json")
    snapshot = json.loads((inputs / "registry-snapshot.json").read_bytes())
    design = "design-v" + snapshot["design_version"] + "-runtime-go.md"
    shutil.copy2(REPO / design, inputs / design)
    runner = CountingRunner()
    services = build_services(
        repo_root=inputs,
        database_path=root / "db" / "state.sqlite3",
        artifact_root=root / "cas",
        workbench=WorkbenchSetup(
            workspace=worktree,
            runtime_profile=None,
            workspace_label="chatgpt-synthetic",
            auth_session="local-uid:" + str(os.getuid()),
            runner=runner,
        ),
    )
    gateway = services.workbench
    if gateway is None or gateway.chatgpt_connection is None:
        raise RuntimeError("ChatGPT connection not composed")
    auth = gateway.chatgpt_connection.auth
    transport = FixtureTransport()
    transport.auth = auth
    auth.transport = transport
    auth.now = lambda: NOW
    server = start_server(
        build_api=lambda origin, token, assets: LocalUiApi(
            services, origin=origin, session_token=token, assets=assets
        ),
        port=0,
        max_body_bytes=services.max_body_bytes,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    log = io.StringIO()
    try:
        with contextlib.redirect_stderr(log):
            thread.start()
            browser.call("open", tab="G", url=server.url + "/", width=1280, height=900)
            browser.wait("G", "document.getElementById('wb-target').options.length >= 2")
            browser.eval("G", "chooseWorkspacePurpose('file_edit')")
            # Prevent navigation to OpenAI. The official URL is checked, not visited.
            browser.eval(
                "G", "window.open = () => ({opener:null, location:{}, close:()=>{}}); true"
            )
            browser.call("click", tab="G", selector="#wb-chatgpt-login")
            browser.wait("G", "!document.getElementById('wb-chatgpt-auth-link').hidden")
            login_url = browser.eval("G", "document.getElementById('wb-chatgpt-auth-link').href")
            official_url = login_url.startswith("https://auth.openai.com/api/accounts/authorize?")
            browser.call(
                "open",
                tab="O",
                url=auth._attempt.redirect_uri + "?" + callback(auth).split("?", 1)[1],
                width=640,
                height=480,
            )
            browser.wait(
                "G",
                "document.getElementById('wb-chatgpt-status').textContent.includes('ログイン済み')",
            )
            browser.eval(
                "G",
                """(() => {
              const s=document.getElementById('wb-provider');s.value='chatgpt';
              s.dispatchEvent(new Event('change',{bubbles:true}));return true;
            })()""",
            )
            browser.wait("G", "document.getElementById('wb-model').options.length === 2")
            model_options = browser.eval(
                "G",
                "Array.from(document.getElementById('wb-model').options).map(o=>({id:o.value,label:o.textContent}))",
            )
            browser.eval(
                "G",
                """(() => {
              document.getElementById('wb-model').value='fixture-model';
              document.getElementById('wb-model').dispatchEvent(new Event('change',{bubbles:true}));
              document.getElementById('wb-target').value='hello.py';
              document.getElementById('wb-instruction').value='日本語の説明コメントを追加してください。';
              document.getElementById('wb-history-mode').value='recent';
              document.getElementById('wb-history-mode').dispatchEvent(
                  new Event('change',{bubbles:true}));
              document.getElementById('wb-important-notes').value='外部ツールを実行しない。';
              return true;
            })()""",
            )
            browser.call("click", tab="G", selector="#wb-create")
            browser.wait("G", "document.getElementById('wb-payload').textContent.length>0")
            wire_text = browser.eval("G", "document.getElementById('wb-payload').textContent")
            wire = json.loads(wire_text)
            no_generation_before_approval = not any(
                url.endswith("/responses") for _, url, _ in transport.calls
            )
            shot(browser, report, "G", "g1-chatgpt-confirmation", selector="#workbench-panel")
            browser.call("click", tab="G", selector="#wb-approve-send")
            browser.wait("G", "!document.getElementById('wb-diff').hidden", timeout=60)
            unchanged_before_apply = (worktree / "hello.py").read_bytes() == before
            browser.call("click", tab="G", selector="#wb-approve-apply")
            browser.wait(
                "G", "document.getElementById('wb-apply-result').textContent.includes('適用した')"
            )
            shot(browser, report, "G", "g1-chatgpt-applied", selector="#wb-diff")
            browser.call("reload", tab="G")
            browser.wait(
                "G",
                "document.getElementById('wb-chatgpt-status').textContent.includes('ログイン済み')",
            )
            observations = browser.call("observations", tab="G")
        sent = [body for _, url, body in transport.calls if url.endswith("/responses")]
        report.record(
            "B-11",
            "ChatGPT:署名付き合成OAuthからモデル取得・別承認で生成/適用",
            "REAL_BROWSER+PRODUCTION_HTTP+SYNTHETIC_SIGNED_OAUTH_AND_SSE",
            official_url
            and no_generation_before_approval
            and unchanged_before_apply
            and wire["store"] is False
            and wire["stream"] is True
            and sent == [wire_text.encode()]
            and (worktree / "hello.py").read_text() == "# 合成コメント\nvalue = 1\n"
            and len(runner.calls) == 0
            and model_options[-1] == {"id": "fixture-model", "label": "合成モデル"},
            inference_requests=len(sent),
            cli_spawns=len(runner.calls),
            cli_profile_required=False,
            live_provider="NOT_RUN",
            model_options=model_options,
            reviewed_body_matches_sent=sent == [wire_text.encode()],
            no_generation_before_approval=no_generation_before_approval,
            unchanged_before_apply=unchanged_before_apply,
            callbacks="REAL_LOOPBACK_HTTP",
            reload_retains_connection=True,
        )
        report.record(
            "B-10",
            "ChatGPT画面にundefinedやconsole errorがない",
            "REAL_BROWSER+SYNTHETIC_OAUTH_AND_SSE",
            not any(
                e.get("kind") == "exception"
                or (e.get("kind") == "console" and e.get("level") == "error")
                for e in observations.get("events", [])
            ),
        )
    except Exception as error:
        report.error("B-11", "ChatGPT合成接続", "REAL_BROWSER+SYNTHETIC_OAUTH_AND_SSE", error)
        with contextlib.suppress(BridgeError):
            shot(browser, report, "G", "g1-chatgpt-failure")
    finally:
        # Never persist the OAuth tab's URL or raw callback observations.
        (report.out / "g1-production-http.log").write_text(log.getvalue(), encoding="utf-8")
        for tab in ("O", "G"):
            with contextlib.suppress(BridgeError):
                browser.call("close_tab", tab=tab)
        report.release("chatgpt:server.shutdown", server.shutdown)
        thread.join(timeout=10)
        report.release("chatgpt:services.close", services.close)


def scenario_text_tasks(scenario: Scenario) -> None:
    """Real browser, fake CLI process; a text artifact must never acquire an apply plan."""
    sys.path.insert(0, str(REPO))
    from tests.integration.workbench.conftest import make_env

    from harness.presentation.local_ui.api import LocalUiApi
    from harness.presentation.local_ui.server import start_server

    report, browser = scenario.report, scenario.need_browser()
    root = scenario.root / "text-artifacts"
    root.mkdir()
    expected = "# 合成の企画草案\n\n事実: 資料Aは合成。\n" + XSS_TEXT + "\n"
    env = make_env(root, replacement=expected, extra_providers={"claude": "OK"})
    before = tree_digest(env.worktree)
    server = start_server(
        build_api=lambda origin, token, assets: LocalUiApi(
            env.services, origin=origin, session_token=token, assets=assets
        ),
        port=0,
        max_body_bytes=env.services.max_body_bytes,
    )
    log = io.StringIO()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    downloads: list[dict[str, Any]] = []
    try:
        with contextlib.redirect_stdout(log):
            thread.start()
            browser.call("open", tab="T", url=server.url + "/", width=1280, height=900)
            browser.wait("T", "document.getElementById('wb-provider').options.length > 0")
            directory = Path(browser.call("artifact_downloads", origin=server.url)["directory"])
            initial = browser.eval(
                "T",
                """({
                purpose:workspacePurpose(),
                targetDisabled:document.getElementById('wb-target').disabled,
                heading:document.querySelector('h1').textContent,
                purposes:Array.from(document.querySelectorAll('[data-purpose]')).map(p=>p.dataset.purpose)
            })""",
            )
            browser.call("click", tab="T", selector=".provider-codex")
            no_spawn_before_approval = True
            no_approval_before_payload = True
            for index, output_format in enumerate(("markdown", "text")):
                if index:
                    browser.call("click", tab="T", selector="#wb-result-continue")
                    browser.call("click", tab="T", selector=".provider-claude")
                browser.call("click", tab="T", selector='[data-purpose="planning"]')
                browser.eval(
                    "T",
                    """(() => {
                    const model=document.getElementById('wb-model'); model.value='__custom__';
                    model.dispatchEvent(new Event('change',{bubbles:true}));
                    document.getElementById('wb-model-custom').value='fake-model';
                    document.getElementById('wb-instruction').value='資料Aをもとに企画を作成してください。';
                    document.getElementById('wb-reference').value='資料A: 合成条件';
                    return true;
                })()""",
                )
                browser.eval(
                    "T",
                    "document.getElementById('wb-output-format').value="
                    + json.dumps(output_format),
                )
                before_calls = len(env.runner.calls)
                browser.call("observations", tab="T")
                browser.call(
                    "intercept",
                    tab="T",
                    path="/confirmation",
                    method="GET",
                    mode="delay",
                    ms=1200,
                    times=1,
                )
                browser.call("click", tab="T", selector="#wb-create")
                browser.wait("T", "!document.getElementById('wb-confirm').hidden")
                browser.eval("T", "approveAndSend()")
                observed = browser.call("observations", tab="T")
                no_approval_before_payload = (
                    no_approval_before_payload
                    and posts(observed, "/approve-send") == 0
                    and posts(observed, "/send") == 0
                )
                no_spawn_before_approval = (
                    no_spawn_before_approval and len(env.runner.calls) == before_calls
                )

                browser.wait(
                    "T",
                    "!document.getElementById('wb-confirm').hidden && "
                    "!document.getElementById('wb-approve-send').disabled",
                )
                session = browser.eval(
                    "T",
                    "({id:wbSession.session_id, canApply:wbSession.can_apply, "
                    "target:wbSession.target})",
                )
                request = json.loads(
                    browser.eval("T", "document.getElementById('wb-payload').textContent")
                )
                no_spawn_before_approval = (
                    no_spawn_before_approval and len(env.runner.calls) == before_calls
                )
                if request.get("task_kind") != "planning" or "target_relative_path" in request:
                    raise RuntimeError("file-free request was not reviewed")
                browser.call("click", tab="T", selector="#wb-approve-send")
                browser.wait("T", "!document.getElementById('wb-result-card').hidden", timeout=60)
                displayed = browser.eval(
                    "T", "document.getElementById('wb-result-text').textContent"
                )
                if index == 0 and displayed != expected:
                    raise RuntimeError("synthetic output differs from the configured artifact")
                diff_hidden = browser.eval("T", "document.getElementById('wb-diff-card').hidden")
                browser.call("click", tab="T", selector="#wb-result-copy")
                browser.wait(
                    "T",
                    "document.getElementById('wb-result-action-status').textContent.includes('コピーしました')",
                )
                copied = browser.eval("T", "navigator.clipboard.readText()")
                browser.call("click", tab="T", selector="#wb-result-download")
                artifact = env.gateway.result(session["id"])
                path = directory / artifact["filename"]
                deadline = time.monotonic() + 15
                while not path.is_file() and time.monotonic() < deadline:
                    time.sleep(0.1)
                saved = path.read_bytes() if path.is_file() else b""
                history = request.get("conversation_history")
                downloads.append(
                    {
                        "format": output_format,
                        "download_hash": hashlib.sha256(saved).hexdigest(),
                        "matches_result": saved == displayed.encode() == copied.encode(),
                        "no_apply_plan": session["canApply"] is False
                        and session["target"] is None
                        and diff_hidden,
                        "reviewed_body_matches_sent": json.loads(env.runner.payloads[-1])
                        == request,
                        "history_preserved": index == 0
                        or history["turns"][0]["result_text"] == expected,
                        "xss_not_executed": browser.eval(
                            "T",
                            "!window.__gh01_xss && document.querySelectorAll("
                            "'#wb-result-text img,#wb-result-text script').length===0",
                        ),
                    }
                )
                shot(
                    browser, report, "T", "text-result-" + output_format, selector="#wb-result-card"
                )
            for width, dark in ((390, False), (1024, True)):
                browser.call("viewport", tab="T", width=width, height=900, dark=dark)
                shot(browser, report, "T", "text-layout-" + str(width), selector="#wb-form")
            report.record(
                "B-13",
                "文章/Markdownの生成・コピー・実ダウンロード・別AIへの引継ぎ",
                PATH_WORKBENCH,
                initial["purpose"] == "writing"
                and initial["targetDisabled"]
                and all(
                    all(
                        row[key]
                        for key in (
                            "matches_result",
                            "no_apply_plan",
                            "reviewed_body_matches_sent",
                            "history_preserved",
                            "xss_not_executed",
                        )
                    )
                    for row in downloads
                )
                and no_approval_before_payload
                and no_spawn_before_approval
                and tree_digest(env.worktree) == before
                and len(env.runner.calls) == len(downloads),
                no_approval_before_payload=no_approval_before_payload,
                downloads=downloads,
                no_spawn_before_approval=no_spawn_before_approval,
                workspace_unchanged=tree_digest(env.worktree) == before,
                live_provider="NOT_RUN",
                initial=initial,
            )
    except Exception as error:
        report.error("B-13", "汎用AIワークスペース", PATH_WORKBENCH, error)
        with contextlib.suppress(BridgeError):
            shot(browser, report, "T", "text-task-failure", selector="#workbench-panel")
    finally:
        (report.out / "text-tasks-http.log").write_text(log.getvalue(), encoding="utf-8")
        with contextlib.suppress(BridgeError):
            browser.call("close_tab", tab="T")
        report.release("text:server.shutdown", server.shutdown)
        thread.join(timeout=10)
        report.release("text:services.close", env.services.close)


def scenario_knowledge_import(scenario: Scenario) -> None:
    """Browser import/preview/local save/CLI payload review, without provider invocation."""
    sys.path.insert(0, str(REPO))
    from tests.integration.workbench.conftest import make_env
    from tests.unit.ui.test_knowledge_export import exported

    from harness.presentation.local_ui.api import LocalUiApi
    from harness.presentation.local_ui.server import start_server

    report, browser = scenario.report, scenario.need_browser()
    root = scenario.root / "knowledge-import"
    root.mkdir()
    env = make_env(root, extra_providers={"claude": "OK", "gemini": "OK"})
    server = start_server(
        build_api=lambda origin, token, assets: LocalUiApi(
            env.services, origin=origin, session_token=token, assets=assets
        ),
        port=0,
        max_body_bytes=env.services.max_body_bytes,
    )
    log = io.StringIO()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    before = tree_digest(env.worktree)
    checks: dict[str, Any] = {}
    try:
        with contextlib.redirect_stdout(log):
            thread.start()
            browser.call("open", tab="K", url=server.url + "/", width=1280, height=900)
            browser.wait("K", "document.getElementById('wb-provider').options.length > 0")
            browser.eval("K", "document.getElementById('knowledge-panel').open=true")
            synthetic = exported()
            synthetic["mapping"]["answer"]["message"]["content"] = {
                "content_type": "multimodal_text",
                "parts": ["採用した回答", {"asset_pointer": "synthetic-image"}],
            }
            fixture = json.dumps([synthetic], ensure_ascii=False)
            browser.eval(
                "K",
                """(() => {
                  const data = new DataTransfer();
                  data.items.add(new File(["""
                + json.dumps(fixture)
                + """],
                    'conversations.json', {type:'application/json'}));
                  const input=document.getElementById('knowledge-file');
                  input.files=data.files; input.dispatchEvent(new Event('change',{bubbles:true}));
                  return true;
                })()""",
            )
            browser.wait(
                "K", "document.getElementById('knowledge-content').value.includes('採用した回答')"
            )
            selected = browser.eval("K", "document.getElementById('knowledge-content').value")
            source = browser.eval(
                "K", "document.getElementById('knowledge-source-status').textContent"
            )
            checks["only_selected_branch"] = (
                "別の分岐の回答" not in selected
                and "分岐外ノード 1" in source
                and "非テキスト部分 1" in source
            )
            stored_before = db_state(env.services.database_path), tree_digest(env.root / "cas")
            browser.call("click", tab="K", selector="#knowledge-preview")
            browser.wait("K", "!document.getElementById('knowledge-review').hidden")
            checks["preview_no_write"] = stored_before == (
                db_state(env.services.database_path),
                tree_digest(env.root / "cas"),
            )
            browser.eval(
                "K",
                """(() => {
                const title=document.getElementById('knowledge-title');
                title.value='編集したタイトル';
                title.dispatchEvent(new Event('input',{bubbles:true}));
                return true;
            })()""",
            )
            checks["edit_invalidates_review"] = browser.eval(
                "K",
                "document.getElementById('knowledge-review').hidden && "
                "document.getElementById('knowledge-save').disabled",
            )
            browser.call("click", tab="K", selector="#knowledge-preview")
            browser.wait("K", "!document.getElementById('knowledge-review').hidden")
            browser.call("click", tab="K", selector="#knowledge-approve-save")
            browser.call("click", tab="K", selector="#knowledge-save")
            browser.wait("K", "knowledgeSavedId!==null && !knowledgeSaving")
            exported_id = browser.eval("K", "knowledgeSavedId")
            checks["export_saved"] = len(env.services.conversations.list_messages(exported_id)) == 1
            browser.eval(
                "K",
                """(() => {
                const kind=document.getElementById('knowledge-kind');
                kind.value='chatgpt_memory'; kind.dispatchEvent(new Event('change',{bubbles:true}));
                document.getElementById('knowledge-title').value='合成メモリー';
                const content=document.getElementById('knowledge-content');
                content.value='日本語で簡潔に説明する。事実と仮定を分ける。';
                content.dispatchEvent(new Event('input',{bubbles:true}));
                return true;
            })()""",
            )
            checks["source_switch_clears_export_claims"] = browser.eval(
                "K",
                "document.getElementById('knowledge-file').files.length===0 && "
                "document.getElementById('knowledge-export-choice').hidden && "
                "!document.getElementById('knowledge-source-status').textContent.includes('分岐')",
            )
            browser.call(
                "intercept",
                tab="K",
                path="/api/knowledge/preview",
                method="POST",
                mode="delay",
                ms=900,
                times=1,
            )
            browser.call("click", tab="K", selector="#knowledge-preview")
            browser.eval(
                "K",
                """(() => {
                  const content=document.getElementById('knowledge-content');
                  content.value+=' 表示した本文を保存する。';
                  content.dispatchEvent(new Event('input',{bubbles:true}));
                  return true;
                })()""",
            )
            browser.wait(
                "K",
                "document.getElementById('knowledge-status').textContent.includes('内容検査中')",
            )
            time.sleep(1.2)
            checks["late_preview_does_not_authorize_edited_content"] = browser.eval(
                "K", "knowledgePreview===null && document.getElementById('knowledge-save').disabled"
            )
            browser.call("click", tab="K", selector="#knowledge-preview")
            browser.wait("K", "!document.getElementById('knowledge-review').hidden")
            reviewed = browser.eval(
                "K", "document.getElementById('knowledge-preview-body').textContent"
            )
            browser.call("click", tab="K", selector="#knowledge-approve-save")
            browser.call("click", tab="K", selector="#knowledge-save")
            browser.wait("K", "knowledgeSavedId!==null && !knowledgeSaving")
            memory_id = browser.eval("K", "knowledgeSavedId")
            browser.call("click", tab="K", selector="#knowledge-use")
            browser.wait(
                "K",
                "document.getElementById('wb-handoff-status').textContent.includes('1メッセージ')",
            )
            checks["use_saved_reference"] = browser.eval("K", "wbSourceConversationId") == memory_id
            checks["all_saved_references_selectable"] = (
                browser.eval(
                    "K",
                    "Array.from(document.getElementById('wb-thread-source').options)"
                    ".filter(o=>o.value.startsWith('saved:')).length",
                )
                == 2
            )
            payload_checks = []
            for provider in ("codex", "claude", "gemini"):
                browser.call("click", tab="K", selector=".provider-" + provider)
                browser.eval(
                    "K",
                    """(() => {
                    const model=document.getElementById('wb-model');
                    model.value='__custom__';
                    model.dispatchEvent(new Event('change',{bubbles:true}));
                    document.getElementById('wb-model-custom').value='fake-model';
                    document.getElementById('wb-instruction').value='この知識を参考に説明してください。';
                    return true;
                })()""",
                )
                old_id = browser.eval("K", "wbSession ? wbSession.session_id : null")
                browser.call("click", tab="K", selector="#wb-create")
                browser.wait(
                    "K",
                    "wbSession && wbSession.session_id !== "
                    + json.dumps(old_id)
                    + " && wbReviewedConfirmation && "
                    "!document.getElementById('wb-approve-send').disabled",
                )
                payload = json.loads(
                    browser.eval("K", "document.getElementById('wb-payload').textContent")
                )
                rows = payload["conversation_history"]["local_messages"]
                payload_checks.append(
                    rows[0]["body"] == reviewed and rows[0]["role"] == "USER_TASK"
                )
            checks["three_cli_review_payloads_match"] = all(payload_checks)
            checks["provider_not_invoked"] = env.runner.calls == []
            checks["workspace_unchanged"] = tree_digest(env.worktree) == before
            browser.eval("K", "document.getElementById('knowledge-panel').open=true")
            shot(browser, report, "K", "knowledge-import-desktop", selector="#knowledge-panel")
            for width in (390, 1024):
                browser.call("viewport", tab="K", width=width, height=900, dark=width == 1024)
                shot(
                    browser,
                    report,
                    "K",
                    "knowledge-import-" + str(width),
                    selector="#knowledge-panel",
                )
            report.record(
                "B-14",
                "Web会話/メモリーの選択・保存・3CLIの送信前確認",
                PATH_WORKBENCH,
                all(checks.values()),
                checks=checks,
                provider_invoked=0,
                live_provider="NOT_RUN",
            )
    except Exception as error:
        report.error("B-14", "Webの知識取込", PATH_WORKBENCH, error)
        with contextlib.suppress(BridgeError):
            shot(browser, report, "K", "knowledge-import-failure", selector="#workbench-panel")
    finally:
        (report.out / "knowledge-http.log").write_text(log.getvalue(), encoding="utf-8")
        with contextlib.suppress(BridgeError):
            browser.call("close_tab", tab="K")
        report.release("knowledge:server.shutdown", server.shutdown)
        thread.join(timeout=10)
        report.release("knowledge:services.close", env.services.close)


SCENARIOS: dict[str, Callable[[Scenario], None]] = {
    "knowledge": scenario_knowledge_import,
    "text-tasks": scenario_text_tasks,
    "read-only": scenario_read_only,
    "orphan": scenario_orphan,
    "failed-copy": scenario_failed_copy,
    "blockers": scenario_blockers,
    "workbench": scenario_workbench,
    "handoff": scenario_conversation_handoff,
    "chatgpt": scenario_chatgpt,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, type=Path, help="新規作成する出力Directory")
    parser.add_argument("--chromium", required=True, help="Chromium/Chrome実行体の絶対Path")
    parser.add_argument("--node", default=shutil.which("node"), help="Node.js 22以上の実行体")
    parser.add_argument(
        "--scenario",
        action="append",
        choices=sorted([*SCENARIOS, "invalid-operator"]),
        help="省略時は全シナリオ",
    )
    args = parser.parse_args(argv)
    out = args.out.resolve()
    if out.exists() or not out.parent.is_dir():
        print("--out must be a new directory whose parent exists", file=sys.stderr)
        return EXIT_USAGE
    if str(out).startswith("/mnt/"):
        print("--out must be on the Linux native filesystem", file=sys.stderr)
        return EXIT_USAGE
    out.mkdir()
    (out / "screenshots").mkdir()
    report = Report(out=out)
    selected = args.scenario or ["invalid-operator", *SCENARIOS]
    meta: dict[str, Any] = {
        "commit": git(["rev-parse", "HEAD"]),
        "worktree_clean": git(["status", "--porcelain"]) == "",
        "scenarios": selected,
        "environment": environment(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    browser_needed = any(name in SCENARIOS for name in selected)
    browser: Browser | None = None
    if browser_needed:
        chromium = Path(args.chromium)
        if not chromium.is_file() or not os.access(chromium, os.X_OK):
            return unavailable(report, meta, "CHROMIUM_EXECUTABLE_NOT_FOUND")
        if not args.node or not Path(args.node).is_file():
            return unavailable(report, meta, "NODE_EXECUTABLE_NOT_FOUND")
        try:
            browser = Browser(args.node, str(chromium), out / "profile", out / "bridge.log")
        except (BridgeError, OSError) as error:
            # Browser() は失敗時にBridge/Chromium/Logを手放し、結果を例外へ付けて投げる。
            return unavailable(report, meta, "BROWSER_START_FAILED", error)
        try:
            meta["browser"] = browser.call("version")
        except (BridgeError, OSError) as error:
            cleanup_after_failure(error, browser.close)
            return unavailable(report, meta, "BROWSER_START_FAILED", error)
    sys.path.insert(0, str(REPO / "src"))
    try:
        if "invalid-operator" in selected:
            try:
                scenario_invalid_operator(report, out / "work")
            except Exception as error:  # FAILとして記録
                report.error("B-02", "無効なoperator session", PATH_CLI_ONLY, error)
        scenario = Scenario(report, browser, out / "work")
        (out / "work").mkdir(exist_ok=True)
        for name in selected:
            if name not in SCENARIOS:
                continue
            try:
                SCENARIOS[name](scenario)
            except Exception as error:  # FAILとして記録し続行
                report.error(name, f"scenario {name}", PATH_RECOVERY, error)
        if browser is not None:
            script = {k: v for k, v in scenario.console.items() if k.endswith(":script")}
            network = {k: v for k, v in scenario.console.items() if k.endswith(":network")}
            report.record(
                "B-10",
                "browser consoleにScript error/exception/CSP違反が無い",
                PATH_RECOVERY,
                not any(script.values()),
                script_errors={k: v[:5] for k, v in script.items() if v},
                expected_network_rejections={k: len(v) for k, v in network.items()},
            )
    finally:
        if browser is not None:
            report.release("browser.close", browser.close)
    return finish(report, meta)


if __name__ == "__main__":
    raise SystemExit(main())
