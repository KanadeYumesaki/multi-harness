"""起動前に「必要な境界が本当に効くか」を測り、効かないなら **起動させない**。

## なぜ設定の確認では足りないか

以前は「Landlock ABI >= 1 か」だけを見て Provider を選べるようにしていた。ABI 1 の
権利集合は 13 bit で、`LANDLOCK_ACCESS_FS_TRUNCATE` を **含まない**。読めない File を
`truncate(path, 0)` で 0 bytes にできる。Codex の独立レビューが Production の
`apply_policy` へ全許可空の Policy を渡して再現している
（`docs/development/cli-workbench-boundary-review-20260908/REVIEW-EVIDENCE.json`）。

だからここでは **操作ごとに合成 canary を触らせて、拒否されたことを確かめる**。
「ABI が新しい」でも「Policy を書いた」でもなく、`EACCES` が返ったことだけを証拠に
する。`ENOENT` や `EISDIR` は証拠に数えない（触れなかった理由が別にある）。

## 子孫の封じ込めも同じ扱いにする

Process Group は `setsid()` で抜けられる。抜けた子が生きていても「片付いた」と
報告していた。ここでは実際に **`setsid` + 二重 fork で離脱する子** を境界の内側で
起こし、PID 1 が終わったあとに PID namespace が空になることを測る。

## 測れない・満たせないときは Provider を出さない

`BoundaryMeasurement.blocking_reasons` が空でなければ、`statuses()` は
`profile_verified=False` と理由を返し、`create_send_plan` は Plan を作らない。
**起動回数 0 のまま止める。** 迂回する Flag も環境変数も作らない。

## いま満たせていない要件（R1-B）

正規 CLI が自分で認証するために、その Provider の資格情報 Directory は境界の内側に
残る。モデル由来のコマンドはそれを読み得る。通信先も制限していない。これは
`UNRESOLVED_DESIGN_BLOCKERS` として **恒久的な blocking 理由** に入れてある。
Report に書いたことを承認と読み替えない。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from harness.infrastructure.provider import sandbox_launcher
from harness.infrastructure.provider.process_containment import (
    NamespaceLookupFailed,
    find_sandbox_namespace,
    namespace_members,
    read_process_namespace,
)

__all__ = [
    "UNRESOLVED_DESIGN_BLOCKERS",
    "BoundaryMeasurement",
    "BoundaryRequest",
    "LinuxBoundaryProbe",
]

#: 合成 canary の中身。**実在の Secret を使わない。**
_CANARY_TEXT: Final[str] = "SYNTHETIC-CANARY-NOT-A-REAL-SECRET\n"
_CANARY_FILE: Final[str] = "boundary-canary.txt"
_CANARY_DIR: Final[str] = "boundary-canary-dir"
_PROBE_TIMEOUT_SECONDS: Final[int] = 120
#: PID 1 が終わったあと、namespace が空になるのを待つ上限。
_SETTLE_SECONDS: Final[float] = 5.0

#: 設計として未解決で、いまの実装では満たせない要件。
#:
#: **測って足りないのではなく、方式が無い。** だから測定結果に関係なく
#: blocking 理由として積む。解消するまで Provider は起動できない。
UNRESOLVED_DESIGN_BLOCKERS: Final[tuple[str, ...]] = (
    "R1-B: 正規 CLI の認証に要る資格情報 Directory が境界の内側に残る。"
    "モデル由来のコマンドから読まれ得る経路を塞げていない",
    "R1-B: 通信先を制限していない。境界の内側から任意の宛先へ接続できる",
)

#: 境界の内側で「離脱する子」を演じる Probe。
#:
#: PID 1 が `setsid` した孫を残して先に終わる。Kernel が namespace ごと片付ければ
#: 孫は残らない。**残ったら封じ込め不成立。**
_ESCAPE_PROBE_SOURCE: Final[str] = """
import os, sys, time
try:
    namespace = os.readlink("/proc/self/ns/pid")
except OSError:
    namespace = ""
ready_read, ready_write = os.pipe()
child = os.fork()
if child == 0:
    os.close(ready_read)
    os.setsid()
    if os.fork() != 0:
        os._exit(0)
    try:
        os.write(ready_write, b"1")
    except OSError:
        pass
    os.close(ready_write)
    time.sleep(600)
    os._exit(0)
os.close(ready_write)
os.waitpid(child, 0)
started = b""
try:
    started = os.read(ready_read, 1)
except OSError:
    pass
sys.stdout.write(("detached " if started == b"1" else "not-detached ") + namespace)
sys.stdout.flush()
os._exit(0)
"""


@dataclass(frozen=True, slots=True)
class BoundaryRequest:
    """1 回の測定に要るもの。**実データを canary にしない。**"""

    launcher_argv: tuple[str, ...]
    policy: dict[str, Any]
    env: dict[str, str]
    cwd: str
    #: 破壊的な操作を試してよい合成 canary を置く Directory。境界の外側にある。
    canary_root: str
    #: 読取りだけを試す実 Path（Workspace・DB・CAS）。**書き換えない。**
    read_only_probes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BoundaryMeasurement:
    """測った結果。**「満たした」と「測れなかった」を分ける。**"""

    filesystem_denied: bool
    descendants_contained: bool
    landlock_abi: int
    truncate_handled: bool
    details: dict[str, Any] = field(default_factory=dict)
    blocking_reasons: tuple[str, ...] = ()

    @property
    def satisfied(self) -> bool:
        """必要な境界がすべて成立しているか。"""
        return not self.blocking_reasons

    def as_dict(self) -> dict[str, Any]:
        return {
            "filesystem_denied": self.filesystem_denied,
            "descendants_contained": self.descendants_contained,
            "landlock_abi": self.landlock_abi,
            "truncate_handled": self.truncate_handled,
            "satisfied": self.satisfied,
            "blocking_reasons": list(self.blocking_reasons),
            "details": self.details,
        }


class LinuxBoundaryProbe:
    """この Machine で境界が成立するかを実測する。

    測定は Kernel の能力に依存し、Process の一生の間に変わらない。同じ Policy に
    対しては 1 度だけ測り、以後は同じ結果を返す。**Cache するのは Kernel 能力で
    あって、File の同一性ではない**（そちらは毎回 Hash し直す）。
    """

    def __init__(self) -> None:
        self._cache: dict[str, BoundaryMeasurement] = {}

    def measure(self, request: BoundaryRequest) -> BoundaryMeasurement:
        key = json.dumps(
            {
                "argv": list(request.launcher_argv),
                "policy": request.policy,
                "cwd": request.cwd,
                "canary_root": request.canary_root,
                "read_only": list(request.read_only_probes),
            },
            sort_keys=True,
        )
        cached = self._cache.get(key)
        if cached is None:
            cached = self._measure(request)
            self._cache[key] = cached
        return cached

    # -- 実測 ---------------------------------------------------------------

    def _measure(self, request: BoundaryRequest) -> BoundaryMeasurement:
        reasons: list[str] = []
        filesystem = self._measure_filesystem(request)
        if not filesystem.get("all_denied"):
            reasons.append(
                "承認範囲の外にある合成 canary へ、境界の内側から操作できた: "
                + _failing_operations(filesystem)
            )
        abi = int(filesystem.get("landlock_abi") or 0)
        truncate_handled = bool(filesystem.get("truncate_handled"))
        if abi < 1:
            reasons.append("Landlock が使えない。実行前の操作範囲制限を成立させられない")
        elif not truncate_handled and not filesystem.get("readonly_mounts"):
            reasons.append(
                f"Landlock ABI {abi} は TRUNCATE 権利を扱わない（ABI 3 以降が要る）。"
                "読めない File の中身を 0 bytes にできる"
            )
        containment = self._measure_containment(request)
        contained = bool(containment.get("contained"))
        if not contained:
            reasons.append(
                "起動した Process の子孫を全数確認できない: " + str(containment.get("verdict"))
            )
        reasons.extend(UNRESOLVED_DESIGN_BLOCKERS)
        return BoundaryMeasurement(
            filesystem_denied=bool(filesystem.get("all_denied")),
            descendants_contained=contained,
            landlock_abi=abi,
            truncate_handled=truncate_handled,
            details={"filesystem": filesystem, "containment": containment},
            blocking_reasons=tuple(reasons),
        )

    def _measure_filesystem(self, request: BoundaryRequest) -> dict[str, Any]:
        """合成 canary へ操作を試させ、拒否されたことを確かめる。"""
        try:
            canaries = _prepare_canaries(Path(request.canary_root))
        except OSError as error:
            return {"all_denied": False, "error": f"canary setup failed: {error.errno}"}
        payload = json.dumps(
            {
                "canaries": [str(item) for item in canaries],
                "read_only_probes": list(request.read_only_probes),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        argv = [
            *request.launcher_argv,
            json.dumps(request.policy, separators=(",", ":"), sort_keys=True),
            "--",
            sandbox_launcher.SELF_TEST_FLAG,
            payload,
        ]
        completed = _run(argv, request)
        if completed is None:
            return {"all_denied": False, "error": "self-test did not finish in time"}
        text = completed.stdout.decode("utf-8", "replace").strip()
        try:
            report = json.loads(text)
        except ValueError:
            return {
                "all_denied": False,
                "error": "self-test produced no usable report",
                "returncode": completed.returncode,
            }
        if not isinstance(report, dict):
            return {"all_denied": False, "error": "self-test report has an unexpected shape"}
        report["returncode"] = completed.returncode
        # canary は測定のたびに作り直す。**壊れたまま残さない。**
        _clear_canaries(Path(request.canary_root))
        return report

    def _measure_containment(self, request: BoundaryRequest) -> dict[str, Any]:
        """`setsid` して離脱した子が、PID 1 の終了後に残らないことを確かめる。"""
        argv = [
            *request.launcher_argv,
            json.dumps(request.policy, separators=(",", ":"), sort_keys=True),
            "--",
            os.path.realpath(sys.executable),
            "-c",
            _ESCAPE_PROBE_SOURCE,
        ]
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed argv list, no shell, explicit env
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=request.cwd,
                env=dict(request.env),
                close_fds=True,
                start_new_session=True,
            )
        except OSError as error:
            return {"contained": False, "verdict": f"probe could not start (errno={error.errno})"}
        # namespace の識別子は 2 経路で取る。Probe 自身が境界の内側から読んだ値を
        # 第一とし、取れなければ procfs を辿る。**どちらも取れなければ測れていない。**
        try:
            walked: str | None = find_sandbox_namespace(
                process.pid, still_running=lambda: process.poll() is None
            )
        except NamespaceLookupFailed:
            walked = None
        try:
            stdout, _ = process.communicate(timeout=_PROBE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return {"contained": False, "verdict": "probe did not finish in time"}
        marker, _, reported = stdout.decode("utf-8", "replace").strip().partition(" ")
        if marker != "detached":
            # 離脱する子を起こせていない。**測れていないので「封じ込めた」と言わない。**
            return {
                "contained": False,
                "verdict": f"probe did not detach a child (stdout={marker[:40]!r})",
                "namespace": walked,
            }
        namespace = reported.strip() or walked
        if not namespace:
            return {"contained": False, "verdict": "could not identify the pid namespace"}
        deadline = time.monotonic() + _SETTLE_SECONDS
        census = namespace_members(namespace)
        while census.reliable and census.members and time.monotonic() < deadline:
            time.sleep(0.05)
            census = namespace_members(namespace)
        if not census.reliable:
            return {
                "contained": False,
                "verdict": "/proc could not be read; residual processes are unverifiable",
                "namespace": namespace,
            }
        if census.members:
            for pid in census.members:
                # 自分が起こした Probe の後始末だけを行う。
                try:
                    os.kill(pid, 9)
                except OSError:
                    continue
            return {
                "contained": False,
                "verdict": "a detached child survived the sandbox pid namespace",
                "namespace": namespace,
                "residual_pids": list(census.members),
            }
        return {
            "contained": True,
            "verdict": "the detached child died with the pid namespace",
            "namespace": namespace,
            "own_namespace": read_process_namespace(os.getpid()),
        }


def _failing_operations(report: dict[str, Any]) -> str:
    """拒否できなかった操作を並べる。**理由を人が読める形で残す。**"""
    failing: list[str] = []
    canaries = report.get("canaries")
    if isinstance(canaries, dict):
        for entry, operations in canaries.items():
            if not isinstance(operations, dict):
                continue
            for name, verdict in sorted(operations.items()):
                if name not in sandbox_launcher.PROTECTED_OPERATIONS:
                    continue
                if not str(verdict).startswith(("DENIED", "NOT_APPLICABLE")):
                    failing.append(f"{name}={verdict} ({Path(str(entry)).name})")
    if not failing and report.get("error"):
        return str(report["error"])
    return ", ".join(failing) if failing else "詳細不明"


def _prepare_canaries(root: Path) -> tuple[Path, ...]:
    """合成 canary を作り直す。**実 DB・実 CAS・Workspace は使わない。**"""
    root.mkdir(parents=True, exist_ok=True)
    canary = root / _CANARY_FILE
    canary.write_text(_CANARY_TEXT, encoding="utf-8")
    directory = root / _CANARY_DIR
    directory.mkdir(exist_ok=True)
    (directory / "inside.txt").write_text(_CANARY_TEXT, encoding="utf-8")
    return (canary, directory)


def _clear_canaries(root: Path) -> None:
    for name in (_CANARY_FILE, _CANARY_DIR):
        target = root / name
        try:
            if target.is_dir():
                for child in target.iterdir():
                    child.unlink()
                target.rmdir()
            else:
                target.unlink()
        except OSError:
            continue


def _run(argv: list[str], request: BoundaryRequest) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(  # noqa: S603 - fixed argv list, no shell, explicit env
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            cwd=request.cwd,
            env=dict(request.env),
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
            close_fds=True,
            start_new_session=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
