"""Provider Readiness 監査 v2 の判定を固定する試験。

## なぜ要るか

v1 は `src/` 全体の識別子を集めて「名前があるか」で判定していた。その集合には
生成物の語彙、試験用の代役、誰も呼んでいない関数、docstring に出てくる語が
混ざる。実際、ローカル UI の散文が資格情報 Store の名を書いただけで
「Keyring Adapter がある」と判定した。**無いものを有ると報告するところだった。**

ここで測るのは「実装済みと言えるか」ではなく、**言えない材料で言ってしまわないか**
である。

## 何を測るか

* 監査器が Provider ID を 1 つも持たないこと（正本から読むこと）
* 監査器が値を 1 つも作らないこと
* Retry と Failover が別の領域として分かれていること
* docstring・Comment・型名だけ・生成物・代役・呼出元なしが実装の根拠にならないこと
* Network も Keyring も触る道具を持たないこと
* v1 の Report を上書きしていないこと

## Report の実行結果を読まない

`test_test_scope.py` と同じ理由である。ただしこの監査は全体実行を含まないので、
Report を読んでも循環しない。**それでも念のため、判定はその場で測り直す。**

## 保存 Package を読まない

監査器は Provider 具体値の Package と v1 Report を読む。どちらも Owner の回答・
監査記録であり、公開用の配布コピーに収録しない。ここでは合成の上流から正規の
生成器が組んだ Package（`tests/support/synthetic_value_package.py`）と合成の v1 を
読ませる。Source Index と Registry は本物を測る。保存 Report が動いていないことは
`tests/private_history/` の試験が確かめる。
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

import audit_provider_readiness_v2 as auditor

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
AUDITOR = REPO_ROOT / "tools/audit_provider_readiness_v2.py"
ROUTE_POLICY = REPO_ROOT / "design-source/registries/route-policy.yaml"
#: 下の Fixture が合成の写しへ向ける。**保存 Package を開かない。**
VALUE_PACKAGE = REPO_ROOT / "docs/decision/DCR-CHAT-PROVIDER-VALUE-INPUT.json"

#: 監査器が持ってはならない道具。Network も Keyring も触らない。
FORBIDDEN_IMPORTS = ("socket", "urllib", "requests", "httpx", "keyring", "ssl", "http")

#: 実装済みと言える判定。ここへ誤って落ちないことを測る。
POSITIVE = {"IMPLEMENTED", "CONFIGURED"}


@pytest.fixture(scope="session")
def value_chain(tmp_path_factory: pytest.TempPathFactory) -> Any:
    from synthetic_value_package import build_value_chain

    return build_value_chain(tmp_path_factory.mktemp("readiness-v2-chain") / "root")


@pytest.fixture(autouse=True)
def synthetic_inputs(
    value_chain: Any, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """監査器の入力 Package と v1 を合成の Root へ向ける。Source と Registry は本物。"""
    monkeypatch.setattr(auditor, "ROOT", value_chain.root)
    monkeypatch.setattr(auditor, "VALUE_PACKAGE", value_chain.package)
    monkeypatch.setattr(auditor, "ANSWER_PACKAGE", value_chain.answers)
    monkeypatch.setattr(auditor, "METHOD_PACKAGE", value_chain.method)
    monkeypatch.setattr(request.module, "VALUE_PACKAGE", value_chain.package)


def _measure() -> dict[str, Any]:
    """その場で測り直す。Report を信じない。"""
    result: dict[str, Any] = auditor.measure()
    return result


def _providers() -> list[dict[str, Any]]:
    document = yaml.safe_load(ROUTE_POLICY.read_text(encoding="utf-8"))
    providers: list[dict[str, Any]] = document["providers"]
    return providers


def _auditor_string_literals() -> list[str]:
    """監査器の Code に現れる文字列 Literal。docstring は除く。"""
    tree = ast.parse(AUDITOR.read_text(encoding="utf-8"), filename=str(AUDITOR))
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


# ---------------------------------------------------------------------------
# 監査器が持ってはならないもの
# ---------------------------------------------------------------------------


def test_the_auditor_holds_no_provider_id() -> None:
    """監査器が Provider ID を 1 つも持たないこと。**正本から読むこと。**"""
    ids = {str(entry["id"]) for entry in _providers()}
    assert ids, "route-policy.yaml に Provider が 1 件も無い"
    literals = set(_auditor_string_literals())
    leaked = sorted(ids & literals)
    assert leaked == [], f"監査器が Provider ID を持っている: {leaked}"


def test_the_auditor_reads_provider_ids_from_the_registry() -> None:
    """Report が Provider ID の出典として正本を挙げていること。"""
    doc = _measure()
    source = doc["provider_id_source"]
    assert source["path"] == "design-source/registries/route-policy.yaml"
    assert source["provider_count"] == len(_providers())
    subjects = {e["subject"] for e in doc["items"] if e["area"] == "PROVIDER"}
    assert subjects == {str(entry["id"]) for entry in _providers()}


def test_the_auditor_invents_no_value() -> None:
    """未入力の欄が、そのまま未入力として報告されること。**仮値を作らない。**"""
    package = json.loads(VALUE_PACKAGE.read_text(encoding="utf-8"))
    empty = {entry["name"] for entry in package["fields"] if entry["value"] is None}
    doc = _measure()
    reported = {
        entry["item"]
        for entry in doc["items"]
        if entry["area"] == "OWNER_VALUE" and entry["status"] == "OWNER_VALUE_MISSING"
    }
    assert reported == empty, f"未入力の報告が欄と合わない: {sorted(reported ^ empty)}"
    # 値そのものを Report へ書き写していないこと。
    body = json.dumps(doc, ensure_ascii=False)
    for entry in package["fields"]:
        if entry["value"] is not None:  # pragma: no cover - いまは全欄が空
            assert str(entry["value"]) not in body, f"値が Report へ出ている: {entry['name']}"


def test_the_auditor_cannot_reach_network_or_keyring() -> None:
    """監査器が Network も Keyring も触る道具を持たないこと。"""
    source = AUDITOR.read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:import|from)\s+([\w.]+)", source, re.MULTILINE)
    hit = sorted({name for name in imports if name.split(".")[0] in FORBIDDEN_IMPORTS})
    assert hit == [], f"Network / Keyring へ届く import がある: {hit}"


# ---------------------------------------------------------------------------
# 判定の材料
# ---------------------------------------------------------------------------


def test_generated_and_double_modules_are_excluded_from_evidence() -> None:
    """生成物と試験用の代役が、根拠の Module から外れていること。"""
    doc = _measure()
    index = doc["source_index"]
    assert index["generated_modules"], "生成物を 1 つも認識していない"
    assert index["test_double_modules"], "試験用の代役を 1 つも認識していない"
    assert index["evidence_modules"] < index["production_modules"], "生成物と代役を外していない"


def test_a_symbol_that_is_only_defined_is_not_implemented() -> None:
    """定義があるだけの Symbol を実装済みにしないこと。

    実在する Class を 1 つ選び、**呼ばれていないなら実装済みにならない**ことを見る。
    """
    index = auditor.SymbolIndex.build()
    evidence = index.evidence_modules()
    uncalled = [
        symbol
        for module in sorted(evidence)
        for symbol in sorted(index.defined[module])
        if not (index.where_called(symbol) & evidence)
    ]
    assert uncalled, "呼ばれていない定義が 1 つも無い（前提が崩れている）"
    for symbol in uncalled[:5]:
        status, detail = index.classify(symbol)
        assert status != "IMPLEMENTED", f"{symbol} が呼出なしで実装済みになった: {detail}"


def test_a_symbol_that_exists_only_in_generated_code_is_not_implemented() -> None:
    """生成物にしかない Symbol を実装済みにしないこと。"""
    index = auditor.SymbolIndex.build()
    generated = [name for name in index.modules if index.is_generated(name)]
    assert generated, "生成物が 1 つも無い"
    only_generated = [
        symbol
        for module in generated
        for symbol in sorted(index.defined[module])
        if index.where_defined(symbol) <= set(generated)
    ]
    assert only_generated, "生成物にしかない Symbol が 1 つも無い"
    for symbol in only_generated[:5]:
        status, _ = index.classify(symbol)
        assert status != "IMPLEMENTED", f"{symbol} が生成物だけで実装済みになった"


def test_a_symbol_that_exists_only_in_a_test_double_is_not_implemented() -> None:
    """試験用の代役にしかない Symbol を実装済みにしないこと。"""
    index = auditor.SymbolIndex.build()
    doubles = [name for name in index.modules if index.is_double(name)]
    assert doubles, "試験用の代役が 1 つも無い"
    only_doubles = [
        symbol
        for module in doubles
        for symbol in sorted(index.defined[module])
        if index.where_defined(symbol) <= set(doubles)
    ]
    assert only_doubles, "代役にしかない Symbol が 1 つも無い"
    for symbol in only_doubles[:5]:
        status, _ = index.classify(symbol)
        assert status != "IMPLEMENTED", f"{symbol} が代役だけで実装済みになった"


def test_docstrings_and_comments_are_not_read() -> None:
    """判定が文字列の中身に依存しないこと。

    監査器は AST の呼出と定義だけを見る。**存在しない Symbol は、どこに書かれて
    いても実装済みにならない。**
    """
    index = auditor.SymbolIndex.build()
    # `src/` に定義されていない名前を選ぶ。docstring には現れうる語である。
    for symbol in ("KeyringAdapter", "HttpProviderAdapter", "SecretResolverPort"):
        if index.where_defined(symbol):
            continue
        status, detail = index.classify(symbol)
        assert status == "NOT_IMPLEMENTED", f"{symbol}: {status} / {detail}"


# ---------------------------------------------------------------------------
# Retry と Failover を混ぜない
# ---------------------------------------------------------------------------


def test_retry_and_failover_are_separate_areas() -> None:
    """Retry と Failover が別の領域として分かれていること。

    Retry は同じ Provider への再送、Failover は別 Provider への切替である。
    `route-policy.yaml` は Fallback の可否だけを定める。**Fallback があることを
    Retry 規則があることと読み替えない。**
    """
    doc = _measure()
    areas = {entry["area"] for entry in doc["items"]}
    assert "RETRY" in areas and "FAILOVER" in areas, f"領域が分かれていない: {sorted(areas)}"
    retry_items = [e for e in doc["items"] if e["area"] == "RETRY"]
    failover_items = [e for e in doc["items"] if e["area"] == "FAILOVER"]
    assert retry_items and failover_items
    assert not ({e["item"] for e in retry_items} & {e["item"] for e in failover_items}), (
        "同じ項目名が両方の領域にある"
    )


def test_fallback_lists_do_not_make_retry_configured() -> None:
    """Fallback の一覧があっても Retry の対象が決まったことにしないこと。"""
    document = yaml.safe_load(ROUTE_POLICY.read_text(encoding="utf-8"))
    assert document["fallback_eligible_failures"], "Fallback の一覧が無い（前提が崩れている）"
    assert "retry" not in ROUTE_POLICY.read_text(encoding="utf-8").lower()
    doc = _measure()
    entry = next(e for e in doc["items"] if e["area"] == "RETRY" and "障害 ID" in e["item"])
    assert entry["status"] not in POSITIVE, f"Retry 対象が {entry['status']} になっている"


def test_failover_priority_is_undefined_when_the_field_is_absent() -> None:
    """順位 Field が無いなら順位未定義と報告すること。**列挙順を順位にしない。**"""
    fields = {key for entry in _providers() for key in entry}
    assert "priority" not in fields and "order" not in fields
    doc = _measure()
    entry = next(e for e in doc["items"] if e["area"] == "FAILOVER" and "順位の Field" in e["item"])
    assert entry["status"] == "NOT_IMPLEMENTED", entry


# ---------------------------------------------------------------------------
# 分類の網羅
# ---------------------------------------------------------------------------


def test_every_item_has_a_known_status_and_evidence() -> None:
    """全項目が既知の判定と根拠を持つこと。**未分類 0 件。**"""
    doc = _measure()
    assert doc["items"], "項目が 1 件も無い"
    for entry in doc["items"]:
        assert entry["status"] in auditor.STATUSES, entry
        assert entry["detail"], entry
        assert entry["source"], entry
    keys = [(e["area"], e["subject"], e["item"]) for e in doc["items"]]
    assert len(keys) == len(set(keys)), "同じ項目が重複している"


def test_no_positive_status_rests_on_the_call_graph_today() -> None:
    """Only the actual SecretRef definition and production caller justify a positive status."""
    from dataclasses import fields

    from harness.ports.chatgpt import SecretRef

    doc = _measure()
    positives = [
        entry
        for entry in doc["items"]
        if entry["status"] in POSITIVE and entry["basis"] == "call_graph"
    ]
    assert len(positives) == 1, positives
    item = positives[0]
    assert (item["area"], item["subject"], item["item"]) == (
        "SECRET_REF",
        None,
        "Production から呼べる SecretRef 型",
    )
    assert item["status"] == "IMPLEMENTED"
    assert item["source"] == "src/harness/"
    assert "['harness.ports.chatgpt']" in item["detail"]
    assert "['harness.infrastructure.provider.chatgpt_auth']" in item["detail"]
    assert [field.name for field in fields(SecretRef)] == ["reference"]


def test_the_v1_report_is_referenced_by_hash_not_rewritten(value_chain: Any) -> None:
    """v2 が v1 を Hash で参照し、v1 とは別の File へ出すこと。"""
    v1 = value_chain.readiness_v1
    before = v1.read_bytes()
    doc = _measure()
    assert doc["supersedes"]["path"] == "docs/audit/chat-provider-readiness.json"
    assert doc["supersedes"]["sha256"] == "sha256:" + hashlib.sha256(before).hexdigest()
    assert v1.read_bytes() == before, "測り直しで v1 が動いた"
    assert auditor.OUT_JSON.name != v1.name
    assert auditor.OUT_JSON.parent.name == "audit"
