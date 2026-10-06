"""ローカル UI の安全側の試験。**通ることではなく通らないことを測る。**

## 何を測っているか

* `127.0.0.1` だけに Bind し、外部 Interface へ開かないこと
* Masking Gate を迂回する保存経路が無いこと
* CAS を迂回した保存が無く、SQLite に本文が入らないこと
* Provider 未設定で Network が 0 回、Assistant 応答が作られないこと
* 偽の Provider 応答を受け取らないこと
* `innerHTML`・外部 URL・CDN が UI 資産に無いこと
* Path Traversal・Body 超過・不正 Origin・不正 Session・重複 sequence を拒むこと
* CAS 欠落を空本文へ読み替えないこと
"""

from __future__ import annotations

import ast
import json
import re
import socket
import sqlite3
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from harness.presentation.local_ui.api import LocalUiApi, Request
from harness.presentation.local_ui.composition import LocalUiServices, build_services
from harness.presentation.local_ui.server import (
    ASSET_TABLE,
    BIND_HOST,
    load_assets,
    new_session_token,
    start_server,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
UI_DIR = REPO_ROOT / "src/harness/presentation/local_ui"
STATIC_DIR = UI_DIR / "static"
ORIGIN = "http://127.0.0.1:65535"
TOKEN = "b" * 64

#: 本文に混ぜる、確実に Reject 分類へ当たる文字列。
SECRET_BODY = "key is sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"


class _Clock:
    def now(self) -> str:
        return "2026-08-31T00:00:00Z"


class _Ids:
    def __init__(self) -> None:
        self._n = 0

    def new_id(self) -> str:
        self._n += 1
        raw = f"{self._n:032x}"
        return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


@pytest.fixture
def services(tmp_path: Path) -> Iterator[LocalUiServices]:
    built = build_services(
        repo_root=REPO_ROOT,
        database_path=tmp_path / "harness.db",
        artifact_root=tmp_path / "cas",
        clock=_Clock(),
        id_source=_Ids(),
    )
    yield built
    built.close()


@pytest.fixture
def api(services: LocalUiServices) -> LocalUiApi:
    return LocalUiApi(
        services, origin=ORIGIN, session_token=TOKEN, assets=load_assets(new_session_token())
    )


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
# Bind
# ---------------------------------------------------------------------------


def test_server_binds_only_to_loopback(services: LocalUiServices) -> None:
    """開くのは `127.0.0.1` だけである。**外へは開かない。**"""
    server = start_server(
        build_api=lambda origin, token, assets: LocalUiApi(
            services, origin=origin, session_token=token, assets=assets
        ),
        port=0,
        max_body_bytes=services.max_body_bytes,
    )
    # `shutdown()` は `serve_forever()` のループが止まるのを待つ。回していなければ
    # 永久に待つ。**先に回してから叩く。**
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.httpd.server_address[0], int(server.httpd.server_address[1])
        assert host == "127.0.0.1"
        assert server.url == f"http://127.0.0.1:{port}"
        # Ephemeral Port である。固定 Port を握らない。
        assert port > 0

        # S310: 宛先は直前に自分で組んだ `http://127.0.0.1:<port>` である。
        # 外から来た URL を開いていない。
        with urllib.request.urlopen(  # noqa: S310
            f"{server.url}/api/providers", timeout=5
        ) as response:
            assert response.status == 200
            payload = json.loads(response.read())
        assert payload["any_send_allowed"] is False

        # 外部 Interface からは繋がらない。**Bind していないので Refuse される。**
        outward_address = _outward_address()
        if outward_address is None:
            # Loopback しか持たない Host である。外から届く経路がそもそも無い。
            # **飛ばさずに、その事実を表明する。**
            assert _local_ipv4_addresses() == {"127.0.0.1"}, "外向き Address を見落とした"
        else:
            outward = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            outward.settimeout(1.5)
            try:
                with pytest.raises(OSError):
                    outward.connect((outward_address, port))
            finally:
                outward.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        assert not thread.is_alive(), "Server Thread が止まらなかった"


def _code_string_literals(path: Path) -> list[str]:
    """Code に現れる文字列 Literal。**docstring を除く。**

    文書が禁止対象を名指しするのは違反ではない。見たいのは実行される値である。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _without_comments(source: str, filename: str) -> str:
    """Comment を落とす。**「使わない」と書いた語を検出しないため。**"""
    if filename.endswith(".html"):
        return re.sub(r"<!--.*?-->", " ", source, flags=re.DOTALL)
    # Preserve quoted URL/comment markers; the old // regex truncated https literals.
    tokens = re.compile(
        "(\"(?:\\\\.|[^\"\\\\])*\"|'(?:\\\\.|[^'\\\\])*'|`(?:\\\\.|[^`\\\\])*`)|/\\*[\\s\\S]*?\\*/|//[^\\r\\n]*"
    )
    return tokens.sub(lambda match: match.group(1) or " ", source)


@pytest.mark.parametrize("quote", ['"', "'", chr(96)])
def test_comment_filter_keeps_url_and_literal_comment_markers(quote: str) -> None:
    url = "https://example.invalid/path"
    source = f"const url = {quote}{url}{quote}; // misleading https://other.invalid/\n"
    stripped = _without_comments(source, "app.js")
    assert url in stripped
    assert "https://other.invalid/" not in stripped
    literal = f"const text = {quote}/*literal*/{quote}; /*actual comment*/"
    stripped = _without_comments(literal, "app.js")
    assert "/*literal*/" in stripped
    assert "actual comment" not in stripped


def _local_ipv4_addresses() -> set[str]:
    """この Host が持つ IPv4 の集合。**通信しない。**

    UDP の `connect` は Packet を出さず、経路表から出口 Address を選ぶだけである。
    """
    found = {"127.0.0.1"}
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # RFC 5737 TEST-NET-1。到達しない
        found.add(str(probe.getsockname()[0]))
    except OSError:
        pass
    finally:
        probe.close()
    return found


def _outward_address() -> str | None:
    """Loopback 以外の IPv4。無ければ `None`。**Skip しない。**"""
    outward = sorted(a for a in _local_ipv4_addresses() if not a.startswith("127."))
    return outward[0] if outward else None


def test_bind_host_is_a_constant_not_an_argument() -> None:
    """Bind 先を選ばせる引数が無いこと。**既定値で守らない。**"""
    import inspect

    from harness.presentation.local_ui import server as server_module

    assert BIND_HOST == "127.0.0.1"
    parameters = set(inspect.signature(server_module.start_server).parameters)
    assert "host" not in parameters
    assert "bind" not in parameters
    # S104: 「全 Interface へ Bind する綴りが Code に無いこと」を測る表明である。
    # 綴りをそのまま書くと検査自身が引っかかるので、組み立てて比べる。
    all_interfaces = ".".join(["0"] * 4)
    forbidden = {all_interfaces, "::", "INADDR_ANY", "0:0:0:0:0:0:0:0"}
    # **docstring は除く。** 文書が「そこへは開かない」と述べるのは違反ではない。
    for path in sorted(UI_DIR.rglob("*.py")):
        for literal in _code_string_literals(path):
            assert literal not in forbidden, f"{path.name}: {literal}"


# ---------------------------------------------------------------------------
# Masking / CAS
# ---------------------------------------------------------------------------


def test_rejected_body_reaches_neither_cas_nor_sqlite(
    tmp_path: Path, api: LocalUiApi, services: LocalUiServices
) -> None:
    """Reject 分類の本文は保存されない。**部分成功も残さない。**"""
    conversation_id = _new_conversation(api)
    status, payload = _post(
        api,
        f"/api/conversations/{conversation_id}/messages",
        {"role": "USER_TASK", "text": SECRET_BODY},
    )
    assert status == 422, payload
    assert payload["error"]["code"] == "MASKING_VERIFICATION_FAILED"

    rows = services.connection.execute("SELECT COUNT(*) FROM conversation_message").fetchone()
    assert rows[0] == 0
    manifests = services.connection.execute("SELECT COUNT(*) FROM artifact_manifest").fetchone()
    assert manifests[0] == 0
    for path in (tmp_path / "cas").rglob("*"):
        if path.is_file():
            assert SECRET_BODY.encode("utf-8") not in path.read_bytes()


def test_response_never_echoes_a_rejected_body(api: LocalUiApi) -> None:
    """拒否の理由に本文を載せない。**分類名だけを述べる。**"""
    conversation_id = _new_conversation(api)
    headers = {"origin": ORIGIN, "x-harness-session": TOKEN, "content-type": "application/json"}
    response = api.handle(
        Request(
            method="POST",
            path=f"/api/conversations/{conversation_id}/messages",
            headers=headers,
            body=json.dumps({"role": "USER_TASK", "text": SECRET_BODY}).encode("utf-8"),
        )
    )
    text = response.body.decode("utf-8")
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in text
    assert SECRET_BODY not in text


def test_every_stored_message_passed_through_the_gate(services: LocalUiServices) -> None:
    """Gate を経ずに保存する経路が無いこと。

    UI は `ConversationService.append_message` しか呼ばない。その Service は
    Gate を最初に呼ぶ。**別の保存経路を UI が持っていない**ことを静的に測る。
    """
    api_source = (UI_DIR / "api.py").read_text(encoding="utf-8")
    for forbidden in ("artifacts.put", "_messages.add", "INSERT INTO", "execute("):
        assert forbidden not in api_source, forbidden
    composition = (UI_DIR / "composition.py").read_text(encoding="utf-8")
    assert "masking_gate=gate" in composition


def test_body_is_stored_in_cas_not_in_sqlite(api: LocalUiApi, services: LocalUiServices) -> None:
    conversation_id = _new_conversation(api)
    marker = "この本文はCASにだけ置かれる"
    status, _ = _post(
        api, f"/api/conversations/{conversation_id}/messages", {"role": "USER_TASK", "text": marker}
    )
    assert status == 201
    dump = "\n".join(services.connection.iterdump())
    assert marker not in dump


def test_missing_cas_bytes_are_not_turned_into_an_empty_body(
    tmp_path: Path, api: LocalUiApi
) -> None:
    """CAS から本文が消えたら「本文取得不可」と出す。**空文字列で代替しない。**"""
    conversation_id = _new_conversation(api)
    marker = "あとで消える本文"
    status, _ = _post(
        api, f"/api/conversations/{conversation_id}/messages", {"role": "USER_TASK", "text": marker}
    )
    assert status == 201

    removed = 0
    for path in sorted((tmp_path / "cas").rglob("*")):
        if path.is_file() and marker.encode("utf-8") in path.read_bytes():
            path.unlink()
            removed += 1
    assert removed == 1, "CAS の実体を見つけられなかった"

    response = api.handle(
        Request(method="GET", path=f"/api/conversations/{conversation_id}", headers={})
    )
    payload = json.loads(response.body)
    message = payload["messages"][0]
    assert message["body"] is None
    assert message["body"] != ""
    assert message["body_status"] == "UNAVAILABLE"
    assert message["body_reason"]


def test_duplicate_sequence_number_is_a_conflict(
    api: LocalUiApi, services: LocalUiServices
) -> None:
    """重複 `sequence_number` は 409 で返る。**上書きしない。**"""
    conversation_id = _new_conversation(api)
    status, payload = _post(
        api,
        f"/api/conversations/{conversation_id}/messages",
        {"role": "USER_TASK", "text": "1件目"},
    )
    assert status == 201
    existing = payload["message"]

    # 既にある `sequence_number` を Repository へ直接ぶつける。UI は経路を持たない
    # ので、ここでは Store の側が競合を拒むことを測る。
    with pytest.raises(sqlite3.IntegrityError):
        services.connection.execute(
            "INSERT INTO conversation_message ("
            " message_id, conversation_id, role, sequence_number, content_artifact_hash,"
            " record_id, created_at, producer, content_hash, store_version"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                "dup",
                conversation_id,
                "USER_TASK",
                existing["sequence_number"],
                existing["content_artifact_hash"],
                "r",
                "2026-08-31T00:00:00Z",
                "test",
                existing["content_hash"],
            ),
        )


# ---------------------------------------------------------------------------
# 送信の Fail-Closed
# ---------------------------------------------------------------------------


def test_send_is_refused_without_network_or_assistant_message(
    api: LocalUiApi, services: LocalUiServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未設定 Provider への送信は 409。**Socket を 1 度も開かない。**"""
    opened: list[Any] = []
    original = socket.socket.connect

    def _record(self: Any, address: Any) -> Any:  # pragma: no cover - 呼ばれたら失敗
        opened.append(address)
        return original(self, address)

    monkeypatch.setattr(socket.socket, "connect", _record)

    conversation_id = _new_conversation(api)
    status, payload = _post(api, "/api/chat/send", {"conversation_id": conversation_id})
    assert status == 409, payload
    assert payload["network_used"] is False
    assert payload["assistant_message_created"] is False
    assert "利用できません" in payload["error"]["reason"]
    assert opened == [], f"Network 通信が発生した: {opened}"

    rows = services.connection.execute("SELECT COUNT(*) FROM conversation_message").fetchone()
    assert rows[0] == 0, "Assistant 応答が保存された"


def test_send_never_returns_a_fabricated_assistant_message(api: LocalUiApi) -> None:
    """Mock 応答を返さない。返せば本物と見分けられなくなる。"""
    status, payload = _post(api, "/api/chat/send", {"provider_id": "mock"})
    assert status == 409
    assert "message" not in payload
    assert "assistant" not in json.dumps(payload, ensure_ascii=False).lower().replace(
        "assistant_message_created", ""
    )


def test_ui_has_no_network_or_credential_reach() -> None:
    """UI が外部通信・Keyring・Browser Cookie へ届く道具を持たないこと。"""
    forbidden = {
        "requests",
        "httpx",
        "urllib",
        "keyring",
        "http.cookiejar",
        "webbrowser",
        "selenium",
        "playwright",
    }
    hits: list[str] = []
    for path in sorted(UI_DIR.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for name in re.findall(r"^\s*(?:import|from)\s+([\w.]+)", source, re.MULTILINE):
            if name.split(".")[0] in forbidden or name in forbidden:
                hits.append(f"{path.name}: {name}")
    assert hits == [], hits


# ---------------------------------------------------------------------------
# Web 資産
# ---------------------------------------------------------------------------


def test_ui_assets_use_no_inner_html_and_no_external_urls() -> None:
    """User navigation uses two fixed official URLs; external embedded assets stay forbidden."""
    allowed_navigation = {
        "index.html": {"https://chatgpt.com/settings/usage"},
        "app.js": {"https://auth.openai.com"},
        "app.css": set(),
    }
    for filename, allowed in allowed_navigation.items():
        raw = (STATIC_DIR / filename).read_text(encoding="utf-8")
        source = _without_comments(raw, filename)
        for forbidden in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "eval(",
        ):
            assert forbidden not in source, f"{filename}: {forbidden}"
        urls = set(re.findall(r"https?://[^\s\"'()]+", source))
        assert urls == allowed, f"{filename}: unexpected navigation URL {urls ^ allowed}"
        for forbidden in ("cdn", "googleapis", "unpkg", "jsdelivr", "iframe", "webview"):
            assert forbidden not in source.lower(), f"{filename}: {forbidden}"
    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'href="https://chatgpt.com/settings/usage"' in page
    assert 'rel="noopener noreferrer"' in page
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'url.origin !== "https://auth.openai.com"' in script
    assert 'url.pathname !== "/api/accounts/authorize"' in script
    assert "url.username || url.password || url.hash" in script
    assert "popup.opener = null" in script


def test_ui_assets_do_not_touch_cookies_or_storage() -> None:
    """Browser の Login 状態を取り込まないこと。"""
    source = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for forbidden in ("document.cookie", "localStorage", "sessionStorage", "indexedDB"):
        assert forbidden not in source, forbidden
    assert 'init.credentials = "omit"' in source


def test_response_carries_a_content_security_policy(api: LocalUiApi) -> None:
    response = api.handle(Request(method="GET", path="/", headers={}))
    headers = dict(response.headers)
    csp = headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "connect-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"


def test_session_token_is_injected_into_the_page_not_the_url() -> None:
    token = new_session_token()
    assets = load_assets(token)
    page = assets["/"][0].decode("utf-8")
    assert token in page
    assert "__HARNESS_SESSION_TOKEN__" not in page
    for route, _, _ in ASSET_TABLE:
        assert token not in route


# ---------------------------------------------------------------------------
# 入力検証
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/../../etc/passwd",
        "/app.js/../../../etc/passwd",
        "/static/%2e%2e/%2e%2e/etc/passwd",
        "/api/conversations?token=x",
        "/api/conversations#fragment",
        "/api\\conversations",
    ],
)
def test_malformed_paths_are_rejected(api: LocalUiApi, path: str) -> None:
    response = api.handle(Request(method="GET", path=path, headers={}))
    assert response.status == 400, path
    assert b"passwd" not in response.body or b"root:" not in response.body


def test_unknown_route_is_404(api: LocalUiApi) -> None:
    response = api.handle(Request(method="GET", path="/api/unknown", headers={}))
    assert response.status == 404


def test_bad_origin_is_forbidden(api: LocalUiApi) -> None:
    status, _ = _post(api, "/api/conversations", origin="http://evil.example")
    assert status == 403
    status, _ = _post(api, "/api/conversations", origin="")
    assert status == 403


def test_bad_session_token_is_forbidden(api: LocalUiApi) -> None:
    status, _ = _post(api, "/api/conversations", token="c" * 64)
    assert status == 403
    status, _ = _post(api, "/api/conversations", token="")
    assert status == 403


def test_wrong_content_type_is_rejected(api: LocalUiApi) -> None:
    status, _ = _post(api, "/api/conversations", content_type="text/plain")
    assert status == 400


def test_malformed_json_is_rejected(api: LocalUiApi) -> None:
    status, _ = _post(api, "/api/conversations", raw=b"{not json")
    assert status == 400


def test_oversized_request_is_rejected(api: LocalUiApi, services: LocalUiServices) -> None:
    oversized = b'{"x":"' + b"a" * (services.max_body_bytes + 16) + b'"}'
    status, _ = _post(api, "/api/conversations", raw=oversized)
    assert status == 413


def test_oversized_message_text_is_rejected(api: LocalUiApi, services: LocalUiServices) -> None:
    conversation_id = _new_conversation(api)
    text = "あ" * (services.max_body_bytes // 3 + 8)
    status, _ = _post(
        api, f"/api/conversations/{conversation_id}/messages", {"role": "USER_TASK", "text": text}
    )
    assert status == 413


def test_unknown_request_fields_are_rejected(api: LocalUiApi) -> None:
    status, payload = _post(api, "/api/conversations", {"title": "x"})
    assert status == 400
    assert "title" in payload["error"]["reason"]


def test_internal_failures_do_not_leak_details(
    api: LocalUiApi, monkeypatch: pytest.MonkeyPatch, services: LocalUiServices
) -> None:
    """分類できない失敗は 500 で、内部情報を返さないこと。"""

    def _boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("internal detail: /secret/path and a password")

    monkeypatch.setattr(services.conversations, "list_conversations", _boom)
    response = api.handle(Request(method="GET", path="/api/conversations", headers={}))
    assert response.status == 500
    text = response.body.decode("utf-8")
    assert "secret" not in text
    assert "RuntimeError" not in text
    assert json.loads(text)["error"]["reason"] == "internal error"
