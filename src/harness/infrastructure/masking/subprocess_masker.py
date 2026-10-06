"""子ProcessとしてMaskerを起動するAdapter（ADR-007 §7、`masker_timeout_seconds`）。

## なぜProcess境界なのか

Pythonは実行中の関数を安全に中断できない。`signal.alarm` は主スレッドの
bytecode境界でしか効かず、C拡張やI/O待ちの最中には割り込めない。
`threading` で待っても、走り続けるスレッドを殺す手段が無い。

Maskerを子Processにすれば、Timeoutは「待つのをやめて殺す」という確実な
操作になる。ADR-007 §7 がMaskerをProcess分離する理由の一つでもある。
隔離要件（Network Egress遮断、Core Dump禁止、Memory Lock）も同じ境界で課す。

## 本文はstdinで渡す

argvへ載せてはならない。`/proc/<pid>/cmdline` は同一ホストの他Userから
読める。Raw PIIをそこへ置けば、隔離を何重に固めても意味が無い。

## 隔離の申告は実際に課した分だけ

Pythonから確実に課せるのは `RLIMIT_CORE=0` 程度である。Network Egress遮断や
`mlockall` はNamespaceや専用Launcherを要する。**課していないものをtrueで
申告すると、Fail-Closedの判定が嘘の申告で通ってしまう。**

したがって本Adapterは現行Policyの隔離要件を満たさず、Pipelineは
`MASKER_ISOLATION_INCOMPLETE` で拒否する。それが正しい状態である。
実隔離を備えたLauncherを用意した時点で申告を上げる。
"""

from __future__ import annotations

import contextlib
import json
import os
import resource
import signal
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from harness.domain.masking_result import MaskerDescriptor
from harness.ports.masker import (
    MaskerIsolationReport,
    MaskerRequest,
    MaskerUnavailableError,
    MaskerUnavailableReason,
)

if TYPE_CHECKING:
    from harness.infrastructure.masking.policy import MaskingPolicy

__all__ = ["SubprocessMasker"]

# `terminate()` のあと、この秒数だけ待ってから `kill()` する。
# SIGTERMで後片付けさせる猶予を与えつつ、無視されたら確実に殺す。
_TERMINATE_GRACE_SECONDS = 2.0


def _drop_core_dumps() -> None:
    """子Process側で実行される。Core DumpにはRaw PIIが丸ごと載る。"""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _process_group_of(process: subprocess.Popen[str]) -> int | None:
    """撃ってよいProcess Groupを返す。確信が持てなければNoneを返す。

    `start_new_session=True` で起動しているので、子はSession兼Group の
    先頭であり `pgid == pid` になる。**この等式を実際に確かめる。**
    確かめずに `process.pid` をGroup IDとして使うと、万一そうでなかった
    ときに `killpg` が**自分のGroupを撃つ**。Harness自身とその親を巻き込む
    ため、疑わしければGroup操作をやめて直接の子だけを対象にする。
    """
    try:
        group = os.getpgid(process.pid)
    except (ProcessLookupError, PermissionError):
        return None  # 既に終了しているか、覗けない
    return group if group == process.pid else None


def _signal_group_then_process(
    process: subprocess.Popen[str], group: int | None, number: int
) -> None:
    """Groupへ送る。送れなければ直接の子へ送る。"""
    if group is not None:
        try:
            os.killpg(group, number)
            return
        except (ProcessLookupError, PermissionError):
            return
    with contextlib.suppress(ProcessLookupError):
        process.send_signal(number)


def _close_pipes(process: subprocess.Popen[str]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()


@dataclass(frozen=True, slots=True)
class _Isolation:
    """本Adapterが**実際に課している**制約。申告はここから作る。"""

    core_dump_disabled: bool = True
    # 以下はPythonの`subprocess`だけでは課せない。専用Launcherが要る。
    network_egress_denied: bool = False
    telemetry_disabled: bool = False
    prompt_logging_disabled: bool = False
    temp_files_disallowed: bool = False
    memory_locked: bool = False


class SubprocessMasker:
    """Maskerを子Processとして呼ぶ。Timeout超過でProcessを停止する。"""

    def __init__(
        self,
        *,
        command: tuple[str, ...],
        timeout_seconds: int,
        descriptor: MaskerDescriptor,
    ) -> None:
        if not command:
            raise ValueError("masker command must not be empty")
        self._command = tuple(command)
        self._timeout_seconds = timeout_seconds
        self._descriptor = descriptor
        self._last_pid: int | None = None

    @classmethod
    def from_policy(
        cls,
        *,
        command: tuple[str, ...],
        policy: MaskingPolicy,
        descriptor: MaskerDescriptor,
    ) -> SubprocessMasker:
        """Timeoutを`masking-policy.yaml`から取る（不変条件#18）。"""
        return cls(
            command=command,
            timeout_seconds=policy.masker_timeout_seconds,
            descriptor=descriptor,
        )

    @staticmethod
    def mock_descriptor(name: str) -> MaskerDescriptor:
        """試験・診断用。実Providerではないと分かる値にする。"""
        return MaskerDescriptor(
            provider="SUBPROCESS",
            model=name,
            model_digest="sha256:" + "0" * 64,
            instruction_hash="sha256:" + "0" * 64,
        )

    # ------------------------------------------------------------------

    @property
    def command(self) -> tuple[str, ...]:
        return self._command

    @property
    def timeout_seconds(self) -> int:
        return self._timeout_seconds

    @property
    def last_pid(self) -> int | None:
        """直近に起動した子ProcessのPID。停止確認の試験で使う。"""
        return self._last_pid

    def descriptor(self) -> MaskerDescriptor:
        return self._descriptor

    def verify_isolation(self) -> MaskerIsolationReport:
        isolation = _Isolation()
        return MaskerIsolationReport(
            network_egress_denied=isolation.network_egress_denied,
            telemetry_disabled=isolation.telemetry_disabled,
            prompt_logging_disabled=isolation.prompt_logging_disabled,
            core_dump_disabled=isolation.core_dump_disabled,
            temp_files_disallowed=isolation.temp_files_disallowed,
            memory_locked=isolation.memory_locked,
        )

    # ------------------------------------------------------------------

    def propose_spans(self, request: MaskerRequest) -> str:
        """子Processへ要求を渡し、標準出力をそのまま返す。

        出力の妥当性は判断しない。`parse_masker_output()` が
        `MASKER_OUTPUT_MALFORMED` として扱う。Adapterがここで判断すると
        検証が2箇所へ散る。
        """
        payload = json.dumps(
            {
                "text": request.text,
                "source_normalized_hash": request.source_normalized_hash,
                "normalization_profile": request.normalization_profile,
                "normalization_profile_artifact_hash": (
                    request.normalization_profile_artifact_hash
                ),
                "allowed_categories": list(request.allowed_categories),
                "candidate_spans": [
                    {"start": span.start, "end": span.end, "category": span.category}
                    for span in request.candidate_spans
                ],
            },
            ensure_ascii=False,
        )

        try:
            process = subprocess.Popen(  # noqa: S603 - 固定argv、shell不使用
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                preexec_fn=_drop_core_dumps,
                # 子を新Sessionの先頭に置く。Maskerがさらに子を起こした場合、
                # 直接の子だけを殺しても孫は生き残る。孫はMaskerの記憶空間を
                # 継いでおり、Raw PIIを持ったまま親を失って走り続ける。
                # Session を切っておけば、Groupごとまとめて落とせる。
                start_new_session=True,
            )
        except OSError as exc:
            raise MaskerUnavailableError(
                f"cannot launch masker process: {exc.strerror}",
                reason=MaskerUnavailableReason.LAUNCH_FAILED,
            ) from None

        self._last_pid = process.pid
        group = _process_group_of(process)
        try:
            stdout, _stderr = process.communicate(payload, timeout=self._timeout_seconds)
        except subprocess.TimeoutExpired:
            self._terminate(process, group)
            raise MaskerUnavailableError(
                f"masker timed out after {self._timeout_seconds}s and was terminated",
                reason=MaskerUnavailableReason.TIMEOUT,
            ) from None
        except BaseException:
            # KeyboardInterrupt等でも子を残さない。
            self._terminate(process, group)
            raise

        if process.returncode != 0:
            # stderrは載せない。Maskerが本文の断片を書いている可能性がある。
            raise MaskerUnavailableError(
                f"masker process exited with status {process.returncode}",
                reason=MaskerUnavailableReason.NONZERO_EXIT,
            )
        return stdout

    @staticmethod
    def _terminate(process: subprocess.Popen[str], group: int | None) -> None:
        """確実に殺してreapする。

        SIGTERMで後片付けの猶予を与え、無視されたらSIGKILLへ上げる。
        `wait()` まで行うのは、Zombieを残さないためである。Maskerは
        Raw PIIを持っているため、生き残ること自体が危険でもある。

        直接の子が死んでも**孫は残りうる**ので、最後にProcess Groupごと
        SIGKILLする。直接の子が猶予内に終了した場合も同じである。
        孫が自分でsetsidして別Sessionへ移っていれば届かないが、
        そこまでやる相手はもはや「Timeoutした」では済まない。

        `wait()` が回収するのは直接の子だけである。孫はinitへ引き取られて
        そちらでreapされるため、Zombieにはならない。
        """
        try:
            if process.poll() is None:
                _signal_group_then_process(process, group, signal.SIGTERM)
                try:
                    process.wait(timeout=_TERMINATE_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    _signal_group_then_process(process, group, signal.SIGKILL)
                    process.wait()
            if group is not None:
                # 子が素直に終わっていても、孫が残っている可能性は消えない。
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(group, signal.SIGKILL)
        finally:
            # `communicate()` はTimeoutで抜けるときPipeを閉じない。
            # 閉じ忘れると実行のたびに3本ずつ漏れ、やがて無関係な処理が
            # `EMFILE` で失敗しはじめる。そのとき原因はここには見えない。
            _close_pipes(process)
