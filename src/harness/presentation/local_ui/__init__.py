"""127.0.0.1 だけに開くローカル UI。

外部 LLM へ送らない。Browser の Cookie も Session も読まない。ChatGPT Web を
埋め込まない。CLI を推測して起動しない。**Network へ出る経路を 1 つも持たない。**

会話の作成・Message の保存・履歴の表示・Snapshot の表示・Context Preview・
Provider 状態の表示を、**既存の Application Service を呼んで**行う。
"""

from __future__ import annotations

from harness.presentation.local_ui.api import LocalUiApi, Request, Response
from harness.presentation.local_ui.composition import LocalUiServices, build_services
from harness.presentation.local_ui.providers import load_provider_status
from harness.presentation.local_ui.server import BIND_HOST, LocalUiServer, start_server

__all__ = [
    "BIND_HOST",
    "LocalUiApi",
    "LocalUiServer",
    "LocalUiServices",
    "Request",
    "Response",
    "build_services",
    "load_provider_status",
    "start_server",
]
