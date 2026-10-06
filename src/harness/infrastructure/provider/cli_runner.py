"""公式 CLI を 1 回だけ起動する Runner。

## 何をしないか

* **再試行しない。** 失敗も timeout も、そのまま呼出側へ返す。
* **stderr の本文を返さない。** 長さと切詰めの有無だけを返す。未検査の外部
  Bytes を Log・例外・DB へ運ぶ経路を作らない。
* **shell を使わない。** `argv` は `list[str]`、`env` は呼出側が作った固定表だけ。

## 上限は読みながら効かせる

`communicate()` で全部読んでから長さを見ると、上限を超える出力を一度メモリへ
載せてしまう。`selectors` で少しずつ読み、**上限に達した時点で打ち切る**。

## 子と孫をまとめて止める

`start_new_session=True` で新しい Process Group を作り、`killpg` で group ごと
止める。孫が Pipe を掴んだまま残ると `wait()` が返っても読み口が閉じない。
Process の終了を観測したあとは、猶予を過ぎたら **読むのをやめる**。

### 親が先に終わっても後片付けをやめない

以前は `process.poll() is not None` なら即 return していた。親だけ終了して孫が
生き残る形を再現したところ、Runner が戻ったあとも孫が動いていた
（`docs/development/cli-workbench-review-fix-20260908/reproduction-before.json`）。
いまは親の生死にかかわらず Group を畳む。

### PID 再利用を踏まない順序

Group へ signal を送るのは **親を `wait()` で回収する前** だけである。回収前の親は
Zombie として Group に残るので、その Group ID が別の Process へ再利用されることは
ない。回収したあとは、Group に何か残っていても signal を送らない。**他人の Process
を止める方が悪い。** 残っていることは観測結果として返す。

### Group ではなく PID namespace で数える

`setsid()` した子は Process Group からも Session からも離れる。Group だけを見張って
いたので、**離れた子が生きていても `CONFIRMED_EMPTY` を返していた**（独立再現:
`docs/development/cli-workbench-boundary-review-20260908/REVIEW-EVIDENCE.json`、
こちらでの再現: `docs/development/cli-workbench-boundary-fix-20260908/`
`boundary-probe-before.json`）。

いまは Launcher が作る PID namespace の member を数える。namespace からは
`setsid()` でも `fork` でも離脱できないので、これが子孫の全数確認になる。
namespace を特定できない、`/proc` を読めない——どちらも「残っていない」の証拠に
ならないので `UNVERIFIABLE` を返し、呼出側が `EFFECT_UNKNOWN` へ倒す。
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import selectors
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from typing import IO, Final

from harness.infrastructure.provider.process_containment import (
    NamespaceLookupFailed,
    find_sandbox_namespace,
    namespace_members,
)
from harness.ports.cli_workbench import (
    CliLaunchSpec,
    CliOutcome,
    CliRunResult,
    Containment,
    DescendantCleanup,
)

__all__ = ["SubprocessCliRunner"]


@dataclass(frozen=True, slots=True)
class _Pipes:
    """`Popen` が返す 3 本の Pipe。**None 検査を 1 箇所へ寄せる。**"""

    stdin: IO[bytes]
    stdout: IO[bytes]
    stderr: IO[bytes]

    @classmethod
    def of(cls, process: subprocess.Popen[bytes]) -> _Pipes | None:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            return None
        return cls(process.stdin, process.stdout, process.stderr)


_READ_CHUNK: Final[int] = 64 * 1024
#: Process 終了後に Pipe を掴んだ孫を待つ猶予。
_DRAIN_GRACE_SECONDS: Final[float] = 2.0
#: SIGTERM のあと SIGKILL までの猶予。
_TERM_GRACE_SECONDS: Final[float] = 3.0


class SubprocessCliRunner:
    """`CliRunnerPort` の具象。**1 spawn だけを行う。**

    走っている Process を 1 つだけ覚えておく。Server を閉じるときに `stop()` で
    止めないと、Worker Thread が SQLite 接続を使っている最中に接続が閉じられ、
    Process ごと落ちる。**後片付けの順序を型と持ち物で決めておく。**
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: tuple[subprocess.Popen[bytes], int | None, str | None] | None = None

    def stop(self) -> None:
        with self._lock:
            active = self._active
        if active is not None:
            _terminate(*active)

    def run(
        self,
        spec: CliLaunchSpec,
        *,
        stdin_payload: bytes,
        timeout_seconds: int,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> CliRunResult:
        if timeout_seconds < 1 or max_stdout_bytes < 1 or max_stderr_bytes < 1:
            raise ValueError("runner limits must be positive")
        argv = list(spec.argv)
        env = dict(spec.env)
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed argv list, no shell, explicit env
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=spec.cwd,
                env=env,
                close_fds=True,
                # 新しい session を作る。孫まで group ごと止められる。
                start_new_session=True,
            )
        except OSError:
            # execve に失敗した。**この場合だけ「起動していない」と言える。**
            # 何も起きていないので、後片付けは「空だと確認済み」である。
            return replace(
                _result(CliOutcome.SPAWN_FAILED, None, b"", False, 0, False),
                descendant_cleanup=DescendantCleanup.CONFIRMED_EMPTY,
            )
        # `start_new_session=True` なので子は自分の Session と Group の長になる。
        # **Group ID は spawn 直後に控える。** 親が終わったあとでは引けない。
        group = _group_of(process)
        # Launcher が作る PID namespace を突き止める。**ここで取れなければ、
        # あとから子孫の全数を確かめる手段が無い。** 取れなかったことは
        # `UNVERIFIABLE` として持ち回り、「空だった」に読み替えない。
        namespace: str | None = None
        if spec.containment == Containment.PID_NAMESPACE:
            try:
                namespace = find_sandbox_namespace(
                    process.pid, still_running=lambda: process.poll() is None
                )
            except NamespaceLookupFailed:
                namespace = None
        with self._lock:
            self._active = (process, group, namespace)
        deadline = time.monotonic() + timeout_seconds
        pipes = _Pipes.of(process)
        if pipes is None:  # pragma: no cover - PIPE を渡しているので起きない
            cleanup = _terminate(process, group, namespace)
            return cleanup.attach(_result(CliOutcome.UNKNOWN, None, b"", False, 0, False))
        try:
            observed = self._pump(
                process,
                pipes,
                stdin_payload,
                deadline,
                max_stdout_bytes,
                max_stderr_bytes,
                tuple(
                    str(item["path"])
                    for item in spec.runtime_projection.get("executables", [])
                    if isinstance(item, dict) and str(item.get("path", "")).endswith(".js")
                ),
            )
        except BaseException:
            _terminate(process, group, namespace)
            with self._lock:
                self._active = None
            raise
        # 後片付けの結果は **戻り値へ載せる。** `finally` の中で書き換えても
        # 返り値には反映されない。
        cleanup = _terminate(process, group, namespace)
        with self._lock:
            self._active = None
        return cleanup.attach(observed)

    def _pump(
        self,
        process: subprocess.Popen[bytes],
        pipes: _Pipes,
        stdin_payload: bytes,
        deadline: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        diagnostic_sources: tuple[str, ...] = (),
    ) -> CliRunResult:
        stdout = bytearray()
        stderr_seen = 0
        stderr_sample = bytearray()
        stdout_truncated = False
        stderr_truncated = False
        pending = memoryview(stdin_payload)
        selector = selectors.DefaultSelector()
        for stream in (pipes.stdin, pipes.stdout, pipes.stderr):
            os.set_blocking(stream.fileno(), False)
        selector.register(pipes.stdout, selectors.EVENT_READ, "stdout")
        selector.register(pipes.stderr, selectors.EVENT_READ, "stderr")
        if pending:
            selector.register(pipes.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            _close(pipes.stdin)
        exited_at: float | None = None
        try:
            while True:
                now = time.monotonic()
                if now > deadline:
                    return _result(
                        CliOutcome.TIMEOUT,
                        process.poll(),
                        bytes(stdout),
                        stdout_truncated,
                        stderr_seen,
                        stderr_truncated,
                    )
                if not selector.get_map():
                    break
                if exited_at is not None and now - exited_at > _DRAIN_GRACE_SECONDS:
                    # 孫が Pipe を掴んだまま残っている。**読むのをやめて不明で返す。**
                    return _result(
                        CliOutcome.UNKNOWN,
                        process.poll(),
                        bytes(stdout),
                        stdout_truncated,
                        stderr_seen,
                        stderr_truncated,
                    )
                for key, _ in selector.select(timeout=0.2):
                    label = str(key.data)
                    if label == "stdin":
                        pending = _write_some(pipes.stdin, selector, pending)
                        continue
                    stream = pipes.stdout if label == "stdout" else pipes.stderr
                    chunk = _read_some(stream)
                    if chunk is None:
                        selector.unregister(stream)
                        _close(stream)
                        continue
                    if label == "stdout":
                        stdout.extend(chunk)
                        if len(stdout) > max_stdout_bytes:
                            stdout_truncated = True
                            return _result(
                                CliOutcome.OUTPUT_LIMIT_EXCEEDED,
                                process.poll(),
                                bytes(stdout[:max_stdout_bytes]),
                                True,
                                stderr_seen,
                                stderr_truncated,
                            )
                    else:
                        stderr_seen += len(chunk)
                        stderr_sample.extend(chunk[: max(0, 4096 - len(stderr_sample))])
                        if stderr_seen > max_stderr_bytes:
                            stderr_truncated = True
                            return _result(
                                CliOutcome.OUTPUT_LIMIT_EXCEEDED,
                                process.poll(),
                                bytes(stdout),
                                stdout_truncated,
                                stderr_seen,
                                True,
                            )
                if exited_at is None and process.poll() is not None:
                    exited_at = time.monotonic()
        finally:
            selector.close()
        remaining = max(0.0, deadline - time.monotonic())
        try:
            code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            return _result(
                CliOutcome.TIMEOUT,
                None,
                bytes(stdout),
                stdout_truncated,
                stderr_seen,
                stderr_truncated,
            )
        return _result(
            CliOutcome.COMPLETED,
            code,
            bytes(stdout),
            stdout_truncated,
            stderr_seen,
            stderr_truncated,
            stderr_classifications=_classify_stderr(bytes(stderr_sample)),
            stderr_source_locations=_stderr_source_locations(
                bytes(stderr_sample), diagnostic_sources
            ),
        )


def _read_some(stream: IO[bytes]) -> bytes | None:
    """読めた Bytes を返す。EOF は `None`。"""
    try:
        chunk = os.read(stream.fileno(), _READ_CHUNK)
    except BlockingIOError:
        return b""
    except OSError:
        return None
    return chunk if chunk else None


def _write_some(
    stdin: IO[bytes], selector: selectors.BaseSelector, pending: memoryview
) -> memoryview:
    try:
        written = os.write(stdin.fileno(), pending[:_READ_CHUNK])
    except BlockingIOError:
        return pending
    except OSError:
        selector.unregister(stdin)
        _close(stdin)
        return pending[:0]
    pending = pending[written:]
    if not pending:
        # **全部書いたら必ず閉じる。** 閉じないと CLI が入力終端を待ち続ける。
        selector.unregister(stdin)
        _close(stdin)
    return pending


def _close(stream: IO[bytes]) -> None:
    try:
        stream.close()
    except OSError:
        pass


@dataclass(frozen=True, slots=True)
class _Cleanup:
    """後片付けの観測結果。**結論だけでなく残った PID も返す。**"""

    verdict: DescendantCleanup
    residual: tuple[int, ...] = ()

    def attach(self, result: CliRunResult) -> CliRunResult:
        return replace(result, descendant_cleanup=self.verdict, residual_pids=self.residual)


def _group_of(process: subprocess.Popen[bytes]) -> int | None:
    """spawn 直後に Process Group を控える。"""
    try:
        return os.getpgid(process.pid)
    except OSError:  # pragma: no cover - spawn 直後に消えることは通常起きない
        return None


def _terminate(
    process: subprocess.Popen[bytes], group: int | None, namespace: str | None
) -> _Cleanup:
    """子と孫をまとめて止める。**残したまま「空」と言わない。**

    ## Group へ signal を送ってよい条件

    Group ID は Group 長の PID である。**長を回収した瞬間からその番号は再利用され得る**
    ので、回収後に Group へ送ると無関係な Process を止めかねない。回収前の長は Zombie
    として Group を押さえているので、その間だけが安全である。

    `_pump` が正常終了を観測した経路では、ここへ来る前に `wait()` が済んでいる
    （`process.returncode` が埋まっている）。**そのときは Group へ 1 通も送らない。**
    残りは namespace 所属を確かめてから、その PID へだけ送る。
    """
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            _close(stream)

    # `poll()` ではなく `returncode` を見る。`poll()` はここで回収してしまう。
    reaped = process.returncode is not None
    if group is not None and not reaped:
        _signal_group(group, signal.SIGTERM)
        if process.poll() is None:
            try:
                process.wait(timeout=_TERM_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        if process.poll() is None:
            _signal_group(group, signal.SIGKILL)
            try:
                process.wait(timeout=_TERM_GRACE_SECONDS)
            except subprocess.TimeoutExpired:  # pragma: no cover - KILL は効く想定
                pass
    else:
        _stop_process(process)

    try:
        process.wait(timeout=_TERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover - KILL 済みなら起きない
        return _Cleanup(DescendantCleanup.RESIDUAL_PROCESSES, (process.pid,))

    if namespace is None:
        # 全数を確かめる手立てが無い。**確認できていないことを結論にする。**
        return _Cleanup(DescendantCleanup.UNVERIFIABLE)
    return _settle_namespace(namespace)


def _settle_namespace(namespace: str) -> _Cleanup:
    """namespace に残った Process を止め、残数を確かめる。

    所属を列挙した後にpidfdを取得し、所属を再確認してからそのhandleへ送る。
    確認後にPIDが再利用されてもhandleは元のプロセスを指す。
    """
    census = namespace_members(namespace)
    if not census.reliable:
        return _Cleanup(DescendantCleanup.UNVERIFIABLE)
    for number in (signal.SIGTERM, signal.SIGKILL):
        if not census.members:
            break
        for pid in census.members:
            if not _signal_namespace_member(pid, namespace, number):
                return _Cleanup(DescendantCleanup.UNVERIFIABLE)
        deadline = time.monotonic() + _TERM_GRACE_SECONDS
        while time.monotonic() < deadline:
            census = namespace_members(namespace)
            if not census.reliable or not census.members:
                break
            time.sleep(0.05)
    if not census.reliable:
        return _Cleanup(DescendantCleanup.UNVERIFIABLE)
    if census.members:
        return _Cleanup(DescendantCleanup.RESIDUAL_PROCESSES, census.members)
    return _Cleanup(DescendantCleanup.CONFIRMED_EMPTY)


def _signal_namespace_member(pid: int, namespace: str, number: int) -> bool:
    """PID再利用後の別プロセスをsignalしない。先に安定したhandleを取得する。"""
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return True
    except (OSError, AttributeError):
        return False
    try:
        from harness.infrastructure.provider.process_containment import read_process_namespace

        if read_process_namespace(pid) != namespace:
            return False
        signal.pidfd_send_signal(descriptor, number)
        return True
    except ProcessLookupError:
        return True
    except (OSError, AttributeError):
        return False
    finally:
        os.close(descriptor)


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    """Group が分からない場合の最低限。親だけを止める。"""
    if process.poll() is None:
        try:
            process.send_signal(signal.SIGTERM)
        except OSError:
            pass
        try:
            process.wait(timeout=_TERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
    try:
        process.wait(timeout=_TERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover
        pass


def _signal_group(group: int, number: int) -> None:
    """Group へ signal を送る。**存在しない Group は失敗ではない。**"""
    try:
        os.killpg(group, number)
    except OSError as error:
        if error.errno not in (errno.ESRCH, errno.EPERM):
            raise


def _result(
    outcome: CliOutcome,
    exit_code: int | None,
    stdout: bytes,
    stdout_truncated: bool,
    stderr_bytes: int,
    stderr_truncated: bool,
    stderr_classifications: tuple[str, ...] = (),
    stderr_source_locations: tuple[tuple[str, int], ...] = (),
) -> CliRunResult:
    """診断 ID は観測値だけから作る。**本文も乱数も含めない。**"""
    digest = hashlib.sha256(
        "|".join(
            (
                outcome.value,
                str(exit_code),
                str(len(stdout)),
                str(stdout_truncated),
                str(stderr_bytes),
                str(stderr_truncated),
            )
        ).encode("utf-8")
    ).hexdigest()[:16]
    return CliRunResult(
        outcome=outcome,
        exit_code=exit_code,
        stdout=stdout,
        stdout_truncated=stdout_truncated,
        stderr_bytes_observed=stderr_bytes,
        stderr_truncated=stderr_truncated,
        diagnostic_id=digest,
        stderr_classifications=stderr_classifications,
        stderr_source_locations=stderr_source_locations,
    )


def _classify_stderr(payload: bytes) -> tuple[str, ...]:
    """固定語の有無だけを返す。外部の本文・Path・識別子は持ち出さない。"""
    sample = payload[:4096].lower()
    markers = {
        "FS_PERMISSION": (b"eacces", b"eperm", b"permission denied", b"operation not permitted"),
        "FS_READ_ONLY": (b"erofs", b"read-only file system"),
        "FILE_MISSING": (b"enoent", b"no such file or directory"),
        "MODULE_MISSING": (b"cannot find module", b"err_module_not_found"),
        "DNS_FAILURE": (b"enotfound", b"eai_again", b"getaddrinfo"),
        "NETWORK_UNREACHABLE": (b"enetunreach", b"econnrefused", b"econnreset"),
        "TLS_FAILURE": (b"certificate", b"cert_has_expired", b"unable_to_verify"),
        "AUTHENTICATION": (b"authenticate", b"authentication", b"unauthorized", b"invalid_grant"),
        "KEYRING": (b"keyring", b"keychain", b"secret-service"),
        "QUOTA": (b"quota", b"resource_exhausted", b"rate limit"),
        "INVALID_ARGUMENT": (b"invalid argument", b"invalid_argument"),
        "EMPTY_TOOLS": (b"function_declarations", b"functiondeclarations"),
        "MANUAL_LOGIN_REQUIRED": (b"manual authorization is required",),
        "MODEL_UNAVAILABLE": (b"model not found", b"model_not_found"),
        "SANDBOX_STARTUP": (b"sandbox_launcher:",),
    }
    return tuple(
        sorted(name for name, needles in markers.items() if any(n in sample for n in needles))
    )


def _stderr_source_locations(
    payload: bytes, sources: tuple[str, ...]
) -> tuple[tuple[str, int], ...]:
    """検証済み実行体のPathに完全一致するstack位置だけ。本文や任意Pathは返さない。"""
    sample = payload[:4096].decode("utf-8", errors="replace")
    found: set[tuple[str, int]] = set()
    for source in sources:
        name = source.rsplit("/", 1)[-1]
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.js", name):
            continue
        for line in re.findall(re.escape(source) + r":([0-9]{1,7}):[0-9]{1,7}", sample):
            found.add((name, int(line)))
    return tuple(sorted(found))[:16]
