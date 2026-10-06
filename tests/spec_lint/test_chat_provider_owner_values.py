"""Owner の具体値を記録する経路を固定する試験。

## この Task では値が入っていない

Owner から実値が示されていない。**推測で埋めずに止めた。** ここで測るのは
「値が入っているか」ではなく、**入っていないことが明示されているか** と、
**不正な値を渡したときに確実に止まるか** である。

## 試験の値は正本へ出ない

拒否を測るには何かを渡すしかない。渡すのは `TEST-ONLY-` で始まる明らかな
仮値と、RFC 2606 が予約する `.invalid` Domain だけである。すべて `tmp_path`
の中で死ぬ。正本へ 1 つも出ていないことを試験自身が測る。

## 通ることではなく通らないことを測る

Provider ID の重複、Failover 順位の同点、上限超過、単位の欠落、正本に無い
障害 ID、Secret 本体、欄の書き換え。**どれも Package を 1 Byte も動かさずに
止まる。**

## 保存 Package を読まない

保存 Package（`DCR-CHAT-PROVIDER-VALUE-INPUT` ほか）は Owner の回答記録であり、
公開用の配布コピーに収録しない。ここでは合成の上流から **正規の生成器** が組んだ
Package（`tests/support/synthetic_value_package.py`）を記録器へ渡す。保存 Package が
未入力のまま止まっていることは `tests/private_history/` の試験が確かめる。
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
RECORDER = REPO_ROOT / "tools/record_chat_provider_values.py"
BUILDER = REPO_ROOT / "tools/build_chat_provider_value_input.py"
#: 記録器へ渡す Package。**合成の Package へ差し替える**（下の Fixture）。
PKG = REPO_ROOT / "docs/decision/DCR-CHAT-PROVIDER-VALUE-INPUT.json"
ROUTE_POLICY = REPO_ROOT / "design-source/registries/route-policy.yaml"

#: 試験だけで使う仮値の印。正本へ出ていないことを測る。
TEST_MARK = "TEST-ONLY-"

#: Network も Keyring も触らない。触る道具を import していないことを測る。
FORBIDDEN_IMPORTS = ("socket", "requests", "urllib", "httpx", "http.client", "keyring", "ssl")


def _load_tool(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rec = _load_tool(RECORDER, "record_chat_provider_values")


@pytest.fixture(scope="session")
def value_chain(tmp_path_factory: pytest.TempPathFactory) -> Any:
    from synthetic_value_package import build_value_chain

    return build_value_chain(tmp_path_factory.mktemp("value-chain") / "root")


@pytest.fixture(autouse=True)
def synthetic_package(
    value_chain: Any, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Module の `PKG` を合成の Package へ向ける。保存 Package は開かない。"""
    monkeypatch.setattr(request.module, "PKG", value_chain.package)


def _pkg() -> dict[str, Any]:
    return json.loads(PKG.read_text(encoding="utf-8"))


def _field(pkg: dict[str, Any], question: str, name_part: str) -> dict[str, Any]:
    hits = [f for f in pkg["fields"] if f["question"] == question and name_part in f["name"]]
    assert len(hits) == 1, f"{question}／{name_part} が {len(hits)} 件"
    return hits[0]


def _canon() -> list[str]:
    route = yaml.safe_load(ROUTE_POLICY.read_text(encoding="utf-8"))
    failures = route["fallback_eligible_failures"] + route["fallback_forbidden_failures"]
    return [entry["id"] for entry in failures]


PROVIDERS = ["TEST-ONLY-ALPHA", "TEST-ONLY-BRAVO"]


def _endpoint(host: str) -> dict[str, str]:
    """試験用の Endpoint。`.invalid` は RFC 2606 が予約し、決して解決しない。"""
    return {
        "Scheme": "TEST-ONLY-SCHEME",
        "Host": host,
        "Port": "443",
        "Path Pattern": "/test-only",
        "Auth Host": host,
        "Proxy": "TEST-ONLY-NONE",
        "DNS": "TEST-ONLY-DNS",
        "TLS": "TEST-ONLY-TLS",
        "Redirect": "TEST-ONLY-DENY",
        "Certificate Validation": "TEST-ONLY-ENFORCED",
        "Tenant Scope": "TEST-ONLY-TENANT",
    }


def _valid_values(pkg: dict[str, Any]) -> dict[str, Any]:
    """全欄を埋めた仮の入力。**正本へは書かない。**"""
    hosts = {p: f"{p.lower()}.invalid" for p in PROVIDERS}
    return {
        _field(pkg, "CVR-1", "Provider ID")["field_id"]: list(PROVIDERS),
        _field(pkg, "CVR-2", "Endpoint")["field_id"]: {p: _endpoint(hosts[p]) for p in PROVIDERS},
        _field(pkg, "CVR-3", "Model ID")["field_id"]: {
            p: [f"TEST-ONLY-MODEL-{index}"] for index, p in enumerate(PROVIDERS, start=1)
        },
        _field(pkg, "CVR-5", "順位")["field_id"]: dict(zip(PROVIDERS, [1, 2], strict=True)),
        _field(pkg, "CVR-7", "account")["field_id"]: "TEST-ONLY-ACCOUNT-FORMAT",
        _field(pkg, "CVR-10", "接頭辞")["field_id"]: "TEST-ONLY-PREFIX",
        _field(pkg, "CVR-10", "区切り文字")["field_id"]: ":",
        _field(pkg, "CVR-13", "障害 ID")["field_id"]: ["PROVIDER_TIMEOUT"],
        _field(pkg, "CVR-16", "Retry 回数")["field_id"]: dict(zip(PROVIDERS, [1, 2], strict=True)),
        _field(pkg, "CVR-16", "Timeout")["field_id"]: dict(zip(PROVIDERS, [30, 30], strict=True)),
        _field(pkg, "CVR-16", "単位")["field_id"]: "TEST-ONLY-UNIT",
        _field(pkg, "CVR-16", "上限")["field_id"]: 60,
    }


def _doc(pkg: dict[str, Any], values: dict[str, Any] | None = None) -> dict[str, Any]:
    doc = rec.template_from(pkg)
    for field_id, value in (values or {}).items():
        doc["values"][field_id]["value"] = value
    return doc


def _reject(
    tmp_path: Path, doc: dict[str, Any], code: str, pkg: dict[str, Any] | None = None
) -> None:
    """不正な入力を渡し、止まることと Package が 1 Byte も動かないことを測る。"""
    pkg_copy = tmp_path / "pkg.json"
    pkg_copy.write_bytes(
        PKG.read_bytes()
        if pkg is None
        else (json.dumps(pkg, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    before = pkg_copy.read_bytes()
    values_file = tmp_path / "values.json"
    values_file.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(rec.ValueRejected) as excinfo:
        rec.main(
            [
                "--pkg",
                str(pkg_copy),
                "--values",
                str(values_file),
                "--status-out",
                str(tmp_path / "status.json"),
                "--status-md",
                str(tmp_path / "status.md"),
            ]
        )
    assert excinfo.value.code == code, str(excinfo.value)
    assert pkg_copy.read_bytes() == before, "拒否したのに Package が動いた"


# ---------------------------------------------------------------------------
# 8 項目
# ---------------------------------------------------------------------------


def test_keyring_derivation_is_not_idempotency(tmp_path: Path) -> None:
    """`CVR-10` は Keyring service 名の導出式であること。分類の書き換えを拒む。"""
    pkg = _pkg()
    prefix = _field(pkg, "CVR-10", "接頭辞")
    assert prefix["category"] == "SECRET_REF"
    doc = _doc(pkg, _valid_values(pkg))
    doc["values"][prefix["field_id"]]["category"] = "IDEMPOTENCY"
    _reject(tmp_path, doc, "FIELD_METADATA_CHANGED")


def test_settled_question_cannot_be_reopened(tmp_path: Path) -> None:
    """既決の設問へ値を寄越されたら止まること。"""
    pkg = _pkg()
    pkg["fields"][0]["question"] = "CVR-19"
    doc = _doc(pkg)
    _reject(tmp_path, doc, "SETTLED_QUESTION_NOT_OPEN", pkg=pkg)


# ---------------------------------------------------------------------------
# 拒否
# ---------------------------------------------------------------------------


def test_duplicate_provider_id_is_rejected(tmp_path: Path) -> None:
    pkg = _pkg()
    values = _valid_values(pkg)
    values[_field(pkg, "CVR-1", "Provider ID")["field_id"]] = [PROVIDERS[0], PROVIDERS[0]]
    _reject(tmp_path, _doc(pkg, values), "DUPLICATED_ITEM")


def test_duplicate_failover_rank_is_rejected(tmp_path: Path) -> None:
    pkg = _pkg()
    values = _valid_values(pkg)
    values[_field(pkg, "CVR-5", "順位")["field_id"]] = dict.fromkeys(PROVIDERS, 1)
    _reject(tmp_path, _doc(pkg, values), "DUPLICATED_RANK")


def test_provider_key_mismatch_is_rejected(tmp_path: Path) -> None:
    pkg = _pkg()
    values = _valid_values(pkg)
    ranks = values[_field(pkg, "CVR-5", "順位")["field_id"]]
    values[_field(pkg, "CVR-5", "順位")["field_id"]] = {"TEST-ONLY-CHARLIE": 1, PROVIDERS[0]: 2}
    assert ranks != values[_field(pkg, "CVR-5", "順位")["field_id"]]
    _reject(tmp_path, _doc(pkg, values), "PROVIDER_KEY_MISMATCH")


def test_per_provider_value_without_provider_list_is_rejected(tmp_path: Path) -> None:
    pkg = _pkg()
    values = _valid_values(pkg)
    del values[_field(pkg, "CVR-1", "Provider ID")["field_id"]]
    _reject(tmp_path, _doc(pkg, values), "PROVIDER_LIST_REQUIRED")


@pytest.mark.parametrize(
    ("broken", "code"),
    [
        ({"Host": "https://test-only.invalid"}, "ENDPOINT_MALFORMED"),
        ({"Host": "test-only.invalid/v1"}, "ENDPOINT_MALFORMED"),
        ({"Port": "70000"}, "ENDPOINT_MALFORMED"),
        ({"Port": "https"}, "ENDPOINT_MALFORMED"),
        ({"Path Pattern": "test-only"}, "ENDPOINT_MALFORMED"),
    ],
)
def test_malformed_endpoint_is_rejected(tmp_path: Path, broken: dict[str, str], code: str) -> None:
    """Endpoint の形だけを見て落とす。**接続確認はしない。**"""
    pkg = _pkg()
    values = _valid_values(pkg)
    endpoints = copy.deepcopy(values[_field(pkg, "CVR-2", "Endpoint")["field_id"]])
    endpoints[PROVIDERS[0]].update(broken)
    values[_field(pkg, "CVR-2", "Endpoint")["field_id"]] = endpoints
    _reject(tmp_path, _doc(pkg, values), code)


def test_missing_endpoint_field_is_rejected(tmp_path: Path) -> None:
    pkg = _pkg()
    values = _valid_values(pkg)
    endpoints = copy.deepcopy(values[_field(pkg, "CVR-2", "Endpoint")["field_id"]])
    del endpoints[PROVIDERS[0]]["Tenant Scope"]
    values[_field(pkg, "CVR-2", "Endpoint")["field_id"]] = endpoints
    _reject(tmp_path, _doc(pkg, values), "ENDPOINT_FIELD_MISSING")


def test_unknown_endpoint_field_is_rejected(tmp_path: Path) -> None:
    pkg = _pkg()
    values = _valid_values(pkg)
    endpoints = copy.deepcopy(values[_field(pkg, "CVR-2", "Endpoint")["field_id"]])
    endpoints[PROVIDERS[0]]["TEST-ONLY-EXTRA"] = "x"
    values[_field(pkg, "CVR-2", "Endpoint")["field_id"]] = endpoints
    _reject(tmp_path, _doc(pkg, values), "ENDPOINT_FIELD_UNKNOWN")


def test_value_over_shared_cap_is_rejected(tmp_path: Path) -> None:
    """共通上限を超えた個別値で止まること。CVR-16-B の「上限で縛る」を測る。"""
    pkg = _pkg()
    values = _valid_values(pkg)
    cap = values[_field(pkg, "CVR-16", "上限")["field_id"]]
    values[_field(pkg, "CVR-16", "Timeout")["field_id"]] = dict.fromkeys(PROVIDERS, cap + 1)
    _reject(tmp_path, _doc(pkg, values), "EXCEEDS_SHARED_CAP")


def test_missing_unit_is_rejected(tmp_path: Path) -> None:
    """単位の無い Retry 値を受け取らないこと。"""
    pkg = _pkg()
    values = _valid_values(pkg)
    del values[_field(pkg, "CVR-16", "単位")["field_id"]]
    _reject(tmp_path, _doc(pkg, values), "UNIT_MISSING")


def test_missing_shared_cap_is_rejected(tmp_path: Path) -> None:
    pkg = _pkg()
    values = _valid_values(pkg)
    del values[_field(pkg, "CVR-16", "上限")["field_id"]]
    _reject(tmp_path, _doc(pkg, values), "SHARED_CAP_REQUIRED")


def test_unit_holding_a_quantity_is_rejected(tmp_path: Path) -> None:
    """`30 秒` は単位ではなく値である。単位欄へ数量を書かせない。"""
    pkg = _pkg()
    values = _valid_values(pkg)
    values[_field(pkg, "CVR-16", "単位")["field_id"]] = "30 TEST-ONLY-UNIT"
    _reject(tmp_path, _doc(pkg, values), "UNIT_NOT_A_UNIT")


def test_failure_id_outside_canon_is_rejected(tmp_path: Path) -> None:
    """正本に無い障害 ID を受け取らないこと。"""
    pkg = _pkg()
    values = _valid_values(pkg)
    values[_field(pkg, "CVR-13", "障害 ID")["field_id"]] = ["TEST-ONLY-FAILURE"]
    _reject(tmp_path, _doc(pkg, values), "FAILURE_ID_NOT_IN_CANON")


def test_allowed_values_drift_is_rejected(tmp_path: Path) -> None:
    """Package の候補が `route-policy.yaml` から離れたら、値を見る前に止まること。"""
    pkg = _pkg()
    field = _field(pkg, "CVR-13", "障害 ID")
    for entry in pkg["fields"]:
        if entry["field_id"] == field["field_id"]:
            entry["constraints"]["allowed_values"] = ["TEST-ONLY-FAILURE"]
    _reject(tmp_path, _doc(pkg), "ALLOWED_VALUES_DRIFTED", pkg=pkg)


@pytest.mark.parametrize(
    "secret",
    [
        "sk-TESTONLYTESTONLYTESTONLYTESTONLY",
        "Bearer TESTONLY",
        "ghp_TESTONLYTESTONLY",
        "A" * 44,
    ],
)
def test_secret_shaped_value_is_rejected(tmp_path: Path, secret: str) -> None:
    """Secret 本体を受け取らないこと。**保存もしない。**"""
    pkg = _pkg()
    values = _valid_values(pkg)
    values[_field(pkg, "CVR-7", "account")["field_id"]] = secret
    _reject(tmp_path, _doc(pkg, values), "SECRET_VALUE_REJECTED")


def test_secret_named_key_is_rejected(tmp_path: Path) -> None:
    """Secret らしい Key 名を受け取らないこと。"""
    pkg = _pkg()
    doc = _doc(pkg, _valid_values(pkg))
    endpoint_field = _field(pkg, "CVR-2", "Endpoint")["field_id"]
    doc["values"][endpoint_field]["value"][PROVIDERS[0]]["api_key"] = "x"
    _reject(tmp_path, doc, "SECRET_VALUE_REJECTED")


def test_extra_top_level_key_is_rejected(tmp_path: Path) -> None:
    """回答以外の Field を入力 File へ足させないこと。"""
    doc = _doc(_pkg())
    doc["TEST-ONLY-EXTRA"] = 1
    _reject(tmp_path, doc, "NOT_A_VALUE_FIELD")


def test_extra_entry_key_is_rejected(tmp_path: Path) -> None:
    pkg = _pkg()
    doc = _doc(pkg)
    doc["values"][pkg["fields"][0]["field_id"]]["TEST-ONLY-EXTRA"] = 1
    _reject(tmp_path, doc, "NOT_A_VALUE_FIELD")


def test_unknown_field_id_is_rejected(tmp_path: Path) -> None:
    doc = _doc(_pkg())
    doc["values"]["F-testonly"] = {
        "question": "CVR-1",
        "name": "x",
        "category": "PROVIDER",
        "shape": "TEXT",
        "value": None,
    }
    _reject(tmp_path, doc, "UNKNOWN_FIELD")


def test_truncated_input_is_rejected(tmp_path: Path) -> None:
    pkg = _pkg()
    doc = _doc(pkg)
    doc["values"].pop(pkg["fields"][0]["field_id"])
    _reject(tmp_path, doc, "FIELD_MISSING")


def test_input_for_another_package_is_rejected(tmp_path: Path) -> None:
    doc = _doc(_pkg())
    doc["package_id"] = "TEST-ONLY-OTHER"
    _reject(tmp_path, doc, "PACKAGE_MISMATCH")


def test_boolean_is_not_an_integer(tmp_path: Path) -> None:
    """`True` を 1 として受け取らないこと。"""
    pkg = _pkg()
    values = _valid_values(pkg)
    values[_field(pkg, "CVR-16", "上限")["field_id"]] = True
    _reject(tmp_path, _doc(pkg, values), "TYPE_MISMATCH")


def test_zero_retry_count_is_allowed_but_negative_is_not(tmp_path: Path) -> None:
    """範囲を検査していること。Retry 回数は 0 以上である。"""
    pkg = _pkg()
    values = _valid_values(pkg)
    values[_field(pkg, "CVR-16", "Retry 回数")["field_id"]] = dict.fromkeys(PROVIDERS, -1)
    _reject(tmp_path, _doc(pkg, values), "OUT_OF_RANGE")


# ---------------------------------------------------------------------------
# 受け取れる入力
# ---------------------------------------------------------------------------


def test_valid_values_are_recorded_without_touching_anything_else(tmp_path: Path) -> None:
    """正しい値を渡すと記録され、**値以外は 1 つも動かない**こと。

    書く先は `tmp_path` である。正本の Package は触らない。
    """
    pkg_copy = tmp_path / "pkg.json"
    pkg_copy.write_bytes(PKG.read_bytes())
    pkg = _pkg()
    values_file = tmp_path / "values.json"
    values_file.write_text(
        json.dumps(_doc(pkg, _valid_values(pkg)), ensure_ascii=False), encoding="utf-8"
    )
    code = rec.main(
        [
            "--pkg",
            str(pkg_copy),
            "--values",
            str(values_file),
            "--status-out",
            str(tmp_path / "status.json"),
            "--status-md",
            str(tmp_path / "status.md"),
        ]
    )
    assert code == 0, "全欄が埋まったのに止まった"

    after = json.loads(pkg_copy.read_text(encoding="utf-8"))
    assert after["status"] == "ANSWERED"
    assert after["unanswered"] == []
    assert all(f["value"] is not None for f in after["fields"])

    before = _pkg()
    frozen = {k: v for k, v in before.items() if k not in rec.MUTABLE_TOP_LEVEL}
    assert frozen == {k: v for k, v in after.items() if k not in rec.MUTABLE_TOP_LEVEL}
    for old, new in zip(before["fields"], after["fields"], strict=True):
        assert {k: v for k, v in old.items() if k != "value"} == {
            k: v for k, v in new.items() if k != "value"
        }

    report = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert report["measured"]["questions_answered"] == 8
    assert report["unanswered_questions"] == []


def test_template_round_trip_keeps_entered_values(tmp_path: Path) -> None:
    """用紙を刷り直しても入力済みの値が消えないこと。"""
    pkg = _pkg()
    filled = copy.deepcopy(pkg)
    values = _valid_values(pkg)
    for field in filled["fields"]:
        field["value"] = values[field["field_id"]]
    reissued = rec.template_from(filled)
    for field in filled["fields"]:
        assert reissued["values"][field["field_id"]]["value"] == field["value"]


def test_rebuild_refuses_to_erase_entered_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """値が入った Package を組み直しで空へ戻せないこと。"""
    builder = _load_tool(BUILDER, "build_chat_provider_value_input")
    filled = _pkg()
    values = _valid_values(filled)
    for field in filled["fields"]:
        field["value"] = values[field["field_id"]]
    target = tmp_path / "pkg.json"
    target.write_text(json.dumps(filled, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setattr(builder, "OUT", target)
    with pytest.raises(SystemExit) as excinfo:
        builder.main(["--out", str(tmp_path / "other.json")])
    assert "VALUES_ALREADY_ENTERED" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 触れていないこと
# ---------------------------------------------------------------------------


def test_recorder_cannot_reach_network_or_keyring() -> None:
    """記録器が Network も Keyring も触る道具を持たないこと。"""
    source = RECORDER.read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:import|from)\s+([\w.]+)", source, re.MULTILINE)
    hit = sorted({name for name in imports if name.split(".")[0] in FORBIDDEN_IMPORTS})
    assert hit == [], f"Network / Keyring へ届く import がある: {hit}"
    assert "subprocess" not in imports, "外部 Process を起こしている"


# ---------------------------------------------------------------------------
# 生成直後の Package（合成の上流から正規の生成器で組んだもの）
# ---------------------------------------------------------------------------


def test_a_freshly_built_package_records_no_value() -> None:
    """生成器が値を 1 つも作らないこと。未回答が欄と一致すること。"""
    pkg = _pkg()
    assert pkg["status"] == "DECISION_REQUIRED"
    assert pkg["answers"] == {}
    assert [f["name"] for f in pkg["fields"] if f["value"] is not None] == []
    assert pkg["unanswered"] == [f["field_id"] for f in pkg["fields"]]


def test_status_report_is_derived_from_the_package(tmp_path: Path) -> None:
    """未回答が設問単位と欄単位の両方で、Package から導かれて明示されること。"""
    pkg_copy = tmp_path / "pkg.json"
    pkg_copy.write_bytes(PKG.read_bytes())
    code = rec.main(
        [
            "--pkg",
            str(pkg_copy),
            "--status-out",
            str(tmp_path / "status.json"),
            "--status-md",
            str(tmp_path / "status.md"),
        ]
    )
    assert code == 3, "未入力なのに止まらなかった"
    pkg = _pkg()
    report = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    questions = sorted({f["question"] for f in pkg["fields"]}, key=lambda q: int(q.split("-")[1]))
    assert report["measured"] == {
        "questions_total": len(questions),
        "questions_answered": 0,
        "fields_total": len(pkg["fields"]),
        "fields_answered": 0,
    }
    # 設問番号の昇順。文字列順ではない（`CVR-10` は `CVR-2` より後ろに来る）。
    assert report["unanswered_questions"] == questions
    assert report["package_id"] == pkg["package_id"]
    assert report["design_sha256"] == pkg["design_sha256"]
    assert report["failure_id_canon"] == _canon()
    assert pkg_copy.read_bytes() == PKG.read_bytes(), "状態の報告で Package が動いた"


def test_a_fresh_template_holds_no_values() -> None:
    """入力用紙に値が 1 つも無いこと。**既定値も例も置かない。**"""
    template = rec.template_from(_pkg())
    assert template["values"]
    assert all(entry["value"] is None for entry in template["values"].values())
    body = json.dumps(template, ensure_ascii=False)
    assert not re.search(r"[a-z]+://", body), "URL が書いてある"
    assert not re.search(r"\b[a-z0-9][a-z0-9-]*\.(com|net|org|io|ai)\b", body), "Host 名がある"


def test_settled_questions_have_no_input_field() -> None:
    """既決の設問が入力欄を持たないこと。"""
    present = {f["question"] for f in _pkg()["fields"]} & set(rec.SETTLED_QUESTIONS)
    assert not present, f"既決の設問が入力欄になっている: {sorted(present)}"


def test_test_only_placeholders_never_reach_the_public_canon() -> None:
    """試験の仮値が正本へ 1 つも出ていないこと。"""
    scanned = [ROUTE_POLICY, REPO_ROOT / "registry-snapshot.json"]
    scanned += sorted((REPO_ROOT / "design-source/registries").glob("*.yaml"))
    leaked = [p.name for p in scanned if TEST_MARK in p.read_text(encoding="utf-8")]
    assert leaked == [], f"仮値が正本へ出ている: {leaked}"


# ---------------------------------------------------------------------------
# 記録値の再検査（run-checks.sh の owner_values--check）
# ---------------------------------------------------------------------------


def test_check_accepts_the_package_when_it_is_valid(capsys: pytest.CaptureFixture[str]) -> None:
    assert rec.main(["--pkg", str(PKG), "--check"]) == 0
    assert "CHECK_OK" in capsys.readouterr().out


def test_check_reports_an_absent_package_as_unset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """回答 Package が無い初期状態は、検査する記録値が無いと明示して止まること。"""
    assert rec.main(["--pkg", str(tmp_path / "absent.json"), "--check"]) == 0
    assert "OWNER_VALUES_UNSET" in capsys.readouterr().out


def test_check_does_not_hide_a_broken_package_as_unset(tmp_path: Path) -> None:
    """壊れた Package を未設定に見せないこと。**File の欠落だけ**が未設定である。"""
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        rec.main(["--pkg", str(broken), "--check"])


def test_check_does_not_treat_a_dangling_link_as_unset(tmp_path: Path) -> None:
    link = tmp_path / "dangling.json"
    link.symlink_to(tmp_path / "missing-target.json")
    with pytest.raises(FileNotFoundError):
        rec.main(["--pkg", str(link), "--check"])


def test_recording_still_requires_the_package(tmp_path: Path) -> None:
    """未設定の扱いは再検査だけである。記録と用紙の出力は Package を要求する。"""
    with pytest.raises(FileNotFoundError):
        rec.main(["--pkg", str(tmp_path / "absent.json"), "--emit-template", str(tmp_path / "t")])
