"""Mount越境試験の子Process側（`AT-INPUT-PATH-001/MOUNT_CROSSING`）。

非特権 user namespace（`unshare -Urm`）の中で走る。親から分離された
Mount名前空間でだけmountするので、実行環境のMount表を汚さない。

親へはstdoutの最終行にJSONで結果を返す。**Pytestの表明は親側で行う。**
子で `assert` すると、失敗理由が終了Codeへ潰れて読めなくなる。

`pytest` が集めないよう `test_` で始まらない名前にしてある。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# `sudo` は環境変数を落とすので `PYTHONPATH` に頼れない。呼出側から
# 受け取ったRepository Rootから直接 import Pathを組み立てる。
# 起動方法（user namespace / sudo）で挙動が変わらないようにするため、
# 環境に依存しない側へ寄せる。
sys.path.insert(0, str(Path(sys.argv[1]) / "src"))
sys.path.insert(0, str(Path(sys.argv[1]) / "tools"))
sys.path.insert(0, str(Path(sys.argv[1]) / "tests/support"))

import pytest
from input_read_observation import InputReadEffectProbe, capture_input_read

from harness.application.input_read_orchestrator import (
    InputReadOrchestrator,
    InputReadRequest,
)
from harness.domain.errors import ErrorCode
from harness.domain.input_read import CapabilityScope, ReadDenial
from harness.infrastructure.filesystem.safe_reader import (
    CapabilityBroker,
    SafeInputReader,
)
from harness.infrastructure.filesystem.workspace_boundary import FilesystemPolicy
from harness.infrastructure.sqlite.connection_factory import ConnectionFactory, ConnectionRole
from harness.infrastructure.sqlite.event_ledger_repository import SqliteEventLedgerRepository
from harness.infrastructure.sqlite.migrations import migrate
from harness.infrastructure.sqlite.unit_of_work import SqliteUnitOfWork

STREAM_ID = "INPUT_READ_STREAM"


class _FrozenClock:
    def now(self) -> str:
        return "2026-08-20T00:00:00Z"


CAPABILITY_ID = "cap-mount"
OUTSIDE_MARKER = b"content from outside the workspace\n"


def _mount(*arguments: str) -> None:
    executable = shutil.which("mount")
    if executable is None:
        raise RuntimeError("mount(8) が見つからない")
    result = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
        [executable, *arguments], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"mount {arguments} failed: {result.stderr.strip()}")


def main() -> int:
    repo_root = Path(sys.argv[1])
    base = Path(sys.argv[2])
    kind = sys.argv[3]  # "tmpfs" | "bind"
    force_fallback = sys.argv[4] == "fallback"

    workspace = base / "workspace"
    docs = workspace / "docs"
    mounted = docs / "mounted"
    outside = base / "outside"
    mounted.mkdir(parents=True)
    outside.mkdir(parents=True)
    (outside / "secret.txt").write_bytes(OUTSIDE_MARKER)
    (docs / "normal.txt").write_bytes(b"inside\n")

    if kind == "tmpfs":
        # 別Filesystem。st_dev も mnt_id も変わる。
        _mount("-t", "tmpfs", "tmpfs", str(mounted))
        (mounted / "secret.txt").write_bytes(OUTSIDE_MARKER)
    else:
        # 同一Filesystem上のbind。**st_dev は変わらない。**
        # Workspace外の中身がWorkspace内のPathで見えるようになる。
        _mount("--bind", str(outside), str(mounted))

    if force_fallback:
        # ADR-003 の縮退経路。openat2 が無い環境で判定が緩まないこと。
        import harness.infrastructure.filesystem.safe_reader as module

        module.is_available = lambda: False  # type: ignore[assignment]

    same_device = os.stat(mounted).st_dev == os.stat(workspace).st_dev

    # Namespace の中で Orchestrator を動かし、**正本 Ledger へ実際に Append する**。
    # Namespace の外から観測できないので、観測もここで行い JSON で持ち帰る。
    factory = ConnectionFactory(base / "harness.db")
    migrate(factory, recorded_at="2026-08-20T00:00:00Z")
    made = factory.connect(ConnectionRole.RUNTIME)
    ledger = SqliteEventLedgerRepository(made)

    with CapabilityBroker(FilesystemPolicy.load(repo_root), allow_test_filesystems=True) as broker:
        broker.issue(CAPABILITY_ID, workspace, CapabilityScope(("docs",)))
        reader = SafeInputReader(broker)

        orchestrator = InputReadOrchestrator(
            reader=reader,
            ledger=ledger,
            unit_of_work=SqliteUnitOfWork(factory, made),
            clock=_FrozenClock(),
        )
        head_before = ledger.stream_head(STREAM_ID)
        request = InputReadRequest(
            stream_id=STREAM_ID,
            read_decision_id="read-mount-crossing",
            capability_id=CAPABILITY_ID,
            relative_path="docs/mounted/secret.txt",
        )
        effects = InputReadEffectProbe()
        with pytest.MonkeyPatch.context() as patch:
            effects.install(patch)
            outcome = orchestrator.read(request)
        typed_result = capture_input_read(
            outcome, request, ledger, effects, head_before=head_before
        )
        # Ledger を読み戻す。Orchestrator の申告をそのまま観測値にしない。
        observed = [entry.event_type for entry in ledger.load_stream(STREAM_ID)]
        head_after = ledger.stream_head(STREAM_ID)

        crossing = reader.open_read(CAPABILITY_ID, "docs/mounted/secret.txt")
        normal = reader.open_read(CAPABILITY_ID, "docs/normal.txt")
    made.close()

    payload = {
        "typed_result": typed_result,
        "kind": kind,
        "path": "openat2" if not force_fallback else "fallback",
        "same_device": same_device,
        "crossing_denied": isinstance(crossing, ReadDenial),
        "crossing_error": (crossing.error_code.value if isinstance(crossing, ReadDenial) else None),
        "crossing_leaked_bytes": (
            None if isinstance(crossing, ReadDenial) else crossing[0].decode("utf-8", "replace")
        ),
        "normal_allowed": not isinstance(normal, ReadDenial),
        "normal_error": (normal.error_code.value if isinstance(normal, ReadDenial) else None),
        "expected_error": ErrorCode.MOUNT_CROSSING_DENIED.value,
        # --- Orchestrator の観測（正本Ledgerから読み戻した値）---
        "orchestrated_state": outcome.state,
        "orchestrated_error_code": (
            outcome.error_code.value if outcome.error_code is not None else None
        ),
        "orchestrated_subject_id": outcome.read_decision_id,
        "observed_event_sequence": observed,
        "ledger_head_before": head_before,
        "ledger_head_after": head_after,
        "capability_path_hash": str(outcome.capability_path_hash),
    }
    print(json.dumps(payload))
    _restore_ownership(base)
    return 0


def _restore_ownership(base: Path) -> None:
    """作ったFileの所有者を、呼出側の所有者へ戻す。

    実rootで走った場合（CI経路）、ここで作ったFileはrootのものになる。
    pytestの一時Directory掃除は呼出側のUserで動くので、戻さないと
    **別のSessionの掃除が失敗する**。失敗するのは今日ではなく、
    一時Directoryが3世代たまったあとになる。原因を辿りにくい壊し方なので、
    作った側で片付ける。
    """
    if os.geteuid() != 0:
        return
    owner = os.stat(base)
    for path in (base, *base.rglob("*")):
        try:
            os.chown(path, owner.st_uid, owner.st_gid, follow_symlinks=False)
        except OSError:
            # Mount配下など触れないものは残る。Mountは名前空間ごと消える。
            continue


if __name__ == "__main__":
    raise SystemExit(main())
