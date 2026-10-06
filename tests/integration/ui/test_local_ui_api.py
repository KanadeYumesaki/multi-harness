"""ローカル UI の HTTP 契約の試験。**本物の SQLite と本物の CAS を使う。**

Service も Repository も Masking Pipeline も本物である。偽物にするのは時刻と
ID だけで、そこは決定論のために固定する。

## 何を測っているか

* 会話の作成・一覧・詳細が動くこと
* Message が **Masking Gate と CAS を通って**保存され、再起動後も読めること
* Snapshot と Context Preview が出せること
* Provider 状態が読み取り専用で、未設定として出ること
* 送信が Fail-Closed で、**Assistant Message を作らない**こと
"""

from __future__ import annotations

import json
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from harness.domain._chat_context_policy_generated import PROVIDER_OUTPUT_ROLE
from harness.presentation.local_ui.api import LocalUiApi, Request
from harness.presentation.local_ui.composition import LocalUiServices, build_services
from harness.presentation.local_ui.providers import (
    ExecutionMode,
    evaluate_mock_turn,
    execution_mode_views,
)
from harness.presentation.local_ui.server import load_assets, new_session_token

REPO_ROOT = Path(__file__).resolve().parents[3]
ORIGIN = "http://127.0.0.1:65535"
TOKEN = "a" * 64

#: 決定論のために固定する時刻。Service は時刻を自分で引かない。
FIXED_NOW = "2026-08-31T00:00:00Z"


class _Clock:
    def now(self) -> str:
        return FIXED_NOW


class _Ids:
    """UUIDv4 の形を保ったまま、順番だけ決定論にする。"""

    def __init__(self) -> None:
        self._n = 0

    def new_id(self) -> str:
        self._n += 1
        raw = f"{self._n:032x}"
        return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


def _build(tmp_path: Path) -> LocalUiServices:
    return build_services(
        repo_root=REPO_ROOT,
        database_path=tmp_path / "harness.db",
        artifact_root=tmp_path / "cas",
        clock=_Clock(),
        id_source=_Ids(),
    )


@pytest.fixture
def services(tmp_path: Path) -> Iterator[LocalUiServices]:
    built = _build(tmp_path)
    yield built
    built.close()


@pytest.fixture
def api(services: LocalUiServices) -> LocalUiApi:
    return LocalUiApi(
        services, origin=ORIGIN, session_token=TOKEN, assets=load_assets(new_session_token())
    )


def _get(api: LocalUiApi, path: str) -> tuple[int, Any]:
    response = api.handle(Request(method="GET", path=path, headers={}))
    return response.status, json.loads(response.body)


def _post(
    api: LocalUiApi,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    origin: str = ORIGIN,
    token: str = TOKEN,
    content_type: str = "application/json",
    raw: bytes | None = None,
) -> tuple[int, Any]:
    payload = raw if raw is not None else json.dumps(body or {}).encode("utf-8")
    headers = {"origin": origin, "x-harness-session": token, "content-type": content_type}
    response = api.handle(Request(method="POST", path=path, headers=headers, body=payload))
    return response.status, json.loads(response.body)


def _new_conversation(api: LocalUiApi) -> str:
    status, payload = _post(api, "/api/conversations")
    assert status == 201, payload
    conversation_id: str = payload["conversation"]["conversation_id"]
    return conversation_id


# ---------------------------------------------------------------------------
# 会話
# ---------------------------------------------------------------------------


def test_conversation_can_be_created_and_listed(api: LocalUiApi) -> None:
    status, payload = _get(api, "/api/conversations")
    assert status == 200
    assert payload["conversations"] == []

    conversation_id = _new_conversation(api)
    status, payload = _get(api, "/api/conversations")
    assert status == 200
    rows = payload["conversations"]
    assert [row["conversation_id"] for row in rows] == [conversation_id]
    assert rows[0]["message_count"] == 0
    assert rows[0]["latest_snapshot"] is None
    assert rows[0]["hash_verified"] is True
    assert rows[0]["stored_locally"] is True


def test_display_name_is_derived_not_stored(api: LocalUiApi, services: LocalUiServices) -> None:
    """表示名は既存 Field から導く。**Store に列を増やさない。**"""
    conversation_id = _new_conversation(api)
    _, payload = _get(api, "/api/conversations")
    row = payload["conversations"][0]
    assert row["display_name"] == f"{FIXED_NOW} · {conversation_id[:8]}"

    columns = {
        str(entry[1])
        for entry in services.connection.execute("PRAGMA table_info(conversation)").fetchall()
    }
    assert "display_name" not in columns
    assert "title" not in columns


def test_unknown_conversation_is_404(api: LocalUiApi) -> None:
    missing = "00000000-0000-0000-0000-0000000000ff"
    status, payload = _get(api, f"/api/conversations/{missing}")
    assert status == 404, payload


def test_conversation_id_shape_is_checked(api: LocalUiApi) -> None:
    status, _ = _get(api, "/api/conversations/not-a-uuid")
    assert status == 400


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------


def test_message_is_stored_through_cas_and_survives_restart(
    tmp_path: Path, api: LocalUiApi, services: LocalUiServices
) -> None:
    """本文は CAS へ置かれ、Record は Hash だけを持つ。再起動後も読める。"""
    conversation_id = _new_conversation(api)
    body = {"role": "USER_TASK", "text": "議事録をまとめてほしい"}
    status, payload = _post(api, f"/api/conversations/{conversation_id}/messages", body)
    assert status == 201, payload
    message = payload["message"]
    assert message["sequence_number"] == 1
    assert message["body"] == body["text"]
    assert message["content_artifact_hash"].startswith("sha256:")

    # SQLite に本文が入っていないこと。**Record は Hash だけを持つ。**
    rows = services.connection.execute("SELECT * FROM conversation_message").fetchall()
    assert len(rows) == 1
    stored = " ".join(str(value) for value in tuple(rows[0]))
    assert body["text"] not in stored

    services.close()
    reopened = _build(tmp_path)
    try:
        restarted = LocalUiApi(
            reopened, origin=ORIGIN, session_token=TOKEN, assets=load_assets(new_session_token())
        )
        status, payload = _get(restarted, f"/api/conversations/{conversation_id}")
        assert status == 200
        assert [m["body"] for m in payload["messages"]] == [body["text"]]
    finally:
        reopened.close()


def test_sequence_numbers_are_assigned_by_the_service(api: LocalUiApi) -> None:
    conversation_id = _new_conversation(api)
    for index in range(3):
        status, payload = _post(
            api,
            f"/api/conversations/{conversation_id}/messages",
            {"role": "USER_TASK", "text": f"本文 {index}"},
        )
        assert status == 201
        assert payload["message"]["sequence_number"] == index + 1


def test_sequence_number_cannot_be_supplied_by_the_caller(api: LocalUiApi) -> None:
    conversation_id = _new_conversation(api)
    status, payload = _post(
        api,
        f"/api/conversations/{conversation_id}/messages",
        {"role": "USER_TASK", "text": "x", "sequence_number": 99},
    )
    assert status == 400
    assert "sequence_number" in payload["error"]["reason"]


def test_roles_come_from_the_existing_enum(api: LocalUiApi) -> None:
    """Role 名を UI 側で書かない。既存 Enum が正本である。"""
    from harness.domain.context_budget import MessageRole

    conversation_id = _new_conversation(api)
    _, payload = _get(api, f"/api/conversations/{conversation_id}")
    assert payload["roles"] == [role.value for role in MessageRole]


def test_unknown_role_is_rejected(api: LocalUiApi) -> None:
    conversation_id = _new_conversation(api)
    status, payload = _post(
        api,
        f"/api/conversations/{conversation_id}/messages",
        {"role": "ASSISTANT", "text": "x"},
    )
    assert status == 422, payload


def test_empty_text_is_rejected(api: LocalUiApi) -> None:
    conversation_id = _new_conversation(api)
    status, _ = _post(
        api, f"/api/conversations/{conversation_id}/messages", {"role": "USER_TASK", "text": "   "}
    )
    assert status == 422


# ---------------------------------------------------------------------------
# Snapshot と Context Preview
# ---------------------------------------------------------------------------


def _seed(api: LocalUiApi, count: int = 2) -> tuple[str, str]:
    conversation_id = _new_conversation(api)
    for index in range(count):
        status, _ = _post(
            api,
            f"/api/conversations/{conversation_id}/messages",
            {"role": "USER_TASK", "text": f"本文 {index} " + "あ" * index},
        )
        assert status == 201
    status, payload = _post(api, f"/api/conversations/{conversation_id}/snapshots")
    assert status == 201, payload
    return conversation_id, payload["snapshot"]["snapshot_id"]


def test_snapshot_is_displayed_with_its_bindings(api: LocalUiApi) -> None:
    conversation_id, snapshot_id = _seed(api)
    status, payload = _get(api, f"/api/conversations/{conversation_id}")
    assert status == 200
    snapshot = payload["snapshots"][0]
    assert snapshot["snapshot_id"] == snapshot_id
    for key in ("snapshot_hash", "message_set_hash", "schema_set_hash", "design_sha256"):
        assert snapshot[key].startswith("sha256:"), key
    assert snapshot["verified"] is True
    assert payload["design_sha256"] == snapshot["design_sha256"]


def _preview_body(snapshot_id: str, *, total: int = 4096) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot_id,
        "mandatory_message_ids": [],
        "budget": {
            "total_tokens": total,
            "reserved_output_tokens": 0,
            "reserved_tool_tokens": 0,
            "safety_margin_tokens": 0,
        },
        "profile": {
            "provider": "mock",
            "model": "local-preview",
            "context_limit": 8192,
            "maximum_output_limit": 1024,
            "expires_at": "2027-01-01T00:00:00Z",
            "overheads": {
                "system_message_overhead": 0,
                "developer_message_overhead": 0,
                "tool_definition_overhead": 0,
                "per_message_overhead": 0,
                "structured_output_overhead": 0,
                "streaming_frame_overhead": 0,
                "retry_fallback_reservation": 0,
            },
        },
    }


def test_context_preview_reports_selection_and_decision_hash(api: LocalUiApi) -> None:
    conversation_id, snapshot_id = _seed(api, count=3)
    status, payload = _post(
        api, f"/api/conversations/{conversation_id}/context-preview", _preview_body(snapshot_id)
    )
    assert status == 200, payload
    assert len(payload["selected_fragments"]) == 3
    assert payload["excluded_fragments"] == []
    assert payload["decision_hash"].startswith("sha256:")
    assert payload["bundle_hash"].startswith("sha256:")
    assert payload["budget"]["overflow_policy"] == "FAIL_CLOSED"
    assert payload["receipt_projection"]["tie_breaker"]
    # 送信順は sequence_number 昇順である。選択順ではない。
    order = {f["fragment_id"]: f["sequence_number"] for f in payload["selected_fragments"]}
    assert [order[fid] for fid in payload["send_order"]] == [1, 2, 3]


def test_context_preview_excludes_with_a_reason_when_the_budget_is_small(
    api: LocalUiApi,
) -> None:
    conversation_id, snapshot_id = _seed(api, count=3)
    status, payload = _post(
        api,
        f"/api/conversations/{conversation_id}/context-preview",
        _preview_body(snapshot_id, total=20),
    )
    assert status in {200, 422}, payload
    if status == 200:
        # 除外には必ず理由が付く。理由の無い除外を作らない。
        for item in payload["excluded_fragments"]:
            assert item["reason"] in {"BUDGET", "DUPLICATE"}


def test_context_preview_requires_every_budget_field(api: LocalUiApi) -> None:
    """既定値で補わない。**足りなければ止まる。**"""
    conversation_id, snapshot_id = _seed(api)
    body = _preview_body(snapshot_id)
    del body["budget"]["safety_margin_tokens"]
    status, payload = _post(api, f"/api/conversations/{conversation_id}/context-preview", body)
    assert status == 400
    assert "safety_margin_tokens" in payload["error"]["reason"]


def test_context_preview_rejects_an_unknown_snapshot(api: LocalUiApi) -> None:
    conversation_id, _ = _seed(api)
    body = _preview_body("00000000-0000-0000-0000-0000000000ff")
    status, _ = _post(api, f"/api/conversations/{conversation_id}/context-preview", body)
    assert status == 404


def test_context_preview_uses_the_real_counter_identity(api: LocalUiApi) -> None:
    """Profile が名乗る計数器は、実際に数えた側と同じであること。"""
    from harness.infrastructure.tokenizer.deterministic_counter import (
        BYTE_BOUND_TOKENIZER_NAME,
        COUNTING_ADAPTER_VERSION,
    )

    conversation_id, snapshot_id = _seed(api)
    status, payload = _post(
        api, f"/api/conversations/{conversation_id}/context-preview", _preview_body(snapshot_id)
    )
    assert status == 200
    profile = payload["token_profile"]
    assert profile["tokenizer_name"] == BYTE_BOUND_TOKENIZER_NAME
    assert profile["counting_adapter_version"] == COUNTING_ADAPTER_VERSION
    assert profile["estimate_assurance"] == "CONSERVATIVE"


def test_mandatory_is_refused_for_untrusted_roles(api: LocalUiApi) -> None:
    """Chat は Message を必須にできない。**UI で規則を作り直さない。**"""
    conversation_id, snapshot_id = _seed(api)
    _, detail = _get(api, f"/api/conversations/{conversation_id}")
    body = _preview_body(snapshot_id)
    body["mandatory_message_ids"] = [detail["messages"][0]["message_id"]]
    status, payload = _post(api, f"/api/conversations/{conversation_id}/context-preview", body)
    assert status == 422, payload
    assert payload["error"]["code"] in {
        "SCHEMA_CONDITIONAL_VIOLATION",
        "CONTROL_DATA_ROLE_ESCALATION",
    }


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


def test_provider_status_is_read_only_and_unconfigured(api: LocalUiApi) -> None:
    status, payload = _get(api, "/api/providers")
    assert status == 200
    assert payload["any_send_allowed"] is False
    assert payload["external_provider_count"] == 0
    assert payload["value_fields_entered"] == 0
    assert payload["blocking_reasons"]
    for provider in payload["providers"]:
        assert provider["chat_send_allowed"] is False
        assert provider["api_status"] in {"AVAILABLE", "NOT_CONFIGURED", "NOT_IMPLEMENTED"}
        assert provider["endpoint_configured"] is False
        assert provider["model_configured"] is False
        assert provider["secret_ref_configured"] is False


def test_provider_endpoint_has_no_write_method(api: LocalUiApi) -> None:
    status, _ = _post(api, "/api/providers", {})
    assert status == 404


def test_provider_response_carries_no_secret(api: LocalUiApi) -> None:
    response = api.handle(Request(method="GET", path="/api/providers", headers={}))
    text = response.body.decode("utf-8").lower()
    for word in ("api_key", "apikey", "secret_value", "token_value", "keyring_value", "password"):
        assert word not in text, word


@pytest.fixture
def synthetic_value_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provider 具体値の Package を、正規の生成器で組んだ合成の写しへ向ける。

    保存 Package（Owner の回答記録）は公開用の配布コピーに無い。在る木でも開かない。
    """
    from synthetic_value_package import build_value_chain

    from harness.presentation.local_ui import providers

    chain = build_value_chain(tmp_path / "value-chain")
    monkeypatch.setattr(providers, "VALUE_PACKAGE", str(chain.package))
    return chain.package


def test_provider_list_matches_the_registry(api: LocalUiApi, synthetic_value_package: Path) -> None:
    """画面に出す Provider は正本に載っているものだけであること。"""
    import yaml

    route = yaml.safe_load(
        (REPO_ROOT / "design-source/registries/route-policy.yaml").read_text(encoding="utf-8")
    )
    _, payload = _get(api, "/api/providers")
    assert [p["provider_id"] for p in payload["providers"]] == [
        entry["id"] for entry in route["providers"]
    ]
    assert payload["value_fields_entered"] == 0


def test_without_a_value_package_no_provider_is_listed(
    api: LocalUiApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回答 Package が無い初期状態では Provider を出さず、未設定の理由を示すこと。"""
    from harness.presentation.local_ui import providers

    monkeypatch.setattr(providers, "VALUE_PACKAGE", str(tmp_path / "absent.json"))
    _, payload = _get(api, "/api/providers")
    assert payload["providers"] == []
    assert payload["value_package_status"] == "NOT_CONFIGURED"
    assert payload["any_send_allowed"] is False
    assert payload["blocking_reasons"]


# ==========================================================================
# Mock の 1 往復
# ==========================================================================


#: 呼出側が Metadata を送れないことを確かめるための値（Owner Decision `MTM-6-B`）。
#:
#: **この値は通らない。** 送っても拒まれることを測るためだけに持つ。
INJECTED_METADATA = {
    "model_id": "injected-model",
    "instruction_hash": "sha256:" + "1" * 64,
    "output_schema_hash": "sha256:" + "2" * 64,
}

BUDGET = {
    "total_tokens": 4096,
    "reserved_output_tokens": 256,
    "reserved_tool_tokens": 0,
    "safety_margin_tokens": 16,
}

PROFILE = {
    "provider": "profile-under-test",
    "model": "model-under-test",
    "context_limit": 8192,
    "maximum_output_limit": 1024,
    "expires_at": "2026-12-31T00:00:00Z",
    "overheads": {
        "system_message_overhead": 0,
        "developer_message_overhead": 0,
        "tool_definition_overhead": 0,
        "per_message_overhead": 0,
        "structured_output_overhead": 0,
        "streaming_frame_overhead": 0,
        "retry_fallback_reservation": 0,
    },
}


def _turn_body(conversation_id: str, text: str = "こんにちは", **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "conversation_id": conversation_id,
        "provider_id": "mock",
        "execution_mode": "MOCK",
        "text": text,
        "mandatory_message_ids": [],
        "budget": dict(BUDGET),
        "profile": dict(PROFILE),
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# 1〜4: 1 往復と履歴
# ---------------------------------------------------------------------------


def test_a_mock_turn_stores_both_messages(api: LocalUiApi) -> None:
    """UI から 1 往復が実行でき、両方の Message が保存されること。"""
    conversation_id = _new_conversation(api)
    status, payload = _post(api, "/api/chat/mock-turn", _turn_body(conversation_id))
    assert status == 201, payload

    assert payload["decision"]["allowed"] is True
    assert payload["execution_mode"] == "MOCK"
    assert payload["network_used"] is False
    assert payload["external_send_performed"] is False
    assert payload["provider_response"]["network_used"] is False
    assert payload["provider_response"]["provider_id"] == "mock"
    assert payload["provider_response"]["billing_mode"] == "FREE"

    assert payload["user_message"]["role"] == "USER_TASK"
    assert payload["provider_message"]["role"] == PROVIDER_OUTPUT_ROLE
    assert payload["user_message"]["sequence_number"] == 1
    assert payload["provider_message"]["sequence_number"] == 2


def test_the_history_holds_both_messages_after_reload(
    api: LocalUiApi, services: LocalUiServices, tmp_path: Path
) -> None:
    """再読み込みしても履歴が一致すること。**別 Process の読み直しに近い形で見る。**"""
    conversation_id = _new_conversation(api)
    status, sent = _post(api, "/api/chat/mock-turn", _turn_body(conversation_id))
    assert status == 201, sent

    status, detail = _get(api, f"/api/conversations/{conversation_id}")
    assert status == 200
    roles = [row["role"] for row in detail["messages"]]
    assert roles == ["USER_TASK", PROVIDER_OUTPUT_ROLE]

    services.close()
    reopened = _build(tmp_path)
    try:
        again = LocalUiApi(
            reopened,
            origin=ORIGIN,
            session_token=TOKEN,
            assets=load_assets(new_session_token()),
        )
        status, reloaded = _get(again, f"/api/conversations/{conversation_id}")
        assert status == 200
        assert reloaded["messages"] == detail["messages"]
        assert (
            reloaded["conversation"]["conversation_hash"]
            == (detail["conversation"]["conversation_hash"])
        )
    finally:
        reopened.close()


def test_the_snapshot_and_hashes_are_reproducible(api: LocalUiApi) -> None:
    """Snapshot と Hash が再現できること。**検証が真であること。**"""
    conversation_id = _new_conversation(api)
    status, payload = _post(api, "/api/chat/mock-turn", _turn_body(conversation_id))
    assert status == 201, payload
    snapshot = payload["snapshot"]
    assert snapshot["verified"] is True

    status, detail = _get(api, f"/api/conversations/{conversation_id}")
    assert status == 200
    assert detail["conversation"]["hash_verified"] is True
    stored = {row["snapshot_id"]: row for row in detail["snapshots"]}
    assert stored[snapshot["snapshot_id"]]["snapshot_hash"] == snapshot["snapshot_hash"]
    assert stored[snapshot["snapshot_id"]]["message_set_hash"] == snapshot["message_set_hash"]


def test_two_turns_extend_the_same_conversation(api: LocalUiApi) -> None:
    """2 往復目も同じ会話へ積まれること。**上書きしない。**"""
    conversation_id = _new_conversation(api)
    for _ in range(2):
        status, payload = _post(api, "/api/chat/mock-turn", _turn_body(conversation_id))
        assert status == 201, payload

    status, detail = _get(api, f"/api/conversations/{conversation_id}")
    assert status == 200
    assert [row["sequence_number"] for row in detail["messages"]] == [1, 2, 3, 4]
    assert [row["role"] for row in detail["messages"]] == [
        "USER_TASK",
        PROVIDER_OUTPUT_ROLE,
        "USER_TASK",
        PROVIDER_OUTPUT_ROLE,
    ]


# ---------------------------------------------------------------------------
# 5〜7: Fail-Closed
# ---------------------------------------------------------------------------


def test_a_budget_overflow_fails_closed(api: LocalUiApi, services: LocalUiServices) -> None:
    """Budget を超えたら止まること。**Provider 出力を保存しない。**"""
    conversation_id = _new_conversation(api)
    tiny = {
        "total_tokens": 1,
        "reserved_output_tokens": 0,
        "reserved_tool_tokens": 0,
        "safety_margin_tokens": 0,
    }
    status, payload = _post(
        api,
        "/api/chat/mock-turn",
        _turn_body(conversation_id, text="あ" * 200, budget=tiny),
    )
    assert status in {409, 422}, payload

    rows = services.connection.execute(
        "SELECT role FROM conversation_message ORDER BY sequence_number"
    ).fetchall()
    assert [row[0] for row in rows] == ["USER_TASK"], "Provider 出力が保存された"


def test_a_turn_without_a_token_is_refused(api: LocalUiApi, services: LocalUiServices) -> None:
    """Token 無し POST が 403 になること。"""
    conversation_id = _new_conversation(api)
    status, payload = _post(api, "/api/chat/mock-turn", _turn_body(conversation_id), token="")
    assert status == 403, payload
    rows = services.connection.execute("SELECT COUNT(*) FROM conversation_message").fetchone()
    assert rows[0] == 0


@pytest.mark.parametrize(
    ("overrides", "reason_fragment"),
    [
        ({"execution_mode": "CLI"}, "Adapter が未実装"),
        ({"execution_mode": "API"}, "Adapter が未実装"),
        ({"provider_id": "openai"}, "providers に無い"),
        ({"provider_id": "unknown-provider"}, "providers に無い"),
    ],
    ids=["cli", "api", "unregistered-openai", "unregistered-other"],
)
def test_an_unconfigured_provider_or_external_mode_is_refused(
    api: LocalUiApi,
    services: LocalUiServices,
    overrides: dict[str, Any],
    reason_fragment: str,
) -> None:
    """未設定 Provider と外部実行方式を拒むこと。**Message を 1 件も作らない。**"""
    conversation_id = _new_conversation(api)
    status, payload = _post(api, "/api/chat/mock-turn", _turn_body(conversation_id, **overrides))
    assert status == 409, payload
    assert payload["decision"]["allowed"] is False
    assert any(reason_fragment in reason for reason in payload["decision"]["reasons"]), payload
    assert payload["network_used"] is False
    assert payload["assistant_message_created"] is False

    rows = services.connection.execute("SELECT COUNT(*) FROM conversation_message").fetchone()
    assert rows[0] == 0, "拒んだのに Message が保存された"


# ---------------------------------------------------------------------------
# 8〜11: 外へ出ていないこと
# ---------------------------------------------------------------------------


def test_a_turn_opens_no_socket_and_launches_no_process(
    api: LocalUiApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Network・subprocess が 0 回であること。**実際に数える。**"""
    opened: list[Any] = []
    spawned: list[Any] = []
    original_connect = socket.socket.connect
    original_popen = subprocess.Popen.__init__

    def _record_connect(self: Any, address: Any) -> Any:  # pragma: no cover - 呼ばれたら失敗
        opened.append(address)
        return original_connect(self, address)

    def _record_popen(self: Any, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        spawned.append(args)
        return original_popen(self, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", _record_connect)
    monkeypatch.setattr(subprocess.Popen, "__init__", _record_popen)

    conversation_id = _new_conversation(api)
    status, payload = _post(api, "/api/chat/mock-turn", _turn_body(conversation_id))
    assert status == 201, payload
    assert opened == [], f"Network 通信が発生した: {opened}"
    assert spawned == [], f"subprocess が起動した: {spawned}"


def test_the_turn_path_reaches_no_credential_store() -> None:
    """Keyring へ届く道具を持たないこと。**アクセス回数は 0 である。**"""
    ui_dir = Path(__file__).resolve().parents[3] / "src/harness/presentation/local_ui"
    forbidden = ("keyring", "secretstorage", "http.cookiejar", "webbrowser", "requests", "httpx")
    for path in sorted(ui_dir.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for name in forbidden:
            assert f"import {name}" not in source, f"{path.name} が {name} を import している"


def test_the_external_send_path_is_still_refused(api: LocalUiApi) -> None:
    """`/api/chat/send` はいまも拒むこと。**Mock を足しても開かない。**"""
    conversation_id = _new_conversation(api)
    status, payload = _post(api, "/api/chat/send", {"conversation_id": conversation_id})
    assert status == 409, payload
    assert payload["network_used"] is False
    assert payload["assistant_message_created"] is False


# ---------------------------------------------------------------------------
# 12〜13: 表示と補完
# ---------------------------------------------------------------------------


def test_the_display_does_not_change_the_decision(tmp_path: Path) -> None:
    """画面表示を変えても判定が変わらないこと。

    **判定は Registry だけから作る。** 画面資産を差し替えても、同じ入力に対して
    同じ判定が返る。
    """
    before = evaluate_mock_turn(REPO_ROOT, mode=ExecutionMode.MOCK, provider_id="mock")
    services = _build(tmp_path)
    try:
        assets = load_assets(new_session_token())
        rewritten = {
            path: (body.replace(b"Mock", b"READY TO SEND"), content_type)
            for path, (body, content_type) in assets.items()
        }
        api = LocalUiApi(services, origin=ORIGIN, session_token=TOKEN, assets=rewritten)
        status, payload = _get(api, "/api/execution-modes")
        assert status == 200
        assert [mode["mode"] for mode in payload["modes"]] == ["MOCK", "CLI", "API"]
        after = evaluate_mock_turn(REPO_ROOT, mode=ExecutionMode.MOCK, provider_id="mock")
        assert after == before
    finally:
        services.close()


def test_metadata_cannot_be_injected_by_the_caller(
    api: LocalUiApi, services: LocalUiServices
) -> None:
    """呼出側が Metadata を注入できないこと（`MTM-6-B`）。

    **入力欄そのものが無い。** 送れば知らない Field として拒まれる。
    """
    conversation_id = _new_conversation(api)
    status, payload = _post(
        api, "/api/chat/mock-turn", _turn_body(conversation_id, request=dict(INJECTED_METADATA))
    )
    assert status in {400, 422}, payload
    rows = services.connection.execute("SELECT COUNT(*) FROM conversation_message").fetchone()
    assert rows[0] == 0, "拒んだのに Message が保存された"


@pytest.mark.parametrize("name", sorted(INJECTED_METADATA), ids=sorted(INJECTED_METADATA))
def test_each_metadata_field_is_rejected_at_the_top_level(
    api: LocalUiApi, services: LocalUiServices, name: str
) -> None:
    """Metadata を直接 Body へ書いても拒まれること。"""
    conversation_id = _new_conversation(api)
    status, payload = _post(
        api, "/api/chat/mock-turn", _turn_body(conversation_id, **{name: INJECTED_METADATA[name]})
    )
    assert status in {400, 422}, payload
    rows = services.connection.execute("SELECT COUNT(*) FROM conversation_message").fetchone()
    assert rows[0] == 0


def test_the_metadata_is_derived_and_says_where_from(api: LocalUiApi) -> None:
    """3 項目が導出され、出所を名乗ること（`MTM-1-A`／`MTM-3-A`／`MTM-4-A`）。"""
    conversation_id = _new_conversation(api)
    status, payload = _post(api, "/api/chat/mock-turn", _turn_body(conversation_id))
    assert status == 201, payload
    derived = payload["derived_metadata"]
    assert derived["caller_supplied_metadata"] == []
    # `instruction_hash` は最終送信 payload から引く。
    # **`bundle_hash` とは別物である。** あれは内部 ID を含み、並びも選択順である。
    assert derived["instruction_hash"] != payload["context"]["bundle_hash"]
    assert derived["instruction_hash_source"] == "FINAL_SEND_PAYLOAD_AFTER_SELECTION"
    # Mock は Schema 検証を通していないので、指す Schema が無い。
    assert derived["output_schema_hash"] is None
    assert derived["output_schema_source"] == "NOT_USED_BY_THE_MOCK_ROUTE"
    # Model は Profile だけから引く。
    assert derived["model_id"] == PROFILE["model"]
    assert derived["model_id_source"] == "TOKEN_PROFILE_SNAPSHOT"
    assert payload["provider_response"]["model_id"] == PROFILE["model"]


def test_the_hash_target_is_the_payload_after_selection(api: LocalUiApi) -> None:
    """Hash 対象が Context 選択の後であること（`MTM-2-B`）。

    **本文が変われば最終 payload が変わり、Hash も変わる。**
    """
    first = _new_conversation(api)
    status, one = _post(api, "/api/chat/mock-turn", _turn_body(first, text="ひとつめ"))
    assert status == 201, one
    second = _new_conversation(api)
    status, two = _post(api, "/api/chat/mock-turn", _turn_body(second, text="ふたつめ"))
    assert status == 201, two
    assert (
        one["derived_metadata"]["instruction_hash"] != (two["derived_metadata"]["instruction_hash"])
    )


def test_the_selection_is_what_moves_the_hash(api: LocalUiApi) -> None:
    """選択が変われば Hash も変わること（`MTM-2-B`）。

    2 往復目は 1 往復目の Message も候補になるので、選ばれる集合が変わる。
    **最終 payload が変われば Hash も変わる。**
    """
    conversation_id = _new_conversation(api)
    status, one = _post(api, "/api/chat/mock-turn", _turn_body(conversation_id, text="おなじ本文"))
    assert status == 201, one
    status, two = _post(api, "/api/chat/mock-turn", _turn_body(conversation_id, text="おなじ本文"))
    assert status == 201, two
    assert one["context"]["selected_fragment_ids"] != two["context"]["selected_fragment_ids"]
    assert (
        one["derived_metadata"]["instruction_hash"] != two["derived_metadata"]["instruction_hash"]
    )


def test_the_same_final_payload_gives_the_same_hash(api: LocalUiApi) -> None:
    """同じ最終 payload を新規呼出で 2 回処理したら Hash が一致すること。

    **これが直したことである。** 呼出のたびに Snapshot ID も Policy ID も
    Bundle ID も変わるが、送るものが同じなら値は同じになる。
    """
    first = _new_conversation(api)
    status, one = _post(api, "/api/chat/mock-turn", _turn_body(first, text="おなじ本文"))
    assert status == 201, one
    second = _new_conversation(api)
    status, two = _post(api, "/api/chat/mock-turn", _turn_body(second, text="おなじ本文"))
    assert status == 201, two

    assert (
        one["derived_metadata"]["instruction_hash"] == two["derived_metadata"]["instruction_hash"]
    )
    # **内部 ID は実際に違っている。** 違っていなければ何も測っていない。
    assert one["snapshot"]["snapshot_id"] != two["snapshot"]["snapshot_id"]
    assert one["context"]["bundle_hash"] != two["context"]["bundle_hash"]


def test_only_the_snapshot_id_moving_does_not_move_the_hash(api: LocalUiApi) -> None:
    """Snapshot ID だけ変わっても Hash が一致すること。

    Snapshot は往復ごとに新しく作られる。**その ID は payload ではない。**
    """
    first = _new_conversation(api)
    status, one = _post(api, "/api/chat/mock-turn", _turn_body(first, text="同一 payload"))
    assert status == 201, one
    second = _new_conversation(api)
    status, two = _post(api, "/api/chat/mock-turn", _turn_body(second, text="同一 payload"))
    assert status == 201, two

    assert one["snapshot"]["snapshot_id"] != two["snapshot"]["snapshot_id"]
    assert one["snapshot"]["snapshot_hash"] != two["snapshot"]["snapshot_hash"]
    assert (
        one["derived_metadata"]["instruction_hash"] == two["derived_metadata"]["instruction_hash"]
    )


def test_only_the_policy_id_moving_does_not_move_the_hash(api: LocalUiApi) -> None:
    """Policy ID だけ変わっても Hash が一致すること。

    Policy ID は要求ごとに採番される。**Budget の中身が同じなら payload は同じ。**
    """
    first = _new_conversation(api)
    status, one = _post(api, "/api/chat/mock-turn", _turn_body(first, text="同じ予算"))
    assert status == 201, one
    second = _new_conversation(api)
    status, two = _post(api, "/api/chat/mock-turn", _turn_body(second, text="同じ予算"))
    assert status == 201, two
    # Budget の中身は同じで、Policy ID だけが違う。Bundle Hash はそれで動く。
    assert one["context"]["bundle_hash"] != two["context"]["bundle_hash"]
    assert (
        one["derived_metadata"]["instruction_hash"] == two["derived_metadata"]["instruction_hash"]
    )


def test_the_role_and_the_order_are_part_of_the_hash() -> None:
    """role と順序が Hash の一部であること。

    **本文が同じでも、role が違えば別の payload である。** 並べ替えても別である。
    """
    from harness.domain.context_budget import ContextFragment, MessageRole, final_payload_hash
    from harness.domain.hashing import hash_bytes

    def fragment(fragment_id: str, body: bytes, role: MessageRole) -> ContextFragment:
        return ContextFragment(
            fragment_id=fragment_id,
            fragment_content_hash=hash_bytes(body),
            token_count=1,
            message_role=role,
            control_authority=False,
            instruction_eligible=False,
            mandatory=False,
            priority=1,
        )

    one = fragment("a", b"one", MessageRole.USER_TASK)
    two = fragment("b", b"two", MessageRole.USER_TASK)
    recast = fragment("a", b"one", MessageRole.UNTRUSTED_PROVIDER_DATA)

    base = final_payload_hash([one, two])
    assert base == final_payload_hash([one, two]), "同じ入力で値が動いている"
    assert base != final_payload_hash([two, one]), "順序を変えても値が同じ"
    assert base != final_payload_hash([recast, two]), "role を変えても値が同じ"
    assert base != final_payload_hash([one]), "本文を減らしても値が同じ"


def test_internal_ids_are_not_in_the_hash_input() -> None:
    """内部 ID が Hash 入力へ入っていないこと。

    **入っていないことを、射影を見て確かめる。** 入れば同じ payload でも値が動く。
    """
    from harness.domain.context_budget import (
        ContextFragment,
        MessageRole,
        final_payload_projection,
    )
    from harness.domain.hashing import hash_bytes

    fragment = ContextFragment(
        fragment_id="internal-fragment-id",
        fragment_content_hash=hash_bytes(b"body"),
        token_count=7,
        message_role=MessageRole.USER_TASK,
        control_authority=False,
        instruction_eligible=False,
        mandatory=False,
        priority=1,
        source_artifact_id="internal-artifact-id",
    )
    rendered = json.dumps(final_payload_projection([fragment]), ensure_ascii=False)
    for banned in ("internal-fragment-id", "internal-artifact-id", "token_count", "mandatory"):
        assert banned not in rendered, f"payload でないものが入っている: {banned}"
    assert "message_role" in rendered
    assert "content_hash" in rendered


def test_the_hash_follows_the_send_order_not_the_selection_order(api: LocalUiApi) -> None:
    """送信順で Hash を取っていること。

    **選択順は新しい順、送信順は会話の順である。** 逆に取ると、同じ集合でも
    会話が逆さの payload を表す値になる。外から組み直して突き合わせる。
    """
    from harness.domain.context_budget import ContextFragment, MessageRole, final_payload_hash
    from harness.domain.hashing import ContentHash

    conversation_id = _new_conversation(api)
    # 2 往復して、選択順と送信順が別物になる長さにする。
    status, first = _post(api, "/api/chat/mock-turn", _turn_body(conversation_id, text="いち"))
    assert status == 201, first
    status, payload = _post(api, "/api/chat/mock-turn", _turn_body(conversation_id, text="に"))
    assert status == 201, payload

    send_order = payload["context"]["send_order"]
    selected = payload["context"]["selected_fragment_ids"]
    assert len(send_order) > 1, "1 件では順序を測れない"
    assert list(send_order) != list(selected), "選択順と送信順が同じでは測れない"

    status, detail = _get(api, f"/api/conversations/{conversation_id}")
    assert status == 200
    rows = {row["message_id"]: row for row in detail["messages"]}

    def fragment(message_id: str) -> ContextFragment:
        row = rows[message_id]
        return ContextFragment(
            fragment_id=message_id,
            fragment_content_hash=ContentHash.parse(row["content_artifact_hash"]),
            token_count=1,
            message_role=MessageRole(row["role"]),
            control_authority=False,
            instruction_eligible=False,
            mandatory=False,
            priority=1,
        )

    expected = final_payload_hash([fragment(message_id) for message_id in send_order])
    reversed_expected = final_payload_hash([fragment(message_id) for message_id in selected])
    assert payload["derived_metadata"]["instruction_hash"] == str(expected)
    assert payload["derived_metadata"]["instruction_hash"] != str(reversed_expected)


def test_a_candidate_that_never_ships_does_not_move_the_hash(api: LocalUiApi) -> None:
    """選択前の候補が違っても、最終 payload が同じなら Hash が同じこと。

    片方の会話には、予算に収まらない古い Message を先に入れておく。**それは
    選ばれないので送られない。** 送られないものが Hash を動かしてはいけない。
    """
    tight = {
        "total_tokens": 64,
        "reserved_output_tokens": 0,
        "reserved_tool_tokens": 0,
        "safety_margin_tokens": 0,
    }

    plain = _new_conversation(api)
    status, one = _post(
        api, "/api/chat/mock-turn", _turn_body(plain, text="こんにちは", budget=tight)
    )
    assert status == 201, one

    crowded = _new_conversation(api)
    status, added = _post(
        api,
        f"/api/conversations/{crowded}/messages",
        {"role": "USER_TASK", "text": "あ" * 200},
    )
    assert status == 201, added
    status, two = _post(
        api, "/api/chat/mock-turn", _turn_body(crowded, text="こんにちは", budget=tight)
    )
    assert status == 201, two

    # 候補の数は違う。**選ばれたものは同じ。**
    assert len(one["context"]["selected_fragment_ids"]) == 1
    assert len(two["context"]["selected_fragment_ids"]) == 1
    assert two["context"]["excluded_fragments"], "古い Message が候補に残っていない"
    assert (
        one["derived_metadata"]["instruction_hash"] == two["derived_metadata"]["instruction_hash"]
    )


def test_a_profile_without_a_model_stops_the_turn(
    api: LocalUiApi, services: LocalUiServices
) -> None:
    """Profile に Model が無ければ止まること（`MTM-4-A`）。**既定値を入れない。**"""
    conversation_id = _new_conversation(api)
    profile = dict(PROFILE)
    profile["model"] = ""
    status, payload = _post(
        api, "/api/chat/mock-turn", _turn_body(conversation_id, profile=profile)
    )
    assert status in {400, 422}, payload
    rows = services.connection.execute("SELECT COUNT(*) FROM conversation_message").fetchone()
    assert rows[0] == 0


def test_a_schema_outside_the_catalog_is_refused() -> None:
    """Catalog に無い Schema を拒むこと（`MTM-3-A`）。**既定を発明しない。**"""
    from harness.domain.errors import HarnessError
    from harness.infrastructure.schema.registry import CoreSchemaRegistry
    from harness.presentation.local_ui.composition import output_schema_hash

    catalog = CoreSchemaRegistry.bundled()
    assert output_schema_hash(catalog, schema_name=None, schema_version=None) is None

    real = catalog.refs[0]
    derived = output_schema_hash(
        catalog, schema_name=real.schema_name, schema_version=real.schema_version
    )
    assert derived == real.content_hash, "Catalog の Hash と違うものを作っている"

    for name, version in (
        ("NoSuchSchema", "1.0.0"),
        (real.schema_name, "99.0.0"),
        (real.schema_name, None),
        (None, "1.0.0"),
    ):
        with pytest.raises(HarnessError):
            output_schema_hash(catalog, schema_name=name, schema_version=version)


def test_the_provider_request_matches_the_schema_optionality() -> None:
    """Port の必須性が正本 Schema へ揃っていること（`MTM-5-A`）。

    `InvocationManifest@1.0.0` は `instruction_hash` と `output_schema_hash` を
    `required` に入れていない。**Port だけ必須にしない。** 2 つを渡さずに
    要求を組めることで測る。
    """
    import json as _json

    from harness.domain.hashing import hash_bytes
    from harness.ports.provider import ProviderRequest

    schema = _json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "schemas/core/InvocationManifest/1.0.0.schema.json"
        ).read_text(encoding="utf-8")
    )
    for name in ("instruction_hash", "output_schema_hash"):
        assert name in schema["properties"], f"{name} が Schema に無い"
        assert name not in schema["required"], f"{name} は Schema で必須である"

    request = ProviderRequest(
        provider_id="mock",
        model_id="model-under-test",
        context_bundle_hash=hash_bytes(b"bundle"),
        input_artifact_hash=hash_bytes(b"input"),
    )
    assert request.instruction_hash is None
    assert request.output_schema_hash is None


def test_the_ui_has_no_metadata_input_field() -> None:
    """画面に Metadata の入力欄が無いこと（`MTM-6-B`）。"""
    static = Path(__file__).resolve().parents[3] / "src/harness/presentation/local_ui/static"
    html = (static / "index.html").read_text(encoding="utf-8")
    for removed in ("turn-model", "turn-instruction", "turn-output-schema"):
        assert removed not in html, f"入力欄が残っている: {removed}"
    app = (static / "app.js").read_text(encoding="utf-8")
    for removed in ("turn-model", "turn-instruction", "turn-output-schema"):
        assert removed not in app, f"入力欄を読んでいる: {removed}"


def test_the_execution_modes_do_not_claim_more_than_they_do() -> None:
    """実行方式の一覧が、実装していないものを実装済みと言わないこと。"""
    modes = {view["mode"]: view for view in execution_mode_views(REPO_ROOT)}
    assert modes["MOCK"]["implemented"] is True
    assert modes["MOCK"]["external_egress"] is False
    for name in ("CLI", "API"):
        assert modes[name]["implemented"] is False
        assert modes[name]["blocking"], f"{name} の不足が空である"
