"""具体値の入力 Package を固定する試験。

## この Task は値を入れていない

`DCR-CHAT-PROVIDER-VALUE-INPUT` を作っただけである。ここで測るのは
**入力欄が空のまま揃っているか** と **値を捏造していないか** である。

* 8 件の Value Required 設問がすべて入力欄になっている
* 入力欄の `value` が全件 `null`、既定値も無い
* 条件漏れが同義語と欠落へ分類され、取りこぼしが無い
* 参照した Hash（回答 Package・方式決定・監査）を保持している
* 入力済みの Package を組み直せない
* 入力欄に URL・Host 名・仮の数量が無い

## 通ることではなく通らないことを測る

サンプル Provider を 1 つ書いた瞬間、既定値を 1 つ置いた瞬間に落ちる。

## 保存 Package を読まない

上流（方式決定・回答・棚卸し）と保存 Package は Owner の回答・監査記録であり、
公開用の配布コピーに収録しない。ここでは合成の上流から正規の生成器が組んだ
Package（`tests/support/synthetic_value_package.py`）で生成器の規則を測る。
保存 Package と棚卸しの中身は `tests/private_history/` の試験が確かめる。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

import chat_canon_binding

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILDER = REPO_ROOT / "tools/build_chat_provider_value_input.py"
#: 以下 4 つは下の Fixture が合成の写しへ向ける。**保存 Package を開かない。**
PKG = REPO_ROOT / "docs/decision/DCR-CHAT-PROVIDER-VALUE-INPUT.json"
INVENTORY = REPO_ROOT / "docs/audit/chat-provider-concrete-values.json"
ANSWERS = REPO_ROOT / "docs/decision/DCR-CHAT-PROVIDER-VALUES.json"
METHOD = REPO_ROOT / "docs/decision/DCR-CHAT-PROVIDER-CONFIG.json"
ROUTE_POLICY = REPO_ROOT / "design-source/registries/route-policy.yaml"

_URL = re.compile(r"[a-z]+://")
_HOSTNAME = re.compile(r"\b[a-z0-9][a-z0-9-]*\.[a-z]{2,}\b")
_FABRICATED_VALUE = re.compile(
    r"\d+\s*(秒|分|ミリ秒|ms)(?![a-z])"
    r"|既定[はがを]?\s*[「『]?\d+"
    r"|\d+\s*(回|件|個)\s*(まで|とする|に設定|を既定)"
)


@pytest.fixture(scope="session")
def value_chain(tmp_path_factory: pytest.TempPathFactory) -> Any:
    from synthetic_value_package import build_value_chain

    return build_value_chain(tmp_path_factory.mktemp("value-input-chain") / "root")


@pytest.fixture(autouse=True)
def synthetic_chain(
    value_chain: Any, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    monkeypatch.setattr(request.module, "PKG", value_chain.package)
    monkeypatch.setattr(request.module, "INVENTORY", value_chain.inventory)
    monkeypatch.setattr(request.module, "ANSWERS", value_chain.answers)
    monkeypatch.setattr(request.module, "METHOD", value_chain.method)


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _pkg() -> dict:
    return json.loads(PKG.read_text(encoding="utf-8"))


def _inventory() -> dict:
    return json.loads(INVENTORY.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 入力欄が空のまま揃っている
# ---------------------------------------------------------------------------


def test_every_field_is_empty() -> None:
    """**値が 1 つも入っていないこと。** 既定値も置かないこと。"""
    package = _pkg()
    assert package["status"] == "DECISION_REQUIRED", package["status"]
    assert package["answers"] == {}, package["answers"]
    for field in package["fields"]:
        assert field["value"] is None, field["name"]
        assert "default" not in field, field["name"]
        assert "default" not in field["constraints"], field["name"]
    assert package["unanswered"] == [f["field_id"] for f in package["fields"]]


def test_every_value_required_question_has_a_field() -> None:
    """監査が挙げた設問がすべて入力欄になっていること。**取りこぼさない。**"""
    inventory = _inventory()
    expected = {item["question"] for item in inventory["items"]}
    covered = {field["question"] for field in _pkg()["fields"]}
    assert covered == expected, f"差: {sorted(covered ^ expected)}"
    assert len(expected) == inventory["measured_counts"]["value_required_questions"]


def test_field_ids_and_names_are_unique() -> None:
    fields = _pkg()["fields"]
    ids = [f["field_id"] for f in fields]
    names = [f"{f['question']}/{f['name']}" for f in fields]
    assert len(ids) == len(set(ids)), "field_id が重複している"
    assert len(names) == len(set(names)), "入力欄名が重複している"


def test_each_field_carries_only_validation_rules() -> None:
    """検査規則だけを持ち、値を持たないこと。"""
    allowed_shapes = {
        "LIST",
        "TEXT",
        "FORMAT_RULE",
        "PER_PROVIDER_LIST",
        "PER_PROVIDER_VALUE",
        "PER_PROVIDER_OBJECT",
        "SHARED_CAP",
    }
    for field in _pkg()["fields"]:
        assert field["shape"] in allowed_shapes, field["shape"]
        assert field["constraints"], field["name"]
        assert field["note"], field["name"]
        assert field["question"] and field["answer"], field["name"]


def test_enumerated_domain_comes_from_the_canon() -> None:
    """列挙できる入力欄は、正本の値だけを許すこと。

    Retry 可能な障害 ID は `route-policy.yaml` にある ID からしか選べない。
    **この試験が候補を持たない。** Registry から読む。
    """
    route = yaml.safe_load(ROUTE_POLICY.read_text(encoding="utf-8"))
    failures = route["fallback_eligible_failures"] + route["fallback_forbidden_failures"]
    ids = [entry["id"] for entry in failures]
    enumerated = [f for f in _pkg()["fields"] if "allowed_values" in f["constraints"]]
    assert enumerated, "列挙型の入力欄が 1 つも無い"
    for field in enumerated:
        assert field["constraints"]["allowed_values"] == ids, field["name"]


# ---------------------------------------------------------------------------
# 条件漏れを取りこぼしていない
# ---------------------------------------------------------------------------


def test_condition_gaps_are_all_classified() -> None:
    """条件漏れが同義語と欠落へ分けられ、件数が合うこと。

    **黙って落とさない。** 分類の合計が実測と一致する。
    """
    package = _pkg()
    gaps = package["condition_gaps"]
    measured = gaps["measured"]
    assert measured == _inventory()["condition_gaps"]
    classified = len(gaps["resolved_as_synonym"]) + len(gaps["added_as_field"])
    assert classified == len(measured), f"{classified} != {len(measured)}"
    supplies = {item["question"]: item["recorded_supplies"] for item in _inventory()["items"]}
    for entry in gaps["resolved_as_synonym"]:
        assert entry["resolves_to"] in supplies[entry["question"]], entry
        assert entry["reason"], entry
    for entry in gaps["added_as_field"]:
        assert entry["reason"], entry
        assert any(
            f["question"] == entry["question"] and f["name"] == entry["subject"]
            for f in package["fields"]
        ), f"欠落 {entry} が入力欄になっていない"


# ---------------------------------------------------------------------------
# 参照した Hash を保持している
# ---------------------------------------------------------------------------


def test_referenced_packages_are_held_by_hash() -> None:
    package = _pkg()
    assert package["answer_package"]["sha256"] == _sha(ANSWERS)
    assert package["method_decision"]["sha256"] == _sha(METHOD)
    assert package["inventory"]["sha256"] == _sha(INVENTORY)
    answers = json.loads(ANSWERS.read_text(encoding="utf-8"))
    assert answers["status"] == "ANSWERED" and not answers["unanswered"]
    assert package["answer_package"]["answers"] == {
        qid: a["choice_id"] for qid, a in answers["answers"].items()
    }


def test_bound_to_the_answer_time_canon(tmp_path: Path) -> None:
    """合成Package/Reportを実Git履歴へ束縛する。私的な旧回答の正当性は主張しない。"""
    from synthetic_history_fixture import build_bound_audit_fixture

    root, package, inventory = build_bound_audit_fixture(tmp_path / "bound-audit")
    assert package["design_version"] == inventory["design_version"]
    for document, name in ((package, "package"), (inventory, "inventory")):
        resolved = chat_canon_binding.verify_answer_time_canon(root, document, package_id=name)
        assert resolved["design"]["resolved_hash"] == document["design_sha256"], name
        assert resolved["design"]["design_version"] == document["design_version"], name
        assert resolved["snapshot"]["matches"] is True, name


# ---------------------------------------------------------------------------
# 値を捏造していない
# ---------------------------------------------------------------------------


def test_no_fabricated_value_in_the_package() -> None:
    """入力欄の説明に URL・Host 名・仮の数量が無いこと。"""
    package = _pkg()
    blob = "\n".join(
        field["name"] + field["note"] + json.dumps(field["constraints"], ensure_ascii=False)
        for field in package["fields"]
    )
    assert not _URL.search(blob), "入力欄に URL がある"
    known_files = {p.name for p in (REPO_ROOT / "design-source/registries").glob("*.yaml")}
    hosts = sorted(set(_HOSTNAME.findall(blob)) - known_files)
    assert not hosts, f"入力欄に Host 名らしき語がある: {hosts}"
    found = _FABRICATED_VALUE.search(blob)
    assert not found, f"入力欄に仮値がある: {found.group() if found else ''}"


def test_builder_does_not_hardcode_values() -> None:
    """生成器が Provider ID や Endpoint を持っていないこと。

    正本にある Provider ID が Script へ現れたら、それは一覧の手入力である。
    """
    route = yaml.safe_load(ROUTE_POLICY.read_text(encoding="utf-8"))
    values = {entry["id"] for entry in route["providers"]}
    for entry in route["providers"]:
        values |= set(entry.get("capabilities") or [])
    # 棚卸しの監査器（上流）の同じ検査は、監査器と一緒に非公開側に置いた。
    source = BUILDER.read_text(encoding="utf-8")
    leaked = sorted(value for value in values if f'"{value}"' in source)
    assert not leaked, f"{BUILDER.name} が正本の値を手入力している: {leaked}"


# ---------------------------------------------------------------------------
# 入力済みの値を消さない
# ---------------------------------------------------------------------------


def test_builder_refuses_to_erase_entered_values(tmp_path: Path) -> None:
    """入力済みの値がある Package を組み直せないこと。

    再生成で未入力欄が復活しても、入力済みの値が消えたら意味が無い。
    **消せないことを測る。** 合成の Root で正規の生成器を動かす。
    """
    from synthetic_value_package import build_value_chain, load_builder

    chain = build_value_chain(tmp_path / "chain")
    package = chain.package_document()
    package["fields"][0]["value"] = ["placeholder-for-this-test-only"]
    chain.package.write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    before = chain.package.read_bytes()
    builder = load_builder(chain, "value_builder_erase_check")
    other = tmp_path / "other.json"
    with pytest.raises(SystemExit) as excinfo:
        builder.main(["--out", str(other)])
    assert "VALUES_ALREADY_ENTERED" in str(excinfo.value)
    assert chain.package.read_bytes() == before
    assert not other.exists(), "拒否したのに組み直した"
