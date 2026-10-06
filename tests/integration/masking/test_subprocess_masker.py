"""Subprocess Masker のTimeoutとProcess停止（ADR-007 §7、`masker_timeout_seconds`）。

## なぜProcess境界で止めるのか

Pythonは実行中の関数を安全に中断できない。`signal.alarm` は主スレッドの
Python bytecode境界でしか効かず、C拡張やI/O待ちの最中には割り込めない。
`threading` で待っても、走り続けるスレッドを殺す手段が無い。

Maskerを**子Process**として起動すれば、Timeoutは「待つのをやめて殺す」
という確実な操作になる。これがADR-007 §7がMaskerをProcess分離する理由の
一つでもある（隔離要件は同じ境界で課される）。

## 何を固定するか

1. Timeoutを超えたら`MaskerUnavailableError`（→ `MASKER_UNAVAILABLE`）
2. **子Processが確実に死ぬ**。放置すると次の実行がリソース不足で失敗する
3. **孫Processも道連れにする**。直接の子だけを殺すと、Raw PIIを持った孫が
   親を失って走り続ける
4. 停止の巻き添えが呼出し側へ及ばない。Process Group停止は取り違えると
   Harness自身を撃つ
5. 呼出しが**有限時間で返る**。SIGALRMで上限を付けて回帰を止める
6. FDを漏らさない。Timeoutのたびに漏れると無関係な処理が `EMFILE` で落ちる
7. 本文をargvへ載せない。`/proc/<pid>/cmdline` は他Userから読める
8. Shellを経由しない（不変条件#8）
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harness.domain.masking import Span
from harness.infrastructure.masking.subprocess_masker import (
    SubprocessMasker,
    _process_group_of,
)
from harness.ports.masker import MaskerRequest, MaskerUnavailableError

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
SECRET_TEXT = "担当は山田太郎さん Zq7Xw2Nv9Kb4Ld6P"


def request_for(text: str = SECRET_TEXT) -> MaskerRequest:
    return MaskerRequest(
        text=text,
        source_normalized_hash="sha256:" + "a" * 64,
        normalization_profile="NFC_CODEPOINT_V3",
        normalization_profile_artifact_hash="sha256:" + "b" * 64,
        allowed_categories=("PERSON_NAME",),
        candidate_spans=(Span(0, 2, "PERSON_NAME"),),
    )


def python_child(body: str) -> tuple[str, ...]:
    """子Processとして走らせるPythonコード。argvにはコードだけが載る。"""
    return (sys.executable, "-c", body)


ECHO_EMPTY_SPANS = """
import json, sys
request = json.load(sys.stdin)
print(json.dumps({
    "source_normalized_hash": request["source_normalized_hash"],
    "normalization_profile": request["normalization_profile"],
    "normalization_profile_artifact_hash": request["normalization_profile_artifact_hash"],
    "spans": [],
}))
"""

SLEEP_FOREVER = "import time\nwhile True:\n    time.sleep(1)\n"

# SIGTERMを黙って無視して眠り続ける。猶予切れでSIGKILLへ上がるかを見る。
IGNORE_SIGTERM = """
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(1)
"""

# 孫Processを作って自分は先に終わる。孫はstdoutを掴んだまま生き残ろうとする。
# 孫のPIDを親のstderrではなくFileへ書くのは、Pipeを閉じないまま知らせるため。
SPAWN_ORPHAN = """
import os, subprocess, sys, time
pid_file = sys.argv[1]
child = subprocess.Popen(
    [sys.executable, "-c", "import time\\nwhile True: time.sleep(1)"]
)
open(pid_file, "w").write(str(child.pid))
while True:
    time.sleep(1)
"""

# 子Process側で見えるRLIMIT_COREを報告する。親からは確かめようがない。
REPORT_CORE_LIMIT = """
import json, resource, sys
sys.stdin.read()
print(json.dumps({"rlimit_core": resource.getrlimit(resource.RLIMIT_CORE)}))
"""

EXIT_NONZERO = "import sys\nsys.exit(3)\n"

GARBAGE_OUTPUT = "print('not json at all')\n"


# ---------------------------------------------------------------------------
# Timeout と Process停止
# ---------------------------------------------------------------------------


def test_timeout_raises_masker_unavailable(tmp_path: Path) -> None:
    """応答しない子ProcessはTimeoutで打ち切る。"""
    masker = SubprocessMasker(
        command=python_child(SLEEP_FOREVER),
        timeout_seconds=1,
        descriptor=SubprocessMasker.mock_descriptor("sleeper"),
    )
    started = time.monotonic()
    with pytest.raises(MaskerUnavailableError, match="timed out"):
        masker.propose_spans(request_for())
    elapsed = time.monotonic() - started
    assert elapsed < 10, f"Timeout後の復帰に{elapsed:.1f}秒かかっている"


def test_timed_out_child_is_actually_dead(tmp_path: Path) -> None:
    """**子Processを確実に殺す。**

    放置すると、Timeoutのたびに走り続けるProcessが増える。Maskerは
    Raw PIIを持っているため、生き残ること自体が危険でもある。
    """
    masker = SubprocessMasker(
        command=python_child(SLEEP_FOREVER),
        timeout_seconds=1,
        descriptor=SubprocessMasker.mock_descriptor("sleeper"),
    )
    with pytest.raises(MaskerUnavailableError):
        masker.propose_spans(request_for())

    pid = masker.last_pid
    assert pid is not None
    # 既にreapされているので、同じPIDのProcessは存在しない。
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_child_ignoring_sigterm_is_killed(tmp_path: Path) -> None:
    """SIGTERMを無視する子はSIGKILLへ上げる。

    「行儀よく終了してくれ」は要請であって強制ではない。猶予を与えるのは
    後片付けのためであり、猶予が切れたあとも待ち続ける理由は無い。
    """
    masker = SubprocessMasker(
        command=python_child(IGNORE_SIGTERM),
        timeout_seconds=1,
        descriptor=SubprocessMasker.mock_descriptor("stubborn"),
    )
    started = time.monotonic()
    with pytest.raises(MaskerUnavailableError, match="timed out"):
        masker.propose_spans(request_for())
    elapsed = time.monotonic() - started

    pid = masker.last_pid
    assert pid is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    # Timeout 1秒 + 猶予2秒。10秒も掛かるなら猶予が効いていない。
    assert elapsed < 10, f"停止に{elapsed:.1f}秒かかっている"


def test_grandchild_does_not_survive_the_timeout(tmp_path: Path) -> None:
    """**孫Processまで殺す。**

    Maskerが自分で子を起こしていた場合、直接の子だけを殺しても
    孫は生き残る。孫はMaskerの記憶空間を継いでおり、**Raw PIIを持ったまま
    親を失って走り続ける**。Harness側からは見えないので、次に気付くのは
    誰かが `ps` を見たときになる。

    直接の子を殺した時点で「Timeoutを処理した」と数えてよいなら、
    その数え方が間違っている。Process Groupごと落とす。
    """
    pid_file = tmp_path / "grandchild.pid"
    masker = SubprocessMasker(
        command=(*python_child(SPAWN_ORPHAN), str(pid_file)),
        timeout_seconds=2,
        descriptor=SubprocessMasker.mock_descriptor("spawner"),
    )
    with pytest.raises(MaskerUnavailableError, match="timed out"):
        masker.propose_spans(request_for())

    assert pid_file.is_file(), "孫が起動していない。試験が前提を満たしていない"
    grandchild = int(pid_file.read_text(encoding="utf-8"))

    # 直接の子は死んでいる
    assert masker.last_pid is not None
    with pytest.raises(ProcessLookupError):
        os.kill(masker.last_pid, 0)

    # 孫も死んでいなければならない。SIGKILL直後は反映に猶予が要る。
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)

    os.kill(grandchild, signal.SIGKILL)  # 後始末。試験環境を汚さない
    pytest.fail(f"孫Process {grandchild} がTimeout後も生きている")


def test_timeout_does_not_leak_pipe_descriptors(tmp_path: Path) -> None:
    """Timeoutを繰り返してもFDが増えない。

    `communicate()` はTimeoutで例外を投げるとき、stdin/stdout/stderr の
    Pipeを閉じない。閉じ忘れるとTimeoutのたびに3本ずつ漏れ、
    やがて `EMFILE` で**Timeoutと無関係な処理**が失敗しはじめる。
    そのとき原因はここには見えない。
    """
    masker = SubprocessMasker(
        command=python_child(SLEEP_FOREVER),
        timeout_seconds=1,
        descriptor=SubprocessMasker.mock_descriptor("sleeper"),
    )

    def open_descriptors() -> int:
        return len(os.listdir("/proc/self/fd"))

    with pytest.raises(MaskerUnavailableError):
        masker.propose_spans(request_for())
    baseline = open_descriptors()

    for _ in range(3):
        with pytest.raises(MaskerUnavailableError):
            masker.propose_spans(request_for())

    assert open_descriptors() <= baseline, (
        f"Timeout 3回でFDが {baseline} から {open_descriptors()} へ増えている"
    )


def test_group_is_not_used_when_the_child_is_not_its_own_leader() -> None:
    """**自分のProcess Groupを撃たないための番人。**

    Group停止は、Group IDを取り違えると `killpg` が呼出し側のGroupを撃つ。
    Harness自身とその親（試験Runner・Shell）を巻き込むため、間違えたときの
    被害が他の誤りと桁違いになる。「`start_new_session=True` だからGroup先頭の
    はず」という前提に頼らず、`pgid == pid` を実際に確かめてから使う。

    ここでは前提が崩れた状況を作る。新Sessionを切らずに起動した子は
    呼出し側と同じGroupに属するため、判定はNoneでなければならない。
    """
    process = subprocess.Popen(  # noqa: S603 - 固定argv、shell不使用
        python_child(SLEEP_FOREVER),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # start_new_session を**あえて付けない**
    )
    try:
        assert os.getpgid(process.pid) == os.getpgid(0), "試験の前提が成立していない"
        assert _process_group_of(process) is None, "呼出し側と同じGroupを撃つ対象として返している"
    finally:
        process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def test_call_returns_within_a_bounded_time(tmp_path: Path) -> None:
    """呼出しが無限に止まらないことをSIGALRMで固定する。

    §1.16.2のFIFOと同じ形の回帰試験である。あちらは`open`が返らなかった。
    ここでTimeoutが効かなくなれば、Harness全体が停止する。
    """
    masker = SubprocessMasker(
        command=python_child(SLEEP_FOREVER),
        timeout_seconds=1,
        descriptor=SubprocessMasker.mock_descriptor("sleeper"),
    )
    timed_out: list[bool] = []

    def _alarm(_signum: int, _frame: object) -> None:
        timed_out.append(True)
        raise TimeoutError("propose_spans did not return")

    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(15)
    try:
        with pytest.raises(MaskerUnavailableError):
            masker.propose_spans(request_for())
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)

    assert not timed_out


# ---------------------------------------------------------------------------
# 異常終了
# ---------------------------------------------------------------------------


def test_core_dumps_are_actually_disabled_in_the_child() -> None:
    """**子Process側で `RLIMIT_CORE` が 0 になっていること。**

    `verify_isolation()` が True で申告する唯一の隔離要件がこれである。
    残り（Network遮断・Namespace・Memory Lock）は False のまま正直に
    申告している。つまり**この1件だけが「守っている」と言っている**。

    `_drop_core_dumps` は `preexec_fn` として fork 後の子で走るので、
    親から見ると実行の有無を確認できない。カバレッジにも現れない。
    子に自分で報告させる以外に確かめる方法が無い。

    Core DumpにはRaw PIIが丸ごと載る。ここが効いていなければ、
    Maskerが落ちるたびにDisk上へ原文が書き出される。
    """
    masker = SubprocessMasker(
        command=python_child(REPORT_CORE_LIMIT),
        timeout_seconds=10,
        descriptor=SubprocessMasker.mock_descriptor("core-reporter"),
    )
    report = json.loads(masker.propose_spans(request_for()))
    soft, hard = report["rlimit_core"]
    assert (soft, hard) == (0, 0), f"子のRLIMIT_COREが {soft},{hard} のままである"


def test_nonzero_exit_is_unavailable() -> None:
    masker = SubprocessMasker(
        command=python_child(EXIT_NONZERO),
        timeout_seconds=5,
        descriptor=SubprocessMasker.mock_descriptor("failing"),
    )
    with pytest.raises(MaskerUnavailableError, match="exit"):
        masker.propose_spans(request_for())


def test_missing_executable_is_unavailable() -> None:
    masker = SubprocessMasker(
        command=("/nonexistent/masker-binary",),
        timeout_seconds=5,
        descriptor=SubprocessMasker.mock_descriptor("missing"),
    )
    with pytest.raises(MaskerUnavailableError):
        masker.propose_spans(request_for())


def test_garbage_output_is_returned_verbatim() -> None:
    """出力の妥当性はAdapterの責務ではない。

    `parse_masker_output()` が `MASKER_OUTPUT_MALFORMED` として扱う。
    Adapterがここで判断すると、検証が2箇所に散る。
    """
    masker = SubprocessMasker(
        command=python_child(GARBAGE_OUTPUT),
        timeout_seconds=5,
        descriptor=SubprocessMasker.mock_descriptor("garbage"),
    )
    assert masker.propose_spans(request_for()).strip() == "not json at all"


# ---------------------------------------------------------------------------
# 正常系
# ---------------------------------------------------------------------------


def test_successful_call_returns_stdout() -> None:
    masker = SubprocessMasker(
        command=python_child(ECHO_EMPTY_SPANS),
        timeout_seconds=5,
        descriptor=SubprocessMasker.mock_descriptor("echo"),
    )
    import json

    document = json.loads(masker.propose_spans(request_for()))
    assert document["source_normalized_hash"] == "sha256:" + "a" * 64
    assert document["normalization_profile"] == "NFC_CODEPOINT_V3"
    assert document["spans"] == []


# ---------------------------------------------------------------------------
# 本文の渡し方
# ---------------------------------------------------------------------------


def test_text_is_never_placed_on_the_command_line() -> None:
    """**本文をargvへ載せない。**

    `/proc/<pid>/cmdline` は同一ホストの他Userから読める。Raw PIIを
    そこへ置くと、Masker隔離（ADR-007 §7）を何重に固めても意味が無い。
    本文はstdin経由で渡す。
    """
    masker = SubprocessMasker(
        command=python_child(ECHO_EMPTY_SPANS),
        timeout_seconds=5,
        descriptor=SubprocessMasker.mock_descriptor("echo"),
    )
    masker.propose_spans(request_for())
    rendered = " ".join(masker.command)
    assert SECRET_TEXT not in rendered
    assert "山田太郎" not in rendered
    assert "Zq7Xw2Nv9Kb4Ld6P" not in rendered


def test_process_is_launched_without_a_shell() -> None:
    """不変条件#8「Process起動は`list[str]`だけ。`shell=True`を禁止する」。"""
    masker = SubprocessMasker(
        command=python_child(ECHO_EMPTY_SPANS),
        timeout_seconds=5,
        descriptor=SubprocessMasker.mock_descriptor("echo"),
    )
    assert isinstance(masker.command, tuple)
    assert all(isinstance(part, str) for part in masker.command)

    source = (
        REPO_ROOT / "src" / "harness" / "infrastructure" / "masking" / "subprocess_masker.py"
    ).read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "os.system" not in source


# ---------------------------------------------------------------------------
# 隔離の申告
# ---------------------------------------------------------------------------


def test_isolation_report_states_only_what_is_actually_enforced() -> None:
    """申告は実際に課した制約と一致させる。

    Pythonから確実に課せるのはRLIMIT_CORE=0程度である。Network Egress遮断や
    mlockallはNamespaceや専用Launcherが要る。**課していないものをtrueで
    申告すると、Fail-Closedの判定が嘘の申告で通ってしまう。**

    結果としてこのAdapterは現行Policyの隔離要件を満たさず、Pipelineは
    `MASKER_ISOLATION_INCOMPLETE` で拒否する。それが正しい状態である。
    """
    masker = SubprocessMasker(
        command=python_child(ECHO_EMPTY_SPANS),
        timeout_seconds=5,
        descriptor=SubprocessMasker.mock_descriptor("echo"),
    )
    report = masker.verify_isolation()
    assert report.core_dump_disabled is True
    assert report.network_egress_denied is False
    assert report.memory_locked is False


def test_timeout_comes_from_the_registry(tmp_path: Path) -> None:
    """Timeout値を実装へ直書きしない（不変条件#18）。"""
    from harness.infrastructure.masking.policy import MaskingPolicy

    policy = MaskingPolicy.load(REPO_ROOT)
    masker = SubprocessMasker.from_policy(
        command=python_child(ECHO_EMPTY_SPANS),
        policy=policy,
        descriptor=SubprocessMasker.mock_descriptor("echo"),
    )
    assert masker.timeout_seconds == policy.masker_timeout_seconds


def test_core_dump_limit_is_applied_to_the_child() -> None:
    """RLIMIT_CORE=0 を子Processへ実際に課している。

    Core DumpにはRaw PIIが丸ごと載る。ADR-007 §7 が禁じる理由である。
    """
    body = "import resource\nprint(resource.getrlimit(resource.RLIMIT_CORE))\n"
    masker = SubprocessMasker(
        command=python_child(body),
        timeout_seconds=5,
        descriptor=SubprocessMasker.mock_descriptor("rlimit"),
    )
    assert masker.propose_spans(request_for()).strip() == "(0, 0)"


def test_no_zombie_process_remains_after_success() -> None:
    """正常終了でもreapする。"""
    masker = SubprocessMasker(
        command=python_child(ECHO_EMPTY_SPANS),
        timeout_seconds=5,
        descriptor=SubprocessMasker.mock_descriptor("echo"),
    )
    masker.propose_spans(request_for())
    assert masker.last_pid is not None
    result = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
        [sys.executable, "-c", "pass"], capture_output=True, check=False
    )
    assert result.returncode == 0
    with pytest.raises(ProcessLookupError):
        os.kill(masker.last_pid, 0)
