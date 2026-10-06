"""偽CLIが記録した自分の子だけをpidfdで停止する試験専用のfinally。

Process名で検索しない。PID/UID/開始時刻が一致する生存Processのpidfdだけへsignalを
送る。pidfdはPID再利用で別Processへ移らない。所有を確認できない場合は試験を失敗
させ、確認していないProcessを止めない。ProductionのCleanup結果は書き換えない。
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import signal
from collections.abc import Iterator
from pathlib import Path


class CleanupUnconfirmed(RuntimeError):
    """所有または停止を確認できない。"""


def start_ticks(pid: int) -> int:
    text = Path(f"/proc/{pid}/stat").read_text()
    return int(text[text.rfind(")") + 2 :].split()[19])


def cleanup_record(record_path: Path) -> None:
    try:
        record = json.loads(record_path.read_text())
        if not isinstance(record, dict) or set(record) != {"pid", "uid", "start_ticks"}:
            raise CleanupUnconfirmed("CHILD_IDENTITY_INVALID")
        if any(type(record[k]) is not int for k in record):
            raise CleanupUnconfirmed("CHILD_IDENTITY_INVALID")
        pid = record["pid"]
        if pid <= 0 or record["uid"] != os.getuid():
            raise CleanupUnconfirmed("CHILD_OWNER_MISMATCH")
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            return  # KernelがこのPIDの消滅を確認した。signalを送らない。
        try:
            proc = Path(f"/proc/{pid}")
            try:
                actual_uid = proc.stat().st_uid
                ticks = start_ticks(pid)
            except FileNotFoundError:
                poll = select.poll()
                poll.register(fd, select.POLLIN)
                if poll.poll(0):
                    return
                raise CleanupUnconfirmed("CHILD_IDENTITY_UNREADABLE") from None
            if actual_uid != record["uid"] or ticks != record["start_ticks"]:
                raise CleanupUnconfirmed("CHILD_IDENTITY_MISMATCH")
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except ProcessLookupError:
                return
            poll = select.poll()
            poll.register(fd, select.POLLIN)
            if not poll.poll(5000):
                raise CleanupUnconfirmed("CHILD_STOP_UNCONFIRMED")
        finally:
            os.close(fd)
    except (OSError, ValueError, KeyError, IndexError, json.JSONDecodeError) as exc:
        raise CleanupUnconfirmed("CHILD_CLEANUP_UNCONFIRMED") from exc


@contextlib.contextmanager
def own_leaked_child(record_path: Path) -> Iterator[None]:
    try:
        yield
    finally:
        cleanup_record(record_path)
