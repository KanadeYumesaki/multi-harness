"""`127.0.0.1` だけに Bind する HTTP Server。

## 外へ出さない

Bind 先は `127.0.0.1` の定数である。**引数で変えられない。** `0.0.0.0` も `::`
も外部 IP も渡せない。渡す口が無いことが保証であって、既定値で守るのではない。

## Port を固定しない

既定は Ephemeral Port（`0`）で、OS が空きを選ぶ。実際に開いた URL を起動時に
表示する。固定 Port は他の Process と衝突し、衝突した相手の画面を自分の画面だ
と思って操作する事故を生む。

## Session Token を永続化しない

起動ごとに `secrets.token_hex(32)` で作り、Process が終われば消える。File へも
環境変数へも書かない。Token は Header だけで運び、URL にも Query にも載せない。

## Request を 1 件ずつへ直列化する

`ThreadingHTTPServer` は Request ごとに別 Thread を作る。SQLite 接続を素のまま
共有すると `sqlite3.ProgrammingError` になり、画面には「storage temporarily
unavailable」としか出ない。**Handler の入口で Lock を取り、1 度に 1 Request だけ**
が Application へ入るようにする。

長い CLI 呼出しはこの Lock の外にある。Workbench Gateway が claim だけを
Request 内で行い、起動は別 Thread の Worker へ渡すからである。Thread を使う理由は
並行処理ではなく、**Handler を短く保って Keep-Alive 接続が互いを塞がないこと**
にある。

## Log に本文を出さない

`BaseHTTPRequestHandler` の既定 Log は Request 行をそのまま出す。Query を受け
取らない契約なので Path に本文は載らないが、**それでも Method と状態 Code だけ**
に絞る。
"""

from __future__ import annotations

import secrets
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final

from harness.presentation.local_ui.api import LocalUiApi, Request

__all__ = ["BIND_HOST", "LocalUiServer", "load_assets", "new_session_token"]

#: **ここを引数にしない。** Loopback だけに開く。
BIND_HOST: Final[str] = "127.0.0.1"

STATIC_DIR: Final[str] = "static"

#: 配れる資産の固定表。**Filesystem を走査しない。** 表に無い名前は 404 になる。
ASSET_TABLE: Final[tuple[tuple[str, str, str], ...]] = (
    ("/", "index.html", "text/html; charset=utf-8"),
    ("/app.css", "app.css", "text/css; charset=utf-8"),
    ("/app.js", "app.js", "text/javascript; charset=utf-8"),
    ("/knowledge-import.js", "knowledge-import.js", "text/javascript; charset=utf-8"),
)

#: HTML の中で Session Token を差し込む場所。
SESSION_PLACEHOLDER: Final[str] = "__HARNESS_SESSION_TOKEN__"

#: Access Log の 1 行を他の Thread の行と混ぜないための Lock。
_LOG_LOCK: Final[threading.Lock] = threading.Lock()


def new_session_token() -> str:
    """起動ごとの Session Token。**保存しない。**"""
    return secrets.token_hex(32)


def load_assets(session_token: str) -> dict[str, tuple[bytes, str]]:
    """固定表のとおりに資産を読む。Path を組み立てて走査しない。"""
    base = Path(__file__).resolve().parent / STATIC_DIR
    assets: dict[str, tuple[bytes, str]] = {}
    for route, filename, content_type in ASSET_TABLE:
        text = (base / filename).read_text(encoding="utf-8")
        assets[route] = (
            text.replace(SESSION_PLACEHOLDER, session_token).encode("utf-8"),
            content_type,
        )
    return assets


@dataclass(frozen=True, slots=True)
class LocalUiServer:
    """開いた Server と、その URL・Token。"""

    httpd: ThreadingHTTPServer
    url: str
    session_token: str

    def serve_forever(self) -> None:  # pragma: no cover - 実行時のみ
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _handler_class(
    api_holder: dict[str, LocalUiApi], max_body: int, gate: threading.Lock
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "harness-local-ui"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            """本文も Path も出さない。Method と状態 Code だけを出す。

            Request ごとに Thread が違う。`print()` は本文と改行を別々に書くので、
            同時に来た 2 件が 1 行へ混ざる。**1 行を 1 回の write で Lock の中で出す。**
            """
            code = args[1] if len(args) > 1 else "-"
            line = f"[ui] {self.command} {code}\n"
            with _LOG_LOCK:
                sys.stdout.write(line)
                sys.stdout.flush()

        def _read_body(self) -> bytes | None:
            raw = self.headers.get("Content-Length", "0")
            if not raw.isdigit():
                return None
            length = int(raw)
            if length > max_body:
                return None
            return self.rfile.read(length)

        def _respond(self, request: Request) -> None:
            # **1 度に 1 Request だけ**を Application へ通す。共有 SQLite 接続の
            # 所有権をここで直列化する。
            with gate:
                response = api_holder["api"].handle(request)
            self.send_response(response.status)
            for name, value in response.headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response.body)

        def _headers(self) -> Mapping[str, str]:
            return {name.lower(): value for name, value in self.headers.items()}

        def do_GET(self) -> None:
            self._respond(Request(method="GET", path=self.path, headers=self._headers()))

        def do_POST(self) -> None:
            body = self._read_body()
            if body is None:
                # 長さが読めない・大きすぎる。**読み込まずに落とす。**
                self.send_response(413)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._respond(
                Request(method="POST", path=self.path, headers=self._headers(), body=body)
            )

    return Handler


def start_server(
    *,
    build_api: Any,
    port: int = 0,
    max_body_bytes: int,
) -> LocalUiServer:
    """Loopback へ Bind し、開いた URL を返す。

    `build_api` は `(origin, session_token, assets) -> LocalUiApi`。Origin は
    Bind した後でないと決まらないので、Server を開いてから API を組む。
    """
    token = new_session_token()
    holder: dict[str, LocalUiApi] = {}
    handler = _handler_class(holder, max_body_bytes, threading.Lock())
    httpd = ThreadingHTTPServer((BIND_HOST, port), handler)
    address = httpd.server_address
    # 開いた先が Loopback であることを、開いた後にもう一度確かめる。
    # 落ちた理由に値は要らない。**等しいかどうかだけ**を見る。
    if address[0] != BIND_HOST:  # pragma: no cover - 定数なので到達しない
        httpd.server_close()
        raise RuntimeError("server did not bind to the loopback address")
    origin = f"http://{BIND_HOST}:{int(address[1])}"
    holder["api"] = build_api(origin, token, load_assets(token))
    return LocalUiServer(httpd=httpd, url=origin, session_token=token)
