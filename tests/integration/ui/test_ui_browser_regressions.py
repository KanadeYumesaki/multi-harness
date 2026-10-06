"""GH-01 の実ブラウザー検査で見つけた不具合の回帰試験（ブラウザー不要の部分）。

画面の focus と古い確認内容の表示は JavaScript の挙動なので、実ブラウザーの
`tools/run_ui_browser_check.py` が検出する。ここでは Python だけで決定論的に
確かめられるもの（Access Log の行、CSS の色 Token、Runner の未実行表示）を扱う。
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from harness.presentation.local_ui.api import Response
from harness.presentation.local_ui.server import start_server

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[3]
CSS = REPO / "src/harness/presentation/local_ui/static/app.css"
RUNNER = REPO / "tools/run_ui_browser_check.py"
LOG_LINE = re.compile(r"\[ui\] (GET|POST) \d{3}")


class _Recorder(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.writes: list[str] = []

    def write(self, text: str) -> int:
        self.writes.append(text)
        return super().write(text)


class _StaticApi:
    def handle(self, _request: Any) -> Response:
        return Response(status=200, body=b"{}", content_type="application/json")


def _serve() -> Any:
    return start_server(
        build_api=lambda origin, token, assets: _StaticApi(), port=0, max_body_bytes=1024
    )


def test_access_log_writes_each_line_in_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """`print()` は本文と改行を分けて書き、同時Requestの行が混ざった（GH-01 D4）。"""
    recorder = _Recorder()
    monkeypatch.setattr(sys, "stdout", recorder)
    server = _serve()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(server.url + "/api/x", timeout=5) as response:  # noqa: S310
            assert response.status == 200
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert recorder.writes == ["[ui] GET 200\n"]


def test_concurrent_requests_never_share_a_log_line(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(sys, "stdout", recorder)
    server = _serve()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def burst() -> None:
        for _ in range(15):
            with urllib.request.urlopen(server.url + "/api/x", timeout=5) as response:  # noqa: S310
                response.read()

    workers = [threading.Thread(target=burst) for _ in range(12)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=60)
    finally:
        server.shutdown()
        thread.join(timeout=5)
    lines = recorder.getvalue().splitlines()
    assert len(lines) == 12 * 15
    assert [line for line in lines if not LOG_LINE.fullmatch(line)] == []


def _tokens(css: str) -> tuple[dict[str, str], dict[str, str]]:
    light_block = re.search(r"^:root\s*\{(.*?)\}", css, re.S | re.M)
    dark_block = re.search(
        r"@media \(prefers-color-scheme: dark\)\s*\{\s*:root\s*\{(.*?)\}", css, re.S
    )
    assert light_block and dark_block

    def parse(block: str) -> dict[str, str]:
        return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", block))

    light = parse(light_block.group(1))
    return light, {**light, **parse(dark_block.group(1))}


def _luminance(color: str) -> float:
    value = color.strip().lstrip("#")
    assert re.fullmatch(r"[0-9a-fA-F]{6}", value), color
    channels = [int(value[index : index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    a, b = _luminance(first), _luminance(second)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def _resolve(value: str, tokens: dict[str, str]) -> str:
    match = re.fullmatch(r"var\((--[\w-]+)\)", value.strip())
    return tokens[match.group(1)] if match else value.strip()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_operation_cards_keep_text_readable_in_both_schemes(scheme: str) -> None:
    """dark modeで白い固定背景に明るい文字が載り、コントラスト1.13だった（GH-01 D3）。"""
    css = CSS.read_text(encoding="utf-8")
    light, dark = _tokens(css)
    tokens = light if scheme == "light" else dark
    rule = re.search(r"\.ops-cards article\s*\{([^}]*)\}", css)
    assert rule
    background = re.search(r"background:\s*([^;]+);", rule.group(1))
    assert background
    card = _resolve(background.group(1), tokens)
    for text in ("--ink", "--muted", "--warn"):
        assert _contrast(tokens[text], card) >= 4.5, (scheme, text)


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed interpreter and repository script
        [sys.executable, str(RUNNER), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO,
    )


def test_browser_runner_reports_unverified_without_a_browser(tmp_path: Path) -> None:
    out = tmp_path / "result"
    completed = _run(["--out", str(out), "--chromium", str(tmp_path / "absent-chromium")])
    assert completed.returncode == 3, completed.stderr
    document = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert document["overall"] == "UNVERIFIED"
    assert document["unavailable_reason"] == "CHROMIUM_EXECUTABLE_NOT_FOUND"
    assert {check["status"] for check in document["checks"]} == {"UNVERIFIED"}
    import run_ui_browser_check as runner

    assert sorted({check["check_id"] for check in document["checks"]}) == sorted(
        check_id for check_id, _ in runner.ALL_CHECKS
    )
    assert len(document["checks"]) == len(runner.ALL_CHECKS)


def test_browser_runner_refuses_an_existing_output_directory(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("existing evidence", encoding="utf-8")
    completed = _run(["--out", str(tmp_path), "--chromium", "/nonexistent"])
    assert completed.returncode == 2
    assert sorted(path.name for path in tmp_path.iterdir()) == ["keep.txt"]


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize(
    ("foreground", "background"),
    [
        ("--ink", "--panel"),
        ("--muted", "--panel"),
        ("--muted", "--card"),
        ("--lavender-ink", "--lavender"),
        ("--mint-ink", "--mint"),
        ("--button-ink", "--button"),
        ("--rail-ink", "--rail"),
        ("--rail-muted", "--rail"),
        ("--accent", "--ground"),
    ],
)
def test_workspace_labels_buttons_and_status_remain_readable(
    scheme: str, foreground: str, background: str
) -> None:
    """Cards, approvals and navigation must retain readable text in either theme."""
    light, dark = _tokens(CSS.read_text(encoding="utf-8"))
    tokens = light if scheme == "light" else dark
    assert _contrast(tokens[foreground], tokens[background]) >= 4.5, (
        scheme,
        foreground,
        background,
    )
