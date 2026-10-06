"""画面と Workbench Application のあいだの直列化層。

## なぜ Gateway が要るか

`ThreadingHTTPServer` は Request ごとに別 Thread を作る。SQLite 接続を素のまま
共有すると `check_same_thread` で落ち、画面には「storage temporarily
unavailable」としか出ない。**接続の所有権と直列実行をここで決める。**

| 資源 | 所有者 | 直列化 |
|---|---|---|
| Request 用 Connection | Gateway | `_lock`（1 Request ずつ） |
| Worker 用 Connection | 送信 Worker | `_worker_lock`（1 実行ずつ） |

## 長い CLI 呼出しのあいだ Lock を持たない

`start_send` は **Transaction を伴う claim だけ** を Lock 内で行い、起動は Worker
Thread へ渡す。だから生成中も一覧・状態取得・差分表示が止まらない。

## 初期版は 1 実行まで

同時に走らせられる送信は 1 件だけである。2 件目は 409 で断る。ブラウザーの
disabled Button ではなく **サーバー側の状態** が二重実行を止める。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from harness.application.chatgpt_connection import ChatGptConnectionService
from harness.application.workbench_service import WorkbenchService
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.workbench import CONTINUABLE_STATES

__all__ = ["WorkbenchGateway", "WorkbenchJobState"]


@dataclass(frozen=True, slots=True)
class WorkbenchJobState:
    """送信 Worker の見え方。**正本は DB の Session State である。**"""

    running_session_id: str | None
    last_session_id: str | None
    last_error: str | None

    def projection(self) -> dict[str, Any]:
        return {
            "running_session_id": self.running_session_id,
            "last_session_id": self.last_session_id,
            "last_error": self.last_error,
            "note": "ジョブの正本は同じ DB の Session State である。ここは補助表示にすぎない",
        }


class WorkbenchGateway:
    def __init__(
        self,
        *,
        request_service: WorkbenchService,
        worker_service: WorkbenchService,
        auth_session: str,
        chatgpt_connection: ChatGptConnectionService | None = None,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self.chatgpt_connection = chatgpt_connection
        self._service = request_service
        self._worker = worker_service
        self._auth_session = auth_session
        self._lock = threading.RLock()
        self._worker_lock = threading.RLock()
        self._running: str | None = None
        self._last_session: str | None = None
        self._last_error: str | None = None
        self._spawn = spawn or _thread_spawn
        self._idle = threading.Event()
        self._idle.set()

    # -- 読み取り -----------------------------------------------------------

    def overview(self) -> dict[str, Any]:
        with self._lock:
            return {
                "available": True,
                "task_catalog": self._service.task_catalog(),
                "workspace": self._service.workspace_view(),
                "providers": self._service.provider_status(),
                "preferences": self._service.preference_view(),
                "sessions": self._service.list_sessions(),
                "job": self.job_state().projection(),
                "auth_session": self._auth_session,
                "chatgpt": None
                if self.chatgpt_connection is None
                else self.chatgpt_connection.status(),
            }

    def targets(self) -> dict[str, Any]:
        with self._lock:
            return {"targets": self._service.list_targets()}

    def preview(self, relative_path: str) -> dict[str, Any]:
        with self._lock:
            return self._service.preview_target(relative_path)

    def session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            return _summary(self._service.inspect(session_id), self.job_state())

    def confirmation(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            return self._service.confirmation(session_id)

    def history(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            return self._service.conversation_history(session_id)

    def result(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            return self._service.result(session_id)

    def diff(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            return self._service.diff(session_id)

    def job_state(self) -> WorkbenchJobState:
        return WorkbenchJobState(self._running, self._last_session, self._last_error)

    # -- 変更 ---------------------------------------------------------------

    def context_preview(
        self,
        *,
        parent_session_id: str | None,
        conversation_id: str | None,
        history_selection: dict[str, Any] | None,
    ) -> dict[str, Any]:
        with self._lock:
            return self._service.context_preview(
                parent_session_id=parent_session_id,
                conversation_id=conversation_id,
                history_selection=history_selection,
            )

    def chatgpt_action(self, action: str, *, new_account: bool = False) -> dict[str, Any]:
        with self._lock:
            if self.chatgpt_connection is None:
                raise HarnessError(
                    ErrorCode.RUNTIME_SPEC_MISMATCH, "ChatGPT connection unavailable"
                )
            if self._running is not None:
                raise HarnessError(
                    ErrorCode.UNRECONCILED_EFFECT_PRESENT, "wait for the running generation"
                )
            if action == "login":
                return self.chatgpt_connection.start(new_account=new_account)
            if action == "logout":
                return self.chatgpt_connection.disconnect()
            raise HarnessError(ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "unknown login operation")

    def preferences(self) -> dict[str, Any]:
        with self._lock:
            return self._service.preference_view()

    def save_preferences(
        self,
        *,
        provider_id: str,
        model_id: str,
        reasoning_effort: str | None,
        expected_version: int,
        approved: bool,
    ) -> dict[str, Any]:
        with self._lock:
            return self._service.save_preference(
                provider_id=provider_id,
                model_id=model_id,
                reasoning_effort=reasoning_effort,
                expected_version=expected_version,
                approved=approved,
            )

    def create_session(
        self,
        *,
        provider_id: str,
        model_id: str,
        relative_path: str | None,
        instruction: str,
        reasoning_effort: str | None = None,
        parent_session_id: str | None = None,
        conversation_id: str | None = None,
        history_selection: dict[str, Any] | None = None,
        important_notes: str = "",
        task_kind: str = "file_edit",
        reference_text: str = "",
        output_format: str = "markdown",
    ) -> dict[str, Any]:
        with self._lock:
            document = self._service.create_send_plan(
                provider_id=provider_id,
                model_id=model_id,
                relative_path=relative_path,
                instruction=instruction,
                reasoning_effort=reasoning_effort,
                parent_session_id=parent_session_id,
                conversation_id=conversation_id,
                history_selection=history_selection,
                important_notes=important_notes,
                task_kind=task_kind,
                reference_text=reference_text,
                output_format=output_format,
            )
            return _summary(document, self.job_state())

    def approve_send(self, session_id: str, *, execution_plan_hash: str) -> dict[str, Any]:
        with self._lock:
            document = self._service.approve_send(
                session_id,
                execution_plan_hash=execution_plan_hash,
                auth_session=self._auth_session,
            )
            return _summary(document, self.job_state())

    def start_send(self, session_id: str, *, execution_plan_hash: str) -> dict[str, Any]:
        """承認を消費して Journal を確定し、起動は Worker へ渡す。"""
        with self._lock:
            if self._running is not None:
                raise HarnessError(
                    ErrorCode.APPROVAL_REPLAY,
                    "another CLI invocation is already running; the initial version allows one",
                )
            document = self._service.inspect(session_id)
            if document["execution_plan_hash"] != execution_plan_hash:
                raise HarnessError(
                    ErrorCode.APPROVAL_INVALIDATED, "send names a different execution plan"
                )
            document = self._service.claim_send(session_id)
            self._running = session_id
            self._last_session = session_id
            self._last_error = None
            self._idle.clear()
        self._spawn(lambda: self._dispatch(session_id))
        with self._lock:
            return _summary(document, self.job_state())

    def resume_send(self, session_id: str) -> dict[str, Any]:
        """再起動後に `SEND_PREPARED` で残った Session を **明示操作で** 送る。"""
        with self._lock:
            if self._running is not None:
                raise HarnessError(
                    ErrorCode.APPROVAL_REPLAY, "another CLI invocation is already running"
                )
            document = self._service.inspect(session_id)
            if document["state"] != "SEND_PREPARED":
                raise HarnessError(
                    ErrorCode.APPROVAL_REQUIRED, "only a prepared, unsent session can resume"
                )
            self._running = session_id
            self._last_session = session_id
            self._last_error = None
            self._idle.clear()
        self._spawn(lambda: self._dispatch(session_id))
        with self._lock:
            return _summary(self._service.inspect(session_id), self.job_state())

    def mark_unknown(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            return _summary(self._service.mark_send_unknown(session_id), self.job_state())

    def approve_apply(
        self, session_id: str, *, apply_execution_plan_hash: str, proposal_hash: str
    ) -> dict[str, Any]:
        with self._lock:
            document = self._service.approve_apply(
                session_id,
                apply_execution_plan_hash=apply_execution_plan_hash,
                proposal_hash=proposal_hash,
                auth_session=self._auth_session,
            )
            return _summary(document, self.job_state())

    def apply(self, session_id: str, *, apply_execution_plan_hash: str) -> dict[str, Any]:
        with self._lock:
            document = self._service.inspect(session_id)
            if document.get("task_kind", "file_edit") != "file_edit":
                raise HarnessError(
                    ErrorCode.PATH_OUTSIDE_CAPABILITY, "text artifacts cannot be applied"
                )
            if document.get("apply_execution_plan_hash") != apply_execution_plan_hash:
                raise HarnessError(ErrorCode.APPROVAL_INVALIDATED, "apply names a different plan")
            return _summary(self._service.run_apply(session_id), self.job_state())

    def recover(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            return _summary(self._service.recover_apply(session_id), self.job_state())

    # -- Worker -------------------------------------------------------------

    def _dispatch(self, session_id: str) -> None:
        try:
            with self._worker_lock:
                self._worker.dispatch_send(session_id)
        except HarnessError as error:
            # 分類済みの停止。**本文を推測で言い換えない。**
            self._remember(f"{error.code.value}: {error}")
        except Exception as error:
            self._remember(type(error).__name__)
        finally:
            with self._lock:
                self._running = None
            self._idle.set()

    def _remember(self, message: str) -> None:
        with self._lock:
            self._last_error = message[:400]

    def wait_for_idle(self, timeout: float) -> bool:
        """試験と CLI 終了待ちのための同期点。**画面はこれを使わない。**"""
        return self._idle.wait(timeout)

    def shutdown(self, timeout: float = 60.0) -> bool:
        """走っている送信を止めてから静かになるまで待つ。

        **接続を閉じる前に必ず呼ぶ。** Worker が SQLite 接続を使っている最中に
        接続を閉じると Process ごと落ちる。止めた送信は「送っていない」ではなく
        `EFFECT_UNKNOWN` として残る。

        静かにできたかどうかを返す。できなければ呼出側は接続を閉じない。
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                running = self._running is not None
            if not running:
                return True
            # spawn の直前で止めようとした場合、まだ Process が無い。**取りこぼさない
            # ように、静かになるまで繰り返し止めにいく。**
            self._worker.runner.stop()
            if self._worker.chatgpt is not None:
                self._worker.chatgpt.stop()
            if self._idle.wait(0.25):
                return True
            if time.monotonic() > deadline:
                return self._idle.is_set()


def _thread_spawn(job: Callable[[], None]) -> None:
    thread = threading.Thread(target=job, name="workbench-send", daemon=True)
    thread.start()


def _summary(document: dict[str, Any], job: WorkbenchJobState) -> dict[str, Any]:
    """画面へ返す形。**Session Document の全 Field を素通ししない。**"""
    return {
        "session_id": document["session_id"],
        "state": document["state"],
        "version": document["version"],
        "created_at": document["created_at"],
        "expires_at": document["expires_at"],
        "provider_id": document["provider_id"],
        "model_id": document["model_id"],
        "reasoning_effort": document.get("reasoning_effort"),
        "target": document["target"],
        "task_kind": document.get("task_kind", "file_edit"),
        "output_format": document.get("output_format"),
        "can_apply": document.get("task_kind", "file_edit") == "file_edit",
        "before_hash": document["before_hash"],
        "before_size": document["before_size"],
        "instruction": document["instruction"],
        "handoff": document.get("handoff"),
        "can_continue": document["state"] in CONTINUABLE_STATES,
        "execution_plan_hash": document["execution_plan_hash"],
        "plan_content_hash": document["plan_content_hash"],
        "request_payload_hash": document["request_payload_hash"],
        "runtime_hash": document["runtime_hash"],
        "commercial_disclosure": document["commercial_disclosure"],
        "limits": document["limits"],
        "workspace_label": document["workspace_label"],
        "proposal_hash": document.get("proposal_hash"),
        "proposal_size": document.get("proposal_size"),
        "response_hash": document.get("response_hash"),
        "send_observation": document.get("send_observation"),
        "failure": document.get("failure"),
        "apply_execution_plan_hash": document.get("apply_execution_plan_hash"),
        "apply_expires_at": document.get("apply_expires_at"),
        "apply_result": document.get("apply_result"),
        "effect_receipt_hash": document.get("effect_receipt_hash"),
        "recovery_decision": document.get("recovery_decision"),
        "job": job.projection(),
    }
