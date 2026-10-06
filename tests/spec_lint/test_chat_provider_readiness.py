"""Chat の Provider 接続前段の監査を固定する試験。

## この Task は実装していない

Phase 1 の実測で契約が足りず、`DCR-CHAT-PROVIDER-CONFIG` を起票して止めた。
ここで測るのは **止まっている状態が本当に保たれているか** である。

* 監査器が凍結 Report を上書きせず、合成 Git 入力から Report を再現できる
* 監査器が測った木の設計・Registry・Route Policy の Hash へ束縛し、全項目に根拠を持たせる
* 回答済み Package を回答時点 Canon へ束縛できる（合成 Git 履歴）
* Mock 経路に Network / Keyring を持ち込まず、追加のChatGPT Previewを完全一致で限定する
* Provider Adapter が Registry の allowlist 外を拒否する
* 監査 Script が Provider ID を手入力していない

保存 v1/v3 Report と `DCR-CHAT-PROVIDER-CONFIG` は非公開の回答・監査記録であり、
公開用の配布コピーに収録しない。その内容の検査は `tests/private_history/` にある。

## 通ることではなく通らないことを測る

「実装していない」は主張ではなく実測でなければならない。Network 用 Module を
import が明示したChatGPT Previewの組を外れるか、Keyring Libraryを足すと、この試験が落ちる。
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import chat_canon_binding
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash
from harness.infrastructure.provider.mock_provider import DeterministicMockProvider
from harness.ports.provider import ProviderRequest

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src/harness"
AUDIT_TOOL = REPO_ROOT / "tools/audit_chat_provider_readiness.py"
#: 保存 v3 を固定 Git 入力から再現する Verifier（Owner Decision 案A）。
HISTORY_TOOL = REPO_ROOT / "tools/verify_provider_readiness_history.py"
#: v1。**凍結してある。** Hash が回答済み `DCR-CHAT-PROVIDER-CONFIG` に束縛される。
AUDIT = REPO_ROOT / "docs/audit/chat-provider-readiness.json"

#: v3。測り方を直した監査器の出力。
AUDIT_V3 = REPO_ROOT / "docs/audit/provider-readiness-v3.json"

ROUTE_POLICY = REPO_ROOT / "design-source/registries/route-policy.yaml"

#: 外部通信を持ち込む Module。Chat の Provider 接続はまだ契約が無い。
NETWORK_MODULES = frozenset(
    {
        "socket",
        "ssl",
        "http",
        "urllib",
        "requests",
        "httpx",
        "ftplib",
        "smtplib",
        "xmlrpc",
        "webbrowser",
    }
)

#: Keyring を読む Module。service 名の正本が無いので、まだ触らない。
KEYRING_MODULES = frozenset({"keyring", "secretstorage", "gnomekeyring", "win32cred", "keyrings"})

_H = ContentHash.parse("sha256:" + "0" * 64)


def _route() -> dict:
    return dict(yaml.safe_load(ROUTE_POLICY.read_text(encoding="utf-8")))


#: 受信専用として認める `(Repository 相対 Path, Module 名)` の完全一致。
#: **Loopback へ Listen するだけの Module である。** 送信の道具ではない。
#: 例外の中身は `test_the_only_network_import_is_an_inbound_listener` が縛る。
INBOUND_ONLY_EXEMPTIONS = frozenset(
    {("src/harness/presentation/local_ui/server.py", "http.server")}
)


#: Owner-requested Workbench ChatGPT preview only. Exact file/module pairs, no directory wildcard.
#: Mock Provider and normative runtime routes still do not acquire network capabilities.
CHATGPT_PREVIEW_IMPORTS = frozenset(
    {
        ("src/harness/infrastructure/provider/chatgpt_auth.py", "http.server"),
        ("src/harness/infrastructure/provider/chatgpt_auth.py", "urllib.parse"),
        ("src/harness/infrastructure/provider/chatgpt_generation.py", "http.client"),
        ("src/harness/infrastructure/provider/chatgpt_generation.py", "socket"),
        ("src/harness/infrastructure/provider/chatgpt_http.py", "http.client"),
        ("src/harness/infrastructure/provider/chatgpt_http.py", "ssl"),
        ("src/harness/infrastructure/provider/chatgpt_http.py", "urllib.parse"),
    }
)

#: 例外を置いた File が持っていてはならない、送信の道具。
OUTBOUND_TOOLS = (
    "http.client",
    "urlopen",
    "create_connection",
    ".connect(",
    "socket.",
    "getaddrinfo",
)


def _imported_module_names(path: Path) -> set[str]:
    """import した Module の**完全な名前**。`http` と `http.server` を区別する。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


# ---------------------------------------------------------------------------
# 監査 Report が現物と一致する
# ---------------------------------------------------------------------------


def test_the_analyzer_refuses_to_overwrite_v1() -> None:
    """監査器が v1 の Path へ書こうとしたら止まること。

    **凍結を運用の注意で守らない。** 書けない形にする。
    """
    result = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
        [sys.executable, str(AUDIT_TOOL), "--out", str(AUDIT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, "v1 を上書きできてしまう"
    assert "FROZEN_REPORT" in result.stdout + result.stderr


def test_audit_report_is_reproducible_from_pinned_git_inputs(tmp_path: Path) -> None:
    """合成Git入力で実検証器を実行する。私的な保存v3の再現は非公開側で保持する。"""
    import verify_provider_readiness_history as history
    from synthetic_history_fixture import build_provider_history

    fixture = build_provider_history(tmp_path / "synthetic-provider")
    report = history.verify(fixture.root, fixture.manifest)
    assert report["status"] == "HISTORICAL_REPRODUCED"
    assert report["is_runtime_evidence"] is False
    assert report["bound_package_count"] == len(fixture.manifest["bound_packages"])
    assert report["bound_package_count"] > 0


def _synthetic_audit(tmp_path: Path) -> tuple[Path, dict]:
    """実監査器を合成の木で走らせた Report。保存 Report は読まない。"""
    import verify_provider_readiness_history as history
    from synthetic_history_fixture import build_provider_history

    fixture = build_provider_history(tmp_path / "synthetic-provider")
    report_path, _ = history._report_paths()
    return fixture.root, json.loads((fixture.root / report_path).read_text(encoding="utf-8"))


def test_audit_binds_the_design_and_registry_it_measured(tmp_path: Path) -> None:
    """監査 Report が、測った木の設計・Registry・Route Policy の Hash を名乗ること。

    合成の木は現行と別の Canon を持つ。**現行 Snapshot を書き写していたら落ちる。**
    """
    root, report = _synthetic_audit(tmp_path)
    snapshot = json.loads((root / "registry-snapshot.json").read_text(encoding="utf-8"))
    current = json.loads((REPO_ROOT / "registry-snapshot.json").read_text(encoding="utf-8"))
    assert report["design_sha256"] == snapshot["design_sha256"]
    assert report["registry_snapshot_hash"] == snapshot["registry_snapshot_hash"]
    assert report["design_sha256"] != current["design_sha256"]
    route = root / "design-source/registries/route-policy.yaml"
    assert (
        report["route_policy_sha256"] == "sha256:" + hashlib.sha256(route.read_bytes()).hexdigest()
    )


def test_every_item_of_a_fresh_audit_carries_its_evidence(tmp_path: Path) -> None:
    """`derivable` だけの項目を作らないこと。**根拠の無い判定を残さない。**"""
    _, report = _synthetic_audit(tmp_path)
    assert report["items"], "合成の木で項目が 0 件なら、この試験は何も見ていない"
    for item in report["items"]:
        assert item["detail"], item
        assert item["source"], item
        assert item["kind"] in {"CONTRACT", "IMPLEMENTATION"}, item


def test_the_analyzer_refuses_to_overwrite_the_saved_v3() -> None:
    """保存 v3 を CLI から上書きできないこと。JSON と Markdown の両方を塞ぐ。

    **凍結を運用の注意で守らない。** 片方だけ塞ぐと Markdown から壊れる。
    既定の出力先が保存 v3 なので、引数なしの実行も止まる。
    """
    for argv in (
        ["--out", str(AUDIT_V3), "--md-out", "/dev/null"],
        ["--out", "/dev/null", "--md-out", str(REPO_ROOT / "docs/audit/provider-readiness-v3.md")],
        [],
    ):
        result = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
            [sys.executable, str(AUDIT_TOOL), *argv],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0, f"保存 v3 を上書きできてしまう: {argv}"
        assert "FROZEN_REPORT" in result.stdout + result.stderr


# ---------------------------------------------------------------------------
# 不足があるなら止まっている
# ---------------------------------------------------------------------------


def test_the_answers_are_not_reflected_yet() -> None:
    """回答があっても、正本へまだ反映していないこと。

    回答は「そう決めた」であって「そう書いた」ではない。反映は後続 Task である。
    **記録と反映を混同すると、決めた瞬間に実装済みだと読めてしまう。**

    測る対象は監査が不足と判定した項目そのものである。どれか 1 つでも正本へ入れば
    監査 Report が変わり、`test_audit_report_is_reproducible_from_the_current_canon`
    と合わせてここが落ちる。
    """
    route = _route()
    fields = {key for entry in route["providers"] for key in entry}
    for absent in ("endpoint", "models", "secret_ref", "priority"):
        assert absent not in fields, f"providers へ {absent} が入っている（反映は後続 Task）"
    assert "capabilities" not in route, "capabilities の Catalog 節が入っている"
    declared = route["fallback_eligible_failures"] + route["fallback_forbidden_failures"]
    uncoded = [f["id"] for f in declared if f.get("error_code") is None]
    assert uncoded, "error_code: null の障害が 0 件になった。採番は後続 Task である"


def test_dcr_is_bound_to_the_answer_time_canon(tmp_path: Path) -> None:
    """合成Package/Reportを実Git履歴へ束縛する。私的な旧回答の正当性は主張しない。"""
    from synthetic_history_fixture import build_bound_audit_fixture

    root, package, _ = build_bound_audit_fixture(tmp_path / "bound-audit")
    resolved = chat_canon_binding.verify_answer_time_canon(
        root, package, package_id=package["package_id"]
    )
    assert resolved["design"]["resolved_hash"] == package["design_sha256"]
    assert resolved["design"]["design_version"] == package["design_version"]
    assert resolved["snapshot"]["matches"] is True
    measured = (
        "sha256:"
        + hashlib.sha256((root / package["audit_report"]["path"]).read_bytes()).hexdigest()
    )
    assert package["audit_report"]["sha256"] == measured


# ---------------------------------------------------------------------------
# 止まっている状態が保たれている
# ---------------------------------------------------------------------------


def test_production_does_not_reach_the_network() -> None:
    """Only the owner-requested preview has exact network imports; all other routes stay denied."""
    violations: list[str] = []
    preview_imports: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        relative = str(path.relative_to(REPO_ROOT))
        for name in sorted(_imported_module_names(path)):
            if name.split(".")[0] not in NETWORK_MODULES:
                continue
            pair = (relative, name)
            if pair in INBOUND_ONLY_EXEMPTIONS:
                continue
            if pair in CHATGPT_PREVIEW_IMPORTS:
                preview_imports.add(pair)
                continue
            violations.append(f"{relative}: {name}")
    assert not violations, "Undeclared network import:\n" + "\n".join(violations)
    assert preview_imports == CHATGPT_PREVIEW_IMPORTS, "Preview import declarations drifted"


def test_the_only_network_import_is_an_inbound_listener() -> None:
    """例外に置いた File が **送信できない**ままであること。

    例外を作った以上、例外の中身を縛らなければ、そこが抜け道になる。
    """
    assert len(INBOUND_ONLY_EXEMPTIONS) == 1, "例外は 1 組だけである"
    for relative, module in sorted(INBOUND_ONLY_EXEMPTIONS):
        path = REPO_ROOT / relative
        assert path.is_file(), relative
        assert module in _imported_module_names(path), f"{relative} が {module} を使っていない"
        source = path.read_text(encoding="utf-8")
        for tool in OUTBOUND_TOOLS:
            assert tool not in source, f"{relative} が送信の道具を持っている: {tool}"
        # Loopback 以外へ開かないことは、この File の定数が決める。
        assert 'BIND_HOST: Final[str] = "127.0.0.1"' in source


def test_production_does_not_read_a_keyring() -> None:
    """Keyring service 名の正本が無いうちは Keyring を読まないこと。

    読めば、正本に無い service 名が Code で固定される（不変条件#18）。
    """
    violations = [
        f"{path.relative_to(REPO_ROOT)}: {sorted(_imported_roots(path) & KEYRING_MODULES)}"
        for path in sorted(SRC.rglob("*.py"))
        if _imported_roots(path) & KEYRING_MODULES
    ]
    assert not violations, "Production が Keyring Module を import している:\n" + "\n".join(
        violations
    )


def test_provider_adapter_accepts_only_the_enabled_allowlist() -> None:
    """Registry で有効な Provider だけを Adapter が受け入れること。

    Provider ID を試験へ手入力しない。**Registry から読む。**
    """
    enabled = [entry["id"] for entry in _route()["providers"] if entry.get("enabled")]
    assert enabled, "Registry に有効な Provider が 1 件も無い"
    adapter = DeterministicMockProvider(adapter_version="test")
    for provider_id in enabled:
        response = adapter.propose(_request(provider_id))
        assert response.provider_id == provider_id
        assert response.network_used is False


def test_provider_adapter_rejects_an_unlisted_provider() -> None:
    """allowlist に無い Provider を、外部通信の手前で拒否すること。"""
    listed = {entry["id"] for entry in _route()["providers"]}
    unlisted = "provider-not-in-the-allowlist"
    assert unlisted not in listed
    adapter = DeterministicMockProvider(adapter_version="test")
    with pytest.raises(HarnessError) as caught:
        adapter.propose(_request(unlisted))
    assert caught.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH


def _request(provider_id: str) -> ProviderRequest:
    return ProviderRequest(
        provider_id=provider_id,
        model_id="model-under-test",
        instruction_hash=_H,
        context_bundle_hash=_H,
        input_artifact_hash=_H,
        output_schema_hash=_H,
    )


# ---------------------------------------------------------------------------
# 値を手入力していない
# ---------------------------------------------------------------------------


def test_audit_script_does_not_hardcode_provider_values() -> None:
    """監査 Script に Provider ID と Capability 名を書いていないこと。

    書けば、正本にある値と Script の値が二重になる。DCR Script の同じ検査は、
    DCR Script と一緒に非公開側（`tests/private_history/`）に置いた。
    """
    values = {entry["id"] for entry in _route()["providers"]}
    for entry in _route()["providers"]:
        values |= set(entry.get("capabilities") or [])
    source = AUDIT_TOOL.read_text(encoding="utf-8")
    leaked = sorted(value for value in values if f'"{value}"' in source)
    assert not leaked, f"{AUDIT_TOOL.name} が正本の値を手入力している: {leaked}"


def test_no_provider_error_code_was_minted() -> None:
    """Provider 障害の Error Code を推測で足していないこと。

    `route-policy.yaml` は「Provider Adapter を実装する Task で採番する」と書いて
    いる。この Task は採番する Task ではない。
    """
    snapshot = json.loads((REPO_ROOT / "registry-snapshot.json").read_text(encoding="utf-8"))
    codes = set(snapshot["error_codes"])
    route = _route()
    declared = route["fallback_eligible_failures"] + route["fallback_forbidden_failures"]
    minted = sorted(f["id"] for f in declared if f["id"] in codes and f.get("error_code") is None)
    assert not minted, f"error_code: null のまま Error Code へ足されている: {minted}"
