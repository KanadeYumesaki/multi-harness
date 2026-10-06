"""ローカル UI の HTTP 契約。**Socket を知らない純関数として書く。**

Request を受けて Response を返すだけなので、試験は Port を開かずに全経路を
踏める。Socket を開く仕事は `server.py` が持つ。

## 何を拒むか

不正 JSON、Content-Type 不一致、Body 超過、不明 Conversation、不正 Role、
空本文、本文長超過、Path Traversal、重複 `sequence_number`、不正 Origin、
不正 Session Token、未知 Parameter、未設定 Provider への送信。**通す条件を
書くのではなく、通さない条件を並べる。**

## 状態 Code を固定する

| Code | いつ |
|---|---|
| 400 | JSON・Field・Path の形が不正 |
| 403 | Origin または Session が不正 |
| 404 | Conversation・Resource が無い |
| 409 | 状態競合、重複、Provider 未設定 |
| 413 | Request または本文が大きすぎる |
| 422 | Domain 検証・Masking 拒否 |
| 503 | Artifact・SQLite・Provider Adapter の一時利用不能 |
| 500 | 予期しない内部エラー。**内部情報を返さない** |

## 新しい Error を作らない

UI 専用の Error Code を Domain Error Registry へ足さない。既存 Domain Error
Code を持つ失敗だけ、その Code を Response へ載せる。それ以外は HTTP の状態
Code と短い理由だけで表す。

## 本文を URL へ入れない

Query String を一切受け取らない。`?` を含む Path はその時点で拒否する。
Session Token も Path にも Query にも載せない。Header だけで運ぶ。
"""

from __future__ import annotations

import hmac
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from harness.domain._chat_context_policy_generated import PROVIDER_OUTPUT_ROLE
from harness.domain.context_budget import (
    MessageRole,
    TokenBudgetPolicy,
    TokenOverheads,
    TokenProfileSnapshot,
    final_payload_hash,
)
from harness.domain.conversation import ConversationMessage
from harness.domain.conversation_snapshot import ConversationSnapshot
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash
from harness.domain.knowledge_reference import KnowledgeReference
from harness.presentation.local_ui.composition import LocalUiServices, output_schema_hash
from harness.presentation.local_ui.providers import (
    ExecutionMode,
    ProviderStatusView,
    SendDecision,
    evaluate_mock_turn,
    execution_mode_views,
    load_provider_status,
)
from harness.presentation.local_ui.value_input import (
    ConfirmationRejected,
    build_interview,
    confirmed_values,
    load_route_profile_proposal,
)

__all__ = [
    "SESSION_HEADER",
    "LocalUiApi",
    "Request",
    "Response",
]

SESSION_HEADER: Final[str] = "x-harness-session"

#: Path の形。Query も断片も受け取らない。
_SAFE_PATH = re.compile(r"^/[A-Za-z0-9/._-]*$")

#: `uuid4()` が作る形だけを Conversation ID として受け取る。
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_SESSION_TOKEN = re.compile(r"^[0-9a-f]{64}$")

#: 既存 Domain Error Code から HTTP 状態への写像。**ここに無い Code は 500。**
_STATUS_BY_CODE: Final[Mapping[ErrorCode, int]] = {
    ErrorCode.MASKING_VERIFICATION_FAILED: 422,
    ErrorCode.CONTEXT_BUDGET_EXCEEDED: 422,
    ErrorCode.CONTROL_DATA_ROLE_ESCALATION: 422,
    ErrorCode.SCHEMA_CONDITIONAL_VIOLATION: 422,
    ErrorCode.RUNTIME_SPEC_MISMATCH: 422,
    ErrorCode.TOKEN_PROFILE_DRIFT_DETECTED: 422,
    ErrorCode.PLAN_NONDETERMINISTIC: 422,
    ErrorCode.ARTIFACT_CONTENT_CONFLICT: 503,
    ErrorCode.STORAGE_WRITE_FAILED: 503,
    ErrorCode.MIGRATION_FAILED: 503,
    # Workbench が使う既存 Code。**UI 専用 Code を Registry へ足さない。**
    ErrorCode.PATH_OUTSIDE_CAPABILITY: 422,
    ErrorCode.SYMLINK_DENIED: 422,
    ErrorCode.SPECIAL_FILE_DENIED: 422,
    ErrorCode.MOUNT_CROSSING_DENIED: 422,
    ErrorCode.APPROVAL_REQUIRED: 409,
    ErrorCode.APPROVAL_INVALIDATED: 409,
    ErrorCode.APPROVAL_REPLAY: 409,
    ErrorCode.APPROVAL_ISSUER_UNTRUSTED: 403,
    ErrorCode.CLOCK_SKEW_EXCEEDED: 409,
    ErrorCode.EVENT_ORDER_VIOLATION: 409,
    ErrorCode.STALE_FENCING_TOKEN: 409,
    ErrorCode.EFFECT_UNKNOWN: 409,
    ErrorCode.UNRECONCILED_EFFECT_PRESENT: 409,
    ErrorCode.DEPLOY_DRAIN_REQUIRED: 409,
}

#: Workbench の Session ID の形。`uuid4()` が作る形だけを受け取る。
_WORKBENCH_ID = _UUID

_SECURITY_HEADERS: Final[tuple[tuple[str, str], ...]] = (
    (
        "Content-Security-Policy",
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'none'; "
        "font-src 'none'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("X-Frame-Options", "DENY"),
)


class _Reject(Exception):
    """検査に落ちた。**状態 Code と短い理由だけを持つ。**"""

    def __init__(self, status: int, reason: str, code: str | None = None) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.code = code


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    path: str
    headers: Mapping[str, str]
    body: bytes = b""

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: bytes
    content_type: str
    extra_headers: tuple[tuple[str, str], ...] = field(default=())

    @property
    def headers(self) -> tuple[tuple[str, str], ...]:
        return (
            ("Content-Type", self.content_type),
            ("Content-Length", str(len(self.body))),
            *_SECURITY_HEADERS,
            *self.extra_headers,
        )


def _json(status: int, payload: dict[str, Any]) -> Response:
    body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return Response(status=status, body=body, content_type="application/json; charset=utf-8")


def _error(status: int, reason: str, code: str | None = None) -> Response:
    payload: dict[str, Any] = {"error": {"status": status, "reason": reason}}
    if code is not None:
        payload["error"]["code"] = code
    return _json(status, payload)


class LocalUiApi:
    """Request から Response を作る。**保存経路は Application Service だけ。**"""

    def __init__(
        self,
        services: LocalUiServices,
        *,
        origin: str,
        session_token: str,
        assets: Mapping[str, tuple[bytes, str]],
    ) -> None:
        if not _SESSION_TOKEN.match(session_token):
            raise ValueError("session token must be 64 lowercase hex characters")
        self._services = services
        self._origin = origin
        self._token = session_token
        self._assets = dict(assets)

    # -- 入口 ---------------------------------------------------------------

    def handle(self, request: Request) -> Response:
        try:
            return self._route(request)
        except _Reject as reject:
            return _error(reject.status, reject.reason, reject.code)
        except ConfirmationRejected as rejected:
            # Owner の確認が揃っていない。**Package は書き換えていない。**
            return _error(422, str(rejected), rejected.code)
        except HarnessError as error:
            status = _STATUS_BY_CODE.get(error.code)
            if status is None:
                # **内部情報を返さない。** 未知の失敗は分類できていない。
                return _error(500, "internal error")
            return _error(status, str(error), error.code.value)
        except sqlite3.IntegrityError:
            # 重複 `sequence_number` はここへ来る。状態競合として返す。
            return _error(409, "conflicting write; the record already exists")
        except sqlite3.Error:
            return _error(503, "storage temporarily unavailable")
        except Exception:
            return _error(500, "internal error")

    # -- 経路 ---------------------------------------------------------------

    def _route(self, request: Request) -> Response:
        path = self._safe_path(request.path)
        method = request.method.upper()

        if method == "GET" and path in self._assets:
            payload, content_type = self._assets[path]
            return Response(status=200, body=payload, content_type=content_type)
        if method == "GET" and path == "/api/conversations":
            return self._list_conversations()
        if method == "GET" and path == "/api/providers":
            return self._providers()
        if method == "GET" and path == "/api/provider-values":
            return self._provider_values()
        if method == "GET" and path == "/api/route-profile":
            return self._route_profile()
        if method == "POST" and path == "/api/provider-values":
            self._require_write_permission(request)
            body = self._require_object(
                request, allowed=frozenset({"confirmations", "approve_save"})
            )
            return self._save_provider_values(body)
        if method == "POST" and path in ("/api/knowledge/preview", "/api/knowledge/import"):
            self._require_write_permission(request)
            allowed = frozenset({"source_kind", "title", "content", "omission_note"})
            if path.endswith("/import"):
                allowed = allowed | {"preview_hash", "approve_save"}
            body = self._require_object(request, allowed=allowed)
            return self._knowledge(body, save=path.endswith("/import"))
        if method == "POST" and path == "/api/conversations":
            self._require_write_permission(request)
            self._require_object(request, allowed=frozenset())
            return self._create_conversation()
        if method == "GET" and path == "/api/execution-modes":
            return self._execution_modes()
        if method == "POST" and path == "/api/chat/send":
            self._require_write_permission(request)
            self._require_object(request, allowed=frozenset({"conversation_id", "provider_id"}))
            return self._send_refusal()
        if method == "POST" and path == "/api/chat/mock-turn":
            self._require_write_permission(request)
            body = self._require_object(
                request,
                # **Metadata の入力欄を残さない（`MTM-6-B`）。**
                # `instruction_hash`・`output_schema_hash`・`model_id` は
                # Application が導出する。呼出側から受け取らない。
                allowed=frozenset(
                    {
                        "conversation_id",
                        "provider_id",
                        "execution_mode",
                        "text",
                        "mandatory_message_ids",
                        "budget",
                        "profile",
                    }
                ),
            )
            return self._mock_turn(body)

        if path == "/api/operations" and method == "GET":
            self._require_read_permission(request)
            return _json(200, self._services.operation_inspection())
        if path in ("/api/operations/resume", "/api/operations/reconcile") and method == "POST":
            self._require_write_permission(request)
            body = self._require_object(request, allowed=frozenset({"review_hash", "approve"}))
            if body.get("approve") is not True:
                raise _Reject(409, "explicit operation approval is required")
            return _json(
                200,
                self._services.operation_decision(
                    path.rsplit("/", 1)[1], self._require_str(body, "review_hash")
                ),
            )

        if path == "/api/workbench" and method == "GET":
            self._require_read_permission(request)
            return self._workbench_overview()
        if (
            path in ("/api/workbench/chatgpt/login", "/api/workbench/chatgpt/logout")
            and method == "POST"
        ):
            self._require_write_permission(request)
            body = self._require_object(request, allowed=frozenset({"new_account"}))
            if "new_account" in body and type(body["new_account"]) is not bool:
                raise _Reject(400, "new_account must be a boolean")
            return _json(
                200,
                self._workbench().chatgpt_action(
                    path.rsplit("/", 1)[1],
                    new_account=body.get("new_account") is True,
                ),
            )
        if path == "/api/workbench/preferences" and method == "GET":
            self._require_read_permission(request)
            return _json(200, self._workbench().preferences())
        if path == "/api/workbench/preferences" and method == "POST":
            self._require_write_permission(request)
            body = self._require_object(
                request,
                allowed=frozenset(
                    {"provider_id", "model_id", "reasoning_effort", "expected_version", "approved"}
                ),
            )
            effort = body.get("reasoning_effort")
            if effort is not None and not isinstance(effort, str):
                raise _Reject(400, "reasoning_effort must be a string or null")
            return _json(
                200,
                self._workbench().save_preferences(
                    provider_id=self._require_str(body, "provider_id"),
                    model_id=self._require_str(body, "model_id"),
                    reasoning_effort=effort,
                    expected_version=self._require_int(body, "expected_version"),
                    approved=body.get("approved") is True,
                ),
            )
        if path == "/api/workbench/targets" and method == "GET":
            self._require_read_permission(request)
            return _json(200, self._workbench().targets())
        if path == "/api/workbench/targets/preview" and method == "POST":
            self._require_write_permission(request)
            body = self._require_object(request, allowed=frozenset({"relative_path"}))
            return _json(200, self._workbench().preview(self._require_str(body, "relative_path")))
        if path == "/api/workbench/context-preview" and method == "POST":
            self._require_write_permission(request)
            body = self._require_object(
                request,
                allowed=frozenset({"parent_session_id", "conversation_id", "history_selection"}),
            )
            return _json(
                200,
                self._workbench().context_preview(
                    parent_session_id=self._optional_workbench_id(body, "parent_session_id"),
                    conversation_id=self._optional_workbench_id(body, "conversation_id"),
                    history_selection=body.get("history_selection"),
                ),
            )
        if path == "/api/workbench/sessions" and method == "POST":
            self._require_write_permission(request)
            body = self._require_object(
                request,
                allowed=frozenset(
                    {
                        "provider_id",
                        "model_id",
                        "relative_path",
                        "instruction",
                        "reasoning_effort",
                        "parent_session_id",
                        "conversation_id",
                        "history_selection",
                        "important_notes",
                        "task_kind",
                        "reference_text",
                        "output_format",
                    }
                ),
            )
            if "important_notes" in body and not isinstance(body["important_notes"], str):
                raise _Reject(400, "important_notes must be a string")
            return _json(
                201,
                self._workbench().create_session(
                    provider_id=self._require_str(body, "provider_id"),
                    model_id=self._require_str(body, "model_id"),
                    parent_session_id=self._optional_workbench_id(body, "parent_session_id"),
                    conversation_id=self._optional_workbench_id(body, "conversation_id"),
                    relative_path=self._require_str(body, "relative_path")
                    if body.get("relative_path") is not None
                    else None,
                    task_kind=self._require_str(body, "task_kind")
                    if "task_kind" in body
                    else "file_edit",
                    reference_text=self._string_or_empty(body, "reference_text"),
                    output_format=self._require_str(body, "output_format")
                    if "output_format" in body
                    else "markdown",
                    instruction=self._require_str(body, "instruction"),
                    history_selection=body.get("history_selection"),
                    important_notes=self._require_str(body, "important_notes")
                    if body.get("important_notes")
                    else "",
                    reasoning_effort=(
                        self._require_str(body, "reasoning_effort")
                        if body.get("reasoning_effort") is not None
                        else None
                    ),
                ),
            )

        workbench_detail = path.removeprefix("/api/workbench/sessions/")
        if workbench_detail != path:
            return self._workbench_route(request, method, workbench_detail)

        detail = path.removeprefix("/api/conversations/")
        if detail != path:
            return self._conversation_route(request, method, detail)

        if method in {"GET", "POST"}:
            raise _Reject(404, "no such resource")
        raise _Reject(404, "no such resource")

    @staticmethod
    def _string_or_empty(body: dict[str, Any], key: str) -> str:
        value = body.get(key, "")
        if not isinstance(value, str):
            raise _Reject(400, key + " must be a string")
        return value

    def _optional_workbench_id(self, body: dict[str, Any], key: str) -> str | None:
        if body.get(key) is None:
            return None
        value = self._require_str(body, key)
        if not _UUID.fullmatch(value):
            raise _Reject(400, "history source id is not a uuid")
        return value

    def _workbench(self) -> Any:
        """Workbench が組まれていなければ、**409 で「未設定」と答える。**"""
        gateway = self._services.workbench
        if gateway is None:
            raise _Reject(
                409,
                "workbench is not configured; start the ui with --workspace and "
                "--cli-runtime-profile",
                "WORKBENCH_NOT_CONFIGURED",
            )
        return gateway

    def _workbench_overview(self) -> Response:
        if self._services.workbench is None:
            return _json(
                200,
                {
                    "available": False,
                    "reason": (
                        "起動時に --workspace と --cli-runtime-profile を渡していない。"
                        "画面から Path を指定して有効化することはできない。"
                    ),
                },
            )
        return _json(200, self._services.workbench.overview())

    def _workbench_route(self, request: Request, method: str, detail: str) -> Response:
        parts = detail.split("/")
        session_id = parts[0]
        if not _WORKBENCH_ID.match(session_id):
            raise _Reject(400, "workbench session id is not a uuid")
        tail = parts[1:]
        gateway = self._workbench()

        if method == "GET" and not tail:
            self._require_read_permission(request)
            return _json(200, gateway.session(session_id))
        if method == "GET" and tail == ["confirmation"]:
            self._require_read_permission(request)
            return _json(200, gateway.confirmation(session_id))
        if method == "GET" and tail == ["history"]:
            self._require_read_permission(request)
            return _json(200, gateway.history(session_id))
        if method == "GET" and tail == ["result"]:
            self._require_read_permission(request)
            return _json(200, gateway.result(session_id))
        if method == "GET" and tail == ["diff"]:
            self._require_read_permission(request)
            return _json(200, gateway.diff(session_id))
        if method != "POST":
            raise _Reject(404, "no such resource")

        if tail == ["approve-send"]:
            self._require_write_permission(request)
            body = self._require_object(request, allowed=frozenset({"execution_plan_hash"}))
            return _json(
                200,
                gateway.approve_send(
                    session_id,
                    execution_plan_hash=self._require_str(body, "execution_plan_hash"),
                ),
            )
        if tail == ["send"]:
            self._require_write_permission(request)
            body = self._require_object(request, allowed=frozenset({"execution_plan_hash"}))
            return _json(
                202,
                gateway.start_send(
                    session_id,
                    execution_plan_hash=self._require_str(body, "execution_plan_hash"),
                ),
            )
        if tail == ["resume-send"]:
            self._require_write_permission(request)
            self._require_object(request, allowed=frozenset())
            return _json(202, gateway.resume_send(session_id))
        if tail == ["mark-unknown"]:
            self._require_write_permission(request)
            self._require_object(request, allowed=frozenset())
            return _json(200, gateway.mark_unknown(session_id))
        if tail == ["approve-apply"]:
            self._require_write_permission(request)
            body = self._require_object(
                request, allowed=frozenset({"apply_execution_plan_hash", "proposal_hash"})
            )
            return _json(
                200,
                gateway.approve_apply(
                    session_id,
                    apply_execution_plan_hash=self._require_str(body, "apply_execution_plan_hash"),
                    proposal_hash=self._require_str(body, "proposal_hash"),
                ),
            )
        if tail == ["apply"]:
            self._require_write_permission(request)
            body = self._require_object(request, allowed=frozenset({"apply_execution_plan_hash"}))
            return _json(
                200,
                gateway.apply(
                    session_id,
                    apply_execution_plan_hash=self._require_str(body, "apply_execution_plan_hash"),
                ),
            )
        if tail == ["recover"]:
            self._require_write_permission(request)
            self._require_object(request, allowed=frozenset())
            return _json(200, gateway.recover(session_id))
        raise _Reject(404, "no such resource")

    def _conversation_route(self, request: Request, method: str, detail: str) -> Response:
        parts = detail.split("/")
        conversation_id = parts[0]
        if not _UUID.match(conversation_id):
            raise _Reject(400, "conversation id is not a uuid")
        tail = parts[1:]

        if method == "GET" and not tail:
            return self._conversation_detail(conversation_id)
        if method == "POST" and tail == ["messages"]:
            self._require_write_permission(request)
            body = self._require_object(request, allowed=frozenset({"role", "text"}))
            return self._append_message(conversation_id, body)
        if method == "POST" and tail == ["context-preview"]:
            self._require_write_permission(request)
            body = self._require_object(
                request,
                allowed=frozenset({"snapshot_id", "mandatory_message_ids", "budget", "profile"}),
            )
            return self._context_preview(conversation_id, body)
        if method == "POST" and tail == ["snapshots"]:
            self._require_write_permission(request)
            self._require_object(request, allowed=frozenset())
            return self._build_snapshot(conversation_id)
        raise _Reject(404, "no such resource")

    # -- 検査 ---------------------------------------------------------------

    @staticmethod
    def _safe_path(raw: str) -> str:
        """Path の形だけで落とす。**Filesystem へ触る前に落とす。**

        `?` も `%` も受け取らない。Query を持たない契約なので `?` は不要であり、
        `%` を拒めば二重 Encode による `..` の持ち込みが成立しない。
        """
        if "?" in raw or "#" in raw:
            raise _Reject(400, "query strings and fragments are not accepted")
        if "%" in raw or "\\" in raw or "\x00" in raw:
            raise _Reject(400, "path contains an escape or a backslash")
        if ".." in raw or "//" in raw:
            raise _Reject(400, "path traversal is not accepted")
        if not _SAFE_PATH.match(raw):
            raise _Reject(400, "path shape is not accepted")
        return raw

    def _require_read_permission(self, request: Request) -> None:
        """機微な読取りにも Session Token を要求する。

        Origin は同一 Origin の `GET` では送られない。だから読取りは Token
        だけで守る。Token は Header にしか載らず、CORS を開けていないので
        外部 Origin の Script からは付けられない。
        """
        token = request.header(SESSION_HEADER)
        if not token or not hmac.compare_digest(token, self._token):
            raise _Reject(403, "session token is not valid")

    def _require_write_permission(self, request: Request) -> None:
        """書込みは同一 Origin と Session Token の両方を要求する。"""
        origin = request.header("origin")
        if origin != self._origin:
            raise _Reject(403, "origin is not this server")
        token = request.header(SESSION_HEADER)
        if not token or not hmac.compare_digest(token, self._token):
            raise _Reject(403, "session token is not valid")

    def _require_object(self, request: Request, *, allowed: frozenset[str]) -> dict[str, Any]:
        media = request.header("content-type").split(";")[0].strip().lower()
        if media != "application/json":
            raise _Reject(400, "content-type must be application/json")
        if len(request.body) > self._services.max_body_bytes:
            raise _Reject(413, "request body is larger than the masking policy limit")
        try:
            parsed = json.loads(request.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _Reject(400, "request body is not valid json") from error
        if not isinstance(parsed, dict):
            raise _Reject(400, "request body must be a json object")
        unknown = sorted(set(parsed) - allowed)
        if unknown:
            raise _Reject(400, f"unknown request fields: {unknown}")
        return parsed

    @staticmethod
    def _require_str(body: dict[str, Any], key: str) -> str:
        value = body.get(key)
        if not isinstance(value, str) or not value:
            raise _Reject(400, f"field {key} must be a non-empty string")
        return value

    @staticmethod
    def _require_int(source: Mapping[str, Any], key: str, *, minimum: int = 0) -> int:
        value = source.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise _Reject(400, f"field {key} must be an integer")
        if value < minimum:
            raise _Reject(400, f"field {key} must be >= {minimum}")
        return value

    @staticmethod
    def _require_mapping(body: dict[str, Any], key: str, allowed: frozenset[str]) -> dict[str, Any]:
        value = body.get(key)
        if not isinstance(value, dict):
            raise _Reject(400, f"field {key} must be a json object")
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise _Reject(400, f"unknown fields in {key}: {unknown}")
        missing = sorted(allowed - set(value))
        if missing:
            raise _Reject(400, f"missing fields in {key}: {missing}")
        return value

    # -- 会話 ---------------------------------------------------------------

    def _provider_status(self) -> ProviderStatusView:
        return load_provider_status(self._services.repo_root)

    @staticmethod
    def _display_name(conversation_id: str, created_at: str) -> str:
        """表示名は既存 Field から導く。**保存しない。**

        新しい永続 Field を足すと、Schema に無い値が Store へ入る。
        """
        return f"{created_at} · {conversation_id[:8]}"

    def _conversation_row(self, conversation: Any) -> dict[str, Any]:
        messages = self._services.conversations.list_messages(conversation.conversation_id)
        snapshots = self._services.conversations.list_snapshots(conversation.conversation_id)
        latest = snapshots[-1] if snapshots else None
        return {
            "conversation_id": conversation.conversation_id,
            "display_name": self._display_name(
                conversation.conversation_id, conversation.created_at
            ),
            "created_at": conversation.created_at,
            "producer": conversation.producer,
            "conversation_hash": str(conversation.conversation_hash),
            "hash_verified": self._services.conversations.verify_conversation_hash(conversation),
            "message_count": len(messages),
            "latest_snapshot": None
            if latest is None
            else {
                "snapshot_id": latest.snapshot_id,
                "snapshot_hash": str(latest.snapshot_hash),
                "created_at": latest.created_at,
            },
            "stored_locally": True,
        }

    def _list_conversations(self) -> Response:
        conversations = self._services.conversations.list_conversations()
        status = self._provider_status()
        return _json(
            200,
            {
                "conversations": [self._conversation_row(c) for c in conversations],
                "provider_status": {
                    "any_send_allowed": status.any_send_allowed,
                    "provider_count": len(status.providers),
                    "external_provider_count": status.external_provider_count,
                },
                "storage": {
                    "database": str(self._services.database_path),
                    "artifact_root": str(self._services.artifact_root),
                    "kind": "SQLite + Artifact CAS。ローカルのみ",
                },
            },
        )

    def _knowledge(self, body: dict[str, Any], *, save: bool) -> Response:
        omission_note = body.get("omission_note", "")
        if not isinstance(omission_note, str):
            raise _Reject(400, "omission_note must be text")
        reference = KnowledgeReference(
            source_kind=self._require_str(body, "source_kind"),
            title=self._require_str(body, "title"),
            content=self._require_str(body, "content"),
            omission_note=omission_note,
        )
        if not save:
            checked = self._services.conversations.preview_knowledge(reference)
            return _json(
                200,
                {
                    "preview_hash": str(reference.preview_hash),
                    "body": checked.decode("utf-8"),
                    "size_bytes": len(checked),
                    "trust_level": "UNTRUSTED_EXTERNAL_INPUT",
                    "provider_invoked": False,
                    "stored_locally": False,
                },
            )
        if body.get("approve_save") is not True:
            raise _Reject(409, "explicit local knowledge save approval is required")
        conversation, stored = self._services.conversations.import_knowledge(
            reference=reference,
            expected_preview_hash=self._require_content_hash(body, "preview_hash"),
            conversation_id=self._services.id_source.new_id(),
            conversation_record_id=self._services.id_source.new_id(),
            message_id=self._services.id_source.new_id(),
            message_record_id=self._services.id_source.new_id(),
            artifact_id=self._services.id_source.new_id(),
            created_at=self._services.clock.now(),
            producer=self._services.conversations_producer,
        )
        return _json(
            201,
            {
                "conversation": self._conversation_row(conversation),
                "message": self._message_row(stored.message),
                "preview_hash": str(reference.preview_hash),
                "provider_invoked": False,
                "stored_locally": True,
            },
        )

    def _create_conversation(self) -> Response:
        now = self._services.clock.now()
        conversation = self._services.conversations.create_conversation(
            conversation_id=self._services.id_source.new_id(),
            record_id=self._services.id_source.new_id(),
            created_at=now,
            producer=self._services.conversations_producer,
        )
        return _json(201, {"conversation": self._conversation_row(conversation)})

    def _require_conversation(self, conversation_id: str) -> Any:
        conversation = self._services.conversations.get_conversation(conversation_id)
        if conversation is None:
            raise _Reject(404, "no such conversation")
        return conversation

    def _message_row(self, message: ConversationMessage) -> dict[str, Any]:
        row: dict[str, Any] = {
            "message_id": message.message_id,
            "role": message.role.value,
            "sequence_number": message.sequence_number,
            "created_at": message.created_at,
            "producer": message.producer,
            "content_artifact_hash": str(message.content_artifact_hash),
            "content_hash": str(message.content_hash),
        }
        try:
            body = self._services.conversations.read_body(message)
        except HarnessError as error:
            # **空文字列で代替しない。** 取れなかったことをそのまま述べる。
            row["body"] = None
            row["body_status"] = "UNAVAILABLE"
            row["body_reason"] = error.code.value
            return row
        try:
            row["body"] = body.decode("utf-8")
        except UnicodeDecodeError:
            row["body"] = None
            row["body_status"] = "UNAVAILABLE"
            row["body_reason"] = "NOT_UTF8"
            return row
        row["body_status"] = "AVAILABLE"
        return row

    def _snapshot_row(self, snapshot: ConversationSnapshot) -> dict[str, Any]:
        return {
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_hash": str(snapshot.snapshot_hash),
            "message_set_hash": str(snapshot.message_set_hash),
            "schema_set_hash": str(snapshot.schema_set_hash),
            "design_sha256": str(snapshot.design_sha256),
            "created_at": snapshot.created_at,
            "producer": snapshot.producer,
            "verified": self._services.conversations.verify_snapshot(snapshot),
        }

    def _conversation_detail(self, conversation_id: str) -> Response:
        conversation = self._require_conversation(conversation_id)
        messages = self._services.conversations.list_messages(conversation_id)
        snapshots = self._services.conversations.list_snapshots(conversation_id)
        return _json(
            200,
            {
                "conversation": self._conversation_row(conversation),
                "messages": [self._message_row(m) for m in messages],
                "snapshots": [self._snapshot_row(s) for s in snapshots],
                "roles": [role.value for role in MessageRole],
                "design_sha256": str(self._services.design_sha256),
            },
        )

    def _append_message(self, conversation_id: str, body: dict[str, Any]) -> Response:
        self._require_conversation(conversation_id)
        role_name = self._require_str(body, "role")
        text = body.get("text")
        if not isinstance(text, str):
            raise _Reject(400, "field text must be a string")
        if not text.strip():
            raise _Reject(422, "message text must not be empty")
        try:
            # Role 名を UI 側で並べ直さない。既存 Enum が正本である。
            role = MessageRole(role_name)
        except ValueError as error:
            raise _Reject(422, f"unknown message role: {role_name}") from error
        payload = text.encode("utf-8")
        if len(payload) > self._services.max_body_bytes:
            raise _Reject(413, "message text is larger than the masking policy limit")

        now = self._services.clock.now()
        stored = self._services.conversations.append_message(
            conversation_id=conversation_id,
            role=role,
            body=payload,
            message_id=self._services.id_source.new_id(),
            record_id=self._services.id_source.new_id(),
            created_at=now,
            producer=self._services.conversations_producer,
            artifact_id=self._services.id_source.new_id(),
        )
        return _json(201, {"message": self._message_row(stored.message)})

    def _build_snapshot(self, conversation_id: str) -> Response:
        self._require_conversation(conversation_id)
        snapshot = self._services.conversations.build_snapshot(
            conversation_id=conversation_id,
            snapshot_id=self._services.id_source.new_id(),
            record_id=self._services.id_source.new_id(),
            created_at=self._services.clock.now(),
            producer=self._services.conversations_producer,
            design_sha256=self._services.design_sha256,
        )
        return _json(201, {"snapshot": self._snapshot_row(snapshot)})

    # -- Context Preview ----------------------------------------------------

    def _context_preview(self, conversation_id: str, body: dict[str, Any]) -> Response:
        from harness.application.chat_context_service import MessageSelectionInput

        self._require_conversation(conversation_id)
        snapshot_id = self._require_str(body, "snapshot_id")
        snapshots = {
            s.snapshot_id: s for s in self._services.conversations.list_snapshots(conversation_id)
        }
        snapshot = snapshots.get(snapshot_id)
        if snapshot is None:
            raise _Reject(404, "no such snapshot in this conversation")

        mandatory_raw = body.get("mandatory_message_ids")
        if not isinstance(mandatory_raw, list) or not all(
            isinstance(item, str) for item in mandatory_raw
        ):
            raise _Reject(400, "mandatory_message_ids must be a list of strings")
        mandatory = set(mandatory_raw)

        policy = self._budget_policy(body)
        profile = self._profile(body)

        messages = self._services.conversations.list_messages(conversation_id)
        # 未設定の Message を既定値で補わない。全件へ明示の方針を作る。
        selection = {
            message.message_id: MessageSelectionInput(
                message_id=message.message_id, mandatory=message.message_id in mandatory
            )
            for message in messages
        }
        unknown = sorted(mandatory - set(selection))
        if unknown:
            raise _Reject(422, f"mandatory ids outside this conversation: {unknown}")

        result = self._services.context.build_context(
            snapshot=snapshot,
            policy=policy,
            profile=profile,
            selection=selection,
            bundle_id=self._services.id_source.new_id(),
            receipt_id=self._services.id_source.new_id(),
            input_read_capability_set_hash=self._services.empty_capability_set_hash,
            input_read_evidence_hash=self._services.empty_read_evidence_hash,
            now=self._services.clock.now(),
        )
        assembly = result.assembly
        by_id = {message.message_id: message for message in messages}
        return _json(
            200,
            {
                "selected_fragments": [
                    {
                        "fragment_id": fragment.fragment_id,
                        "role": fragment.message_role.value,
                        "token_count": fragment.token_count,
                        "mandatory": fragment.mandatory,
                        "priority": fragment.priority,
                        "sequence_number": by_id[fragment.fragment_id].sequence_number,
                    }
                    for fragment in assembly.selection.selected_fragments
                ],
                "excluded_fragments": [
                    item.projection() for item in assembly.receipt.excluded_fragments
                ],
                "mandatory_fragment_ids": list(assembly.selection.mandatory_fragment_ids),
                "budget": {
                    "total_tokens": policy.total_tokens,
                    "reserved_output_tokens": policy.reserved_output_tokens,
                    "reserved_tool_tokens": policy.reserved_tool_tokens,
                    "safety_margin_tokens": policy.safety_margin_tokens,
                    "available_input_tokens": assembly.selection.available_input_tokens,
                    "selected_tokens": assembly.selection.selected_tokens,
                    "overflow_policy": policy.overflow_policy,
                },
                "decision_hash": str(assembly.receipt.decision_hash),
                "receipt_projection": {
                    "receipt_id": assembly.receipt.receipt_id,
                    "bundle_id": assembly.receipt.bundle_id,
                    "candidate_fragment_ids": list(assembly.receipt.candidate_fragment_ids),
                    "selected_fragment_ids": list(assembly.receipt.selected_fragment_ids),
                    "excluded_fragment_ids": list(assembly.receipt.excluded_fragment_ids),
                    "deduplication_result": list(assembly.receipt.deduplication_result),
                    "estimated_token_total": assembly.receipt.estimated_token_total,
                    "algorithm_version": assembly.receipt.algorithm_version,
                    "tie_breaker": assembly.receipt.tie_breaker,
                    "token_profile_snapshot_hash": str(
                        assembly.receipt.token_profile_snapshot_hash
                    ),
                    "budget_policy_hash": str(assembly.receipt.budget_policy_hash),
                },
                "bundle_hash": str(assembly.bundle.bundle_hash),
                "send_order": list(result.send_order),
                "token_profile": {
                    "provider": profile.provider,
                    "model": profile.model,
                    "tokenizer_name": profile.tokenizer_name,
                    "tokenizer_version": profile.tokenizer_version,
                    "counting_adapter_version": profile.counting_adapter_version,
                    "estimate_assurance": profile.estimate_assurance.value,
                    "expires_at": profile.expires_at,
                },
            },
        )

    def _budget_policy(self, body: dict[str, Any]) -> TokenBudgetPolicy:
        allowed = frozenset(
            {
                "total_tokens",
                "reserved_output_tokens",
                "reserved_tool_tokens",
                "safety_margin_tokens",
            }
        )
        budget = self._require_mapping(body, "budget", allowed)
        try:
            return TokenBudgetPolicy(
                total_tokens=self._require_int(budget, "total_tokens", minimum=1),
                reserved_output_tokens=self._require_int(budget, "reserved_output_tokens"),
                reserved_tool_tokens=self._require_int(budget, "reserved_tool_tokens"),
                safety_margin_tokens=self._require_int(budget, "safety_margin_tokens"),
                # MVP0-A が定義した値だけを使う。UI から別の値を選ばせない。
                compression_max_depth=0,
                overflow_policy="FAIL_CLOSED",
                policy_id=self._services.id_source.new_id(),
            )
        except ValueError as error:
            raise _Reject(422, f"token budget policy is not valid: {error}") from error

    def _profile(self, body: dict[str, Any]) -> TokenProfileSnapshot:
        allowed = frozenset(
            {
                "provider",
                "model",
                "context_limit",
                "maximum_output_limit",
                "expires_at",
                "overheads",
            }
        )
        raw = self._require_mapping(body, "profile", allowed)
        overhead_fields = frozenset(
            {
                "system_message_overhead",
                "developer_message_overhead",
                "tool_definition_overhead",
                "per_message_overhead",
                "structured_output_overhead",
                "streaming_frame_overhead",
                "retry_fallback_reservation",
            }
        )
        overheads_raw = self._require_mapping(raw, "overheads", overhead_fields)
        identity = self._services.tokenizer_identity
        try:
            return TokenProfileSnapshot(
                snapshot_id=self._services.id_source.new_id(),
                provider=self._require_str(raw, "provider"),
                model=self._require_str(raw, "model"),
                # 計数器の同一性は **実際に数える側**から採る。要求に書かせない。
                tokenizer_name=identity["tokenizer_name"],
                tokenizer_version=identity["tokenizer_version"],
                counting_adapter_version=identity["counting_adapter_version"],
                context_limit=self._require_int(raw, "context_limit", minimum=1),
                maximum_output_limit=self._require_int(raw, "maximum_output_limit", minimum=1),
                estimate_assurance=self._services.counter_assurance,
                overheads=TokenOverheads(
                    **{
                        name: self._require_int(overheads_raw, name)
                        for name in sorted(overhead_fields)
                    }
                ),
                retrieved_at=self._services.clock.now(),
                expires_at=self._require_str(raw, "expires_at"),
            )
        except ValueError as error:
            raise _Reject(422, f"token profile snapshot is not valid: {error}") from error

    # -- Provider -----------------------------------------------------------

    def _providers(self) -> Response:
        status = self._provider_status()
        payload = status.projection()
        payload["message"] = (
            "Providerが未設定のため、外部LLMへの送信は現在利用できません。"
            "会話の保存と履歴確認は利用できます。"
        )
        return _json(200, payload)

    # -- Provider 設定の入力支援 --------------------------------------------

    def _provider_values(self) -> Response:
        """会話形式の段取り。**欄は Package から導く。値は持たない。**"""
        payload = build_interview(self._services.repo_root)
        payload["recorder"] = {
            "module": "harness.infrastructure.owner_value_recorder",
            "note": "検査と保存はここだけが行う。この API は Package へ書かない",
        }
        return _json(200, payload)

    def _route_profile(self) -> Response:
        """用途別の希望。**Active Policy ではない。**"""
        return _json(200, load_route_profile_proposal(self._services.repo_root))

    def _save_provider_values(self, body: dict[str, Any]) -> Response:
        """Owner が確認した値を記録器へ渡す。

        検査も書込みも記録器が行う。ここは「Owner が確かに確認したか」だけを見て、
        通ったものを渡す。**1 欄でも未確認なら渡さない。**
        """
        document = confirmed_values(
            self._services.repo_root,
            body.get("confirmations"),
            approve_save=bool(body.get("approve_save", False)),
        )
        saved = self._services.record_owner_values(document)
        return _json(
            200,
            {
                "saved": True,
                "fields_total": len(saved["fields"]),
                "fields_present": sum(1 for f in saved["fields"] if f["value"] is not None),
                "package_status": saved["status"],
            },
        )

    def _execution_modes(self) -> Response:
        """実行方式の一覧。**実装状況を偽らない。**"""
        return _json(
            200,
            {
                "modes": execution_mode_views(self._services.repo_root),
                "note": (
                    "実行できるのは Mock だけである。表示は承認でも送信許可でもRelease 判定でもない"
                ),
            },
        )

    def _require_execution_mode(self, body: dict[str, Any]) -> ExecutionMode:
        raw = self._require_str(body, "execution_mode")
        try:
            return ExecutionMode(raw)
        except ValueError as error:
            raise _Reject(422, f"unknown execution mode: {raw}") from error

    def _decide_mock_turn(self, body: dict[str, Any]) -> tuple[ExecutionMode, str, SendDecision]:
        """送ってよいかを先に決める。**保存より前に決める。**"""
        mode = self._require_execution_mode(body)
        provider_id = self._require_str(body, "provider_id")
        decision = evaluate_mock_turn(self._services.repo_root, mode=mode, provider_id=provider_id)
        return mode, provider_id, decision

    def _mock_turn(self, body: dict[str, Any]) -> Response:
        """Mock の 1 往復。**外部へ 1 Byte も出さない。**

        順序は変えない。判定 → User Message 保存 → Snapshot → Context →
        Mock 応答 → Assistant Message 保存である。判定を後ろへ回すと、送れない
        状態でも本文が保存される。
        """
        from harness.ports.provider import ProviderRequest

        mode, provider_id, decision = self._decide_mock_turn(body)
        if not decision.allowed:
            return _json(
                409,
                {
                    "error": {"status": 409, "reason": "この実行方式では送信できません"},
                    "decision": decision.projection(),
                    "network_used": False,
                    "assistant_message_created": False,
                },
            )

        conversation_id = self._require_str(body, "conversation_id")
        if not _UUID.match(conversation_id):
            raise _Reject(400, "conversation id is not a uuid")
        self._require_conversation(conversation_id)

        text = body.get("text")
        if not isinstance(text, str):
            raise _Reject(400, "field text must be a string")
        if not text.strip():
            raise _Reject(422, "message text must not be empty")
        payload = text.encode("utf-8")
        if len(payload) > self._services.max_body_bytes:
            raise _Reject(413, "message text is larger than the masking policy limit")

        mandatory_raw = body.get("mandatory_message_ids")
        if not isinstance(mandatory_raw, list) or not all(
            isinstance(item, str) for item in mandatory_raw
        ):
            raise _Reject(400, "mandatory_message_ids must be a list of strings")
        policy = self._budget_policy(body)
        profile = self._profile(body)

        now = self._services.clock.now()
        user_stored = self._services.conversations.append_message(
            conversation_id=conversation_id,
            role=MessageRole.USER_TASK,
            body=payload,
            message_id=self._services.id_source.new_id(),
            record_id=self._services.id_source.new_id(),
            created_at=now,
            producer=self._services.conversations_producer,
            artifact_id=self._services.id_source.new_id(),
        )

        snapshot = self._services.conversations.build_snapshot(
            conversation_id=conversation_id,
            snapshot_id=self._services.id_source.new_id(),
            record_id=self._services.id_source.new_id(),
            created_at=now,
            producer=self._services.conversations_producer,
            design_sha256=self._services.design_sha256,
        )

        result = self._build_turn_context(
            conversation_id=conversation_id,
            snapshot=snapshot,
            mandatory=set(mandatory_raw),
            policy=policy,
            profile=profile,
        )
        assembly = result.assembly

        # **いま送った本文が Context に残っていなければ送らない。**
        #
        # Chat の Message は mandatory にできないので、予算が足りなければ例外に
        # ならず静かに除外される。除外されたまま応答を保存すると、その発話への
        # 応答ではないものが、その発話への応答として履歴に残る。
        if user_stored.message.message_id not in assembly.receipt.selected_fragment_ids:
            excluded = [
                item.projection()
                for item in assembly.receipt.excluded_fragments
                if item.fragment_id == user_stored.message.message_id
            ]
            return _json(
                409,
                {
                    "error": {
                        "status": 409,
                        "reason": "送る本文が Context 予算に収まらないため送信しません",
                    },
                    "decision": decision.projection(),
                    "user_message": self._message_row(user_stored.message),
                    "excluded_input": excluded,
                    "budget": {
                        "available_input_tokens": assembly.selection.available_input_tokens,
                        "selected_tokens": assembly.selection.selected_tokens,
                        "overflow_policy": policy.overflow_policy,
                    },
                    "network_used": False,
                    "assistant_message_created": False,
                },
            )

        # **Model の出所は Profile だけである（`MTM-4-A`）。**
        #
        # 送信要求で別の Model を受け取らないので、2 つが食い違いようがない。
        # Profile の Model が空なら `_profile` が既に 400／422 で止めている。
        # `_profile` が `_require_str` で空を落としているので、ここで再び
        # 見張らない。**死んだ分岐を残さない。** 空の Profile Model は
        # `_profile` の時点で 400 になる。
        model_id = profile.model

        # **Hash 対象は Context 選択の後に確定した最終送信 payload である（`MTM-2-B`）。**
        #
        # `bundle_hash` は使わない。あれは内部 ID（`snapshot_id`・`policy_id`）を
        # 含み、並びも選択順である。同じ payload でも呼出ごとに値が変わるので、
        # 再現性の意味を持てない。**2 つを同じ意味だと宣言しない。**
        #
        # 送るのは role と本文だけで、順序は `send_order`（会話の順）である。
        # 正規化は既存の `hash_canonical`（RFC 8785 JCS）をそのまま使う。
        by_id = {
            fragment.fragment_id: fragment for fragment in assembly.selection.selected_fragments
        }
        instruction_hash = final_payload_hash(
            [by_id[fragment_id] for fragment_id in result.send_order]
        )

        # **Schema を発明しない（`MTM-3-A`）。** Mock の応答は Schema 検証を
        # 通していないので、指す Schema が無い。`InvocationManifest` は任意と
        # しているので `None` のままにする。
        schema_hash = output_schema_hash(
            self._services.schema_catalog, schema_name=None, schema_version=None
        )

        response = self._services.provider.propose(
            ProviderRequest(
                provider_id=provider_id,
                model_id=model_id,
                context_bundle_hash=assembly.bundle.bundle_hash,
                input_artifact_hash=user_stored.message.content_artifact_hash,
                instruction_hash=instruction_hash,
                output_schema_hash=schema_hash,
            )
        )
        # **応答が外部へ出ていないことを確かめてから保存する。**
        if response.network_used or response.provider_id != provider_id:
            raise _Reject(409, "provider response did not stay local")

        assistant_stored = self._services.conversations.append_message(
            conversation_id=conversation_id,
            role=MessageRole(PROVIDER_OUTPUT_ROLE),
            body=response.artifact_bytes,
            message_id=self._services.id_source.new_id(),
            record_id=self._services.id_source.new_id(),
            created_at=now,
            producer=self._services.conversations_producer,
            artifact_id=self._services.id_source.new_id(),
            media_type="application/json",
        )

        return _json(
            201,
            {
                "decision": decision.projection(),
                "execution_mode": mode.value,
                "user_message": self._message_row(user_stored.message),
                "provider_message": self._message_row(assistant_stored.message),
                "snapshot": self._snapshot_row(snapshot),
                "context": {
                    "selected_fragment_ids": list(assembly.receipt.selected_fragment_ids),
                    "excluded_fragments": [
                        item.projection() for item in assembly.receipt.excluded_fragments
                    ],
                    "selected_tokens": assembly.selection.selected_tokens,
                    "available_input_tokens": assembly.selection.available_input_tokens,
                    "bundle_hash": str(assembly.bundle.bundle_hash),
                    "decision_hash": str(assembly.receipt.decision_hash),
                    "send_order": list(result.send_order),
                },
                "provider_response": {
                    "provider_id": response.provider_id,
                    "model_id": response.model_id,
                    "adapter_version": response.adapter_version,
                    "artifact_hash": str(response.artifact_hash),
                    "network_used": response.network_used,
                    "billing_mode": response.billing_mode,
                },
                # **導出したものを、導出したと分かる形で出す。**
                "derived_metadata": {
                    "instruction_hash": str(instruction_hash),
                    "instruction_hash_source": "FINAL_SEND_PAYLOAD_AFTER_SELECTION",
                    "output_schema_hash": None if schema_hash is None else str(schema_hash),
                    "output_schema_source": "NOT_USED_BY_THE_MOCK_ROUTE",
                    "model_id": model_id,
                    "model_id_source": "TOKEN_PROFILE_SNAPSHOT",
                    "caller_supplied_metadata": [],
                },
                "network_used": False,
                "external_send_performed": False,
            },
        )

    def _require_content_hash(self, source: Mapping[str, Any], key: str) -> ContentHash:
        """`sha256:…` を値オブジェクトへ変える。**文字列のまま渡さない。**"""
        raw = self._require_str(dict(source), key)
        try:
            return ContentHash.parse(raw)
        except ValueError as error:
            raise _Reject(422, f"field {key} is not a content hash") from error

    def _build_turn_context(
        self,
        *,
        conversation_id: str,
        snapshot: Any,
        mandatory: set[str],
        policy: TokenBudgetPolicy,
        profile: TokenProfileSnapshot,
    ) -> Any:
        """会話全体を候補にして Context を組む。**選択の既定値を作らない。**"""
        from harness.application.chat_context_service import MessageSelectionInput

        messages = self._services.conversations.list_messages(conversation_id)
        selection = {
            message.message_id: MessageSelectionInput(
                message_id=message.message_id, mandatory=message.message_id in mandatory
            )
            for message in messages
        }
        unknown = sorted(mandatory - set(selection))
        if unknown:
            raise _Reject(422, f"mandatory ids outside this conversation: {unknown}")
        return self._services.context.build_context(
            snapshot=snapshot,
            policy=policy,
            profile=profile,
            selection=selection,
            bundle_id=self._services.id_source.new_id(),
            receipt_id=self._services.id_source.new_id(),
            input_read_capability_set_hash=self._services.empty_capability_set_hash,
            input_read_evidence_hash=self._services.empty_read_evidence_hash,
            now=self._services.clock.now(),
        )

    def _send_refusal(self) -> Response:
        """未設定 Provider への送信を拒む。**Network を起こさない。**

        ここで Mock 応答を返さない。返せば架空の Assistant Message を作った
        ことになり、後から本物と見分けられなくなる。
        """
        status = self._provider_status()
        if status.any_send_allowed:  # pragma: no cover - 現時点で到達しない
            raise _Reject(409, "send path is not implemented for any provider")
        return _json(
            409,
            {
                "error": {
                    "status": 409,
                    "reason": (
                        "Providerが未設定のため、外部LLMへの送信は現在利用できません。"
                        "会話の保存と履歴確認は利用できます。"
                    ),
                },
                "blocking_reasons": list(status.blocking_reasons),
                "network_used": False,
                "assistant_message_created": False,
            },
        )
