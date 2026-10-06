"""起動した Process の子孫を、**Process Group ではなく PID namespace で** 数える。

## なぜ Group では足りないか

`setsid()` した子は Process Group からも Session からも離れる。Group だけを見張ると
生き残った子孫を「全部片付いた」と読み違える。実際にそう報告してしまう不具合を
再現した（`docs/development/cli-workbench-boundary-fix-20260908/boundary-probe-before.json`）。

PID namespace は離脱できない。`setsid()` しても namespace の中に留まる。だから
**namespace の member を数えるのが、子孫の全数確認になる。**

## 数えられないときは「空」と言わない

namespace を特定できない、`/proc` を読めない——どちらも「残っていない」の証拠には
ならない。そういうときは `UNVERIFIABLE` を返す。呼出側は `EFFECT_UNKNOWN` へ倒す。
"""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

__all__ = [
    "NamespaceLookupFailed",
    "ProcessCensus",
    "find_sandbox_namespace",
    "namespace_members",
    "read_process_namespace",
]

#: `/proc/<pid>/task/<tid>/children` を辿るときの上限。
#:
#: Launcher は namespace を作ってから CLI を exec する。つまり namespace は CLI が
#: 動き出す **前** に出来上がっている。短い間隔で覗けば取り逃さない。
_MAX_LOOKUP_SECONDS: Final[float] = 5.0
_LOOKUP_INTERVAL: Final[float] = 0.002


class NamespaceLookupFailed(RuntimeError):
    """namespace を特定できなかった。**「空」と読み替えてはならない。**"""


@dataclass(frozen=True, slots=True)
class ProcessCensus:
    """namespace の member を数えた結果。"""

    namespace: str
    members: tuple[int, ...]
    reliable: bool

    @property
    def empty(self) -> bool:
        return self.reliable and not self.members


def read_process_namespace(pid: int, which: str = "pid") -> str | None:
    """その Process が属する namespace の識別子。読めなければ `None`。"""
    try:
        return os.readlink(f"/proc/{pid}/ns/{which}")
    except OSError:
        return None


def _children_of(pid: int) -> tuple[int, ...]:
    """`/proc/<pid>/task/<tid>/children` から直接の子を読む。"""
    collected: list[int] = []
    try:
        tasks = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return ()
    for task in tasks:
        try:
            with open(f"/proc/{pid}/task/{task}/children", encoding="ascii") as handle:
                collected.extend(int(item) for item in handle.read().split())
        except (OSError, ValueError):
            continue
    return tuple(sorted(set(collected)))


def find_sandbox_namespace(
    launcher_pid: int,
    *,
    own_namespace: str | None = None,
    still_running: Callable[[], bool] | None = None,
    budget_seconds: float = _MAX_LOOKUP_SECONDS,
) -> str:
    """Launcher が作った PID namespace の識別子を突き止める。

    Launcher は「外側 → namespace を作る子 → namespace の PID 1」という 3 段で
    起動する。外側の子（2 段目）の `pid_for_children` が、CLI が入る namespace で
    ある。見つからなければ例外で止める。**推測で埋めない。**

    `still_running` を渡すと、Launcher が終わった時点で待つのをやめる。終わって
    しまえば namespace ごと消えているので、待ち続けても見つからない。
    """
    mine = own_namespace or read_process_namespace(os.getpid())
    deadline = time.monotonic() + budget_seconds
    while True:
        for child in _children_of(launcher_pid):
            candidate = read_process_namespace(child, "pid_for_children")
            if candidate is not None and candidate != mine:
                return candidate
        if time.monotonic() >= deadline:
            break
        if still_running is not None and not still_running():
            # もう一度だけ覗いてから諦める（直前に作られていた場合を拾う）。
            for child in _children_of(launcher_pid):
                candidate = read_process_namespace(child, "pid_for_children")
                if candidate is not None and candidate != mine:
                    return candidate
            break
        time.sleep(_LOOKUP_INTERVAL)
    raise NamespaceLookupFailed("could not identify the sandbox pid namespace")


def namespace_members(namespace: str, *, exclude: frozenset[int] = frozenset()) -> ProcessCensus:
    """その namespace に属する生存 Process を数える。

    Zombie は数えない（既に動いていない）。`/proc` をひとつも読めなければ
    `reliable=False` を返す。**読めなかったことを「空」にしない。**
    """
    try:
        entries = os.listdir("/proc")
    except OSError:
        return ProcessCensus(namespace, (), reliable=False)
    members: list[int] = []
    observed_any = False
    reliable = True
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in exclude:
            continue
        identifier = read_process_namespace(pid)
        if identifier is None:
            # namespace symlinkは非dumpableな同一UIDでも読めない。
            # /procのrootにしか属さないProcessは、子namespaceの対象ではない。
            if namespace != read_process_namespace(os.getpid()) and _in_proc_root_namespace(pid):
                continue
            if _is_zombie(pid) is True:
                continue
            # 読めないだけでは他ユーザーと断定しない。消滅か所有者を別途確認する。
            try:
                owner = os.stat(f"/proc/{pid}").st_uid
            except OSError as error:
                if error.errno not in (errno.ENOENT, errno.ESRCH):
                    reliable = False
            else:
                if owner == os.getuid():
                    reliable = False
            continue
        observed_any = True
        if identifier != namespace:
            continue
        zombie = _is_zombie(pid)
        if zombie is None:
            reliable = False
        elif not zombie:
            members.append(pid)
    return ProcessCensus(namespace, tuple(sorted(members)), reliable=reliable and observed_any)


def _is_zombie(pid: int) -> bool | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as error:
        return True if error.errno in (errno.ENOENT, errno.ESRCH) else None
    marker = text.rfind(")")
    if marker < 0:
        return None
    fields = text[marker + 2 :].split()
    return fields[0] == "Z" if fields else None


def _in_proc_root_namespace(pid: int) -> bool:
    """NSpidが一要素なら、このprocfsのrootより内側のPID namespaceには属さない。"""
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("NSpid:"):
                    values = line.split()[1:]
                    return values == [str(pid)]
    except OSError:
        return False
    return False
