"""Mount越境の拒否（`AT-INPUT-PATH-001/MOUNT_CROSSING`）。

## なぜ今まで書かれていなかったか

「bind mount の作成には root 権限が要る」として未実装に分類していた。
**これは誤りだった。** 非特権 user namespace（`unshare -Urm`）の中なら、
一般Userのままtmpfsもbind mountも作れる。実測して分かった。

その誤った前提のせいで、Mount越境の拒否経路は3つとも一度も実行されて
いなかった。実装はあるが、動くかどうかは誰も確かめていなかった。

## bind mount が要点である

tmpfs を被せた場合は `st_dev` が変わるので、素朴な Device 比較でも捕まる。
だが**同一Filesystem上のbind mountは `st_dev` が変わらない**。
それでいて、Workspace外のFileがWorkspace内のPathで見えるようになる。

    workspace/docs/mounted/secret.txt  ->  実体は workspace の外

`st_dev` だけを見る実装はこれを通す。効いているのは
`RESOLVE_NO_XDEV`（openat2）と `mnt_id` 比較（Fallback）の側である。
両方の経路で拒否されることを、実際に mount を作って確かめる。

本試験が実際に効くことは、Guardを外した複製で確認した。

* `mnt_id` 比較を外す  → Fallback側が落ちる
* `RESOLVE_NO_XDEV` も外す → openat2側も落ちる
* このとき `st_dev` 比較は**残したまま**である。それでも読めてしまう。
  Device比較が不十分であることの実証にあたる。

## 名前空間の中で走らせる

mountは子Process側の名前空間にだけ効くので、実行環境のMount表は汚れない。
子は `mount_crossing_driver.py`。結果はJSONで受け取り、**表明は親で行う**。
子で `assert` すると失敗理由が終了Codeへ潰れる。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "support"))

from types import SimpleNamespace  # noqa: E402

from case_probe import observe_case  # noqa: E402

from input_read_case_evidence import validate_input_read  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
DRIVER = Path(__file__).parent / "mount_crossing_driver.py"


def _run(argv: list[str], **keywords: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - 固定argv、shell不使用（不変条件#8）
        argv, capture_output=True, text=True, timeout=120, check=False, **keywords
    )


def _mount_namespace_prefix() -> list[str]:
    """Mount名前空間を作れる呼び方を1つ選ぶ。

    ## なぜ2通り要るのか

    * **開発機（WSL2）**: 非特権 user namespace が使える。sudoはPassword を
      求めるので使えない。
    * **CI（GitHub Runner）**: `unshare --map-root-user` が
      `/proc/self/uid_map: Operation not permitted` で失敗する。
      代わりにPassword無しsudoが使える。

    どちらか一方に決め打つと、もう一方で試験が動かない。動かない試験を
    skipで逃がすと、Mount越境の拒否が**未検証のまま緑になる**（不変条件#16）。
    実際に使える方を実行時に選び、どちらも無ければ失敗させる。
    """
    unshare = shutil.which("unshare")
    if unshare is None:
        return []

    # 1. 非特権 user namespace
    probe = _run([unshare, "--user", "--map-root-user", "--mount", "true"])
    if probe.returncode == 0:
        return [unshare, "--user", "--map-root-user", "--mount", "--propagation", "private"]

    # 2. 実rootでMount名前空間だけを切る
    sudo = shutil.which("sudo")
    if sudo is not None and _run([sudo, "-n", "true"]).returncode == 0:
        return [sudo, "-n", unshare, "--mount", "--propagation", "private"]

    return []


_PREFIX = _mount_namespace_prefix()


def _run_in_namespace(base: Path, kind: str, path_mode: str) -> dict[str, object]:
    assert _PREFIX, (
        "Mount名前空間を作れない。非特権 user namespace もPassword無しsudoも"
        "使えないため、Mount越境の拒否を実測できない。\n"
        "この試験を飛ばすと、Workspace外のFileがWorkspace内のPathで読める"
        "かどうかが未検証のまま残る（不変条件#16）。"
    )

    # 環境変数を渡さない。`sudo` は落とすので、渡せる前提で書くと
    # 起動方法によって動いたり動かなかったりする。Driver側が
    # Repository Rootから import Pathを組み立てる。
    result = _run(
        [*_PREFIX, sys.executable, str(DRIVER), str(REPO_ROOT), str(base), kind, path_mode]
    )
    assert result.returncode == 0, (
        f"名前空間内の実行が失敗した（起動: {' '.join(_PREFIX[:3])}）。\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


class _TransportedLedgerView:
    """Namespace の中で正本Ledgerから読んだ観測を、外へ運ぶための View。

    値を作らない。`mount_crossing_driver.py` が `load_stream` と `stream_head`
    から読んだ結果をそのまま持つだけである。Namespace の外からは同じ Ledger を
    開けないため、観測は中で行い JSON で持ち帰る。
    """

    def __init__(self, report: dict[str, object]) -> None:
        self._events = [str(x) for x in report["observed_event_sequence"]]  # type: ignore[union-attr]
        self._head = int(report["ledger_head_after"])  # type: ignore[arg-type]
        if self._head != len(self._events):
            raise AssertionError(
                f"driver が持ち帰った観測が不整合である: head={self._head} "
                f"events={len(self._events)}"
            )

    @property
    def appended(self) -> list[str]:
        return list(self._events)

    @property
    def head(self) -> int:
        return self._head

    @property
    def effect_attempts(self) -> int:
        started = {"ACTION_PREPARED", "ACTION_EXECUTING", "EXECUTION_ATTEMPTED", "EFFECT_ATTEMPTED"}
        return sum(1 for name in self._events if name in started)


@pytest.mark.case("AT-INPUT-PATH-001/MOUNT_CROSSING")
def test_mount_crossing_is_denied_and_recorded(tmp_path: Path, case_observation: Any) -> None:
    """Orchestrator 経由で拒否し、正本 Ledger へ記録されることを確かめる。

    Mount Namespace の中でしか状況を作れないため、Orchestrator の実行も観測も
    driver の中で行い、結果を JSON で持ち帰る。**Test 側では Event を作らない。**
    """
    report = _run_in_namespace(tmp_path, "bind", "openat2")

    assert report["same_device"] is True, "bind mount で st_dev が変わっている"
    assert report["crossing_denied"] is True

    # The child captured a real typed result inside the mount namespace.
    # Validate that transported metadata before attaching it to this Case.
    validate_input_read(report["typed_result"])
    case_observation.typed_result = report["typed_result"]
    effects = SimpleNamespace(**report["typed_result"]["effects"])
    view = _TransportedLedgerView(report)
    observe_case(
        case_observation,
        "AT-INPUT-PATH-001/MOUNT_CROSSING",
        state=str(report["orchestrated_state"]),
        subject_id=str(report["orchestrated_subject_id"]),
        error_code=(
            str(report["orchestrated_error_code"])
            if report["orchestrated_error_code"] is not None
            else None
        ),
        ledger=view,
        effects=effects,
        head_before=int(report["ledger_head_before"]),
        payload={
            "kind": report["kind"],
            "path_mode": report["path"],
            "same_device": report["same_device"],
            "capability_path_hash": report["capability_path_hash"],
            "producer_module": "harness.application.input_read_orchestrator",
            "producer_symbol": "InputReadOrchestrator.read",
        },
    )


@pytest.mark.parametrize("path_mode", ["openat2", "fallback"])
def test_reading_across_a_bind_mount_is_denied(tmp_path: Path, path_mode: str) -> None:
    """**同一Device上のbind越しにWorkspace外を読ませない。**

    `st_dev` は一致するので、Device比較だけの実装は素通りさせる。
    openat2 経路（`RESOLVE_NO_XDEV`）と Fallback 経路（`mnt_id` 比較）の
    両方で拒否されること。片方だけ厳しくても、緩い側が抜け穴になる。
    """
    report = _run_in_namespace(tmp_path, "bind", path_mode)

    assert report["same_device"] is True, (
        "bind mount で st_dev が変わってしまい、試験が意図した状況になっていない"
    )
    assert report["crossing_denied"] is True, (
        f"Workspace外の内容が読めている: {report['crossing_leaked_bytes']!r}"
    )
    assert report["crossing_error"] == report["expected_error"]


@pytest.mark.parametrize("path_mode", ["openat2", "fallback"])
def test_reading_across_a_foreign_filesystem_is_denied(tmp_path: Path, path_mode: str) -> None:
    """別Filesystem（tmpfs）を被せた場合。`st_dev` も `mnt_id` も変わる。"""
    report = _run_in_namespace(tmp_path, "tmpfs", path_mode)

    assert report["same_device"] is False, "tmpfs なのに st_dev が同じ。前提が崩れている"
    assert report["crossing_denied"] is True
    assert report["crossing_error"] == report["expected_error"]


@pytest.mark.parametrize("path_mode", ["openat2", "fallback"])
def test_normal_read_still_works_inside_the_namespace(tmp_path: Path, path_mode: str) -> None:
    """越境拒否が、同じWorkspace内の普通の読取りまで巻き込んでいないこと。

    これが無いと「全部拒否する実装」でも上の2件が通ってしまう。
    """
    report = _run_in_namespace(tmp_path, "bind", path_mode)
    assert report["normal_allowed"] is True, (
        f"Workspace内の通常Fileが読めない: {report['normal_error']}"
    )
