"""Provider 具体値の入力支援を固定する試験。

## この Task では値が 1 つも入らない

Owner から実値が示されていない。**推測で埋めずに 12 欄すべてを null のまま保つ。**
ここで測るのは「入力できるか」ではなく、**入力していないものを入力済みにしないか**
である。

## 何を測るか

* 12 欄を Package から導いていること（手入力していないこと）
* 11 件の `pending_owner_values` と 1 件の条件漏れで 12 になる説明が保たれること
* Owner が確認していない値を保存しないこと
* 保存が記録器だけを通ること
* 用途別の希望が Active Policy にならないこと
* 仮値・API Key・Cookie・CLI Session を拒むこと
* Provider 未設定のまま `any_send_allowed=false` と 409 を保つこと

## 通ることではなく通らないことを測る

確認の無い保存、仮 Provider ID、Cookie の混入、順位への自動変換。**どれも
Package を 1 Byte も動かさずに止まる。**

## 保存 Package を読まない

回答 Package・用途別の提案は Owner の記録であり、公開用の配布コピーに収録しない。
ここでは合成の上流から **正規の生成器** が組んだ Package と合成の提案
（`tests/support/synthetic_value_package.py`）を、Production と同じ読込み経路へ渡す。
保存 Package が未入力のまま止まっていることは `tests/private_history/` が確かめる。
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from harness.presentation.local_ui.api import LocalUiApi, Request
from harness.presentation.local_ui.composition import LocalUiServices, build_services
from harness.presentation.local_ui.server import load_assets, new_session_token
from harness.presentation.local_ui.value_input import (
    ConfirmationRejected,
    build_interview,
    confirmed_values,
    load_route_profile_proposal,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
UI_DIR = REPO_ROOT / "src/harness/presentation/local_ui"
PACKAGE = REPO_ROOT / "docs/decision/DCR-CHAT-PROVIDER-VALUE-INPUT.json"
ANSWERS = REPO_ROOT / "docs/decision/DCR-CHAT-PROVIDER-VALUES.json"
PROPOSAL = REPO_ROOT / "docs/decision/OWNER-ROUTE-PROFILE-PROPOSAL.json"
ROUTE_POLICY = REPO_ROOT / "design-source/registries/route-policy.yaml"

ORIGIN = "http://127.0.0.1:65535"
TOKEN = "d" * 64


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


@pytest.fixture(scope="session")
def value_chain(tmp_path_factory: pytest.TempPathFactory) -> Any:
    from synthetic_value_package import build_value_chain

    return build_value_chain(tmp_path_factory.mktemp("ui-value-chain") / "root")


@pytest.fixture(autouse=True)
def synthetic_decision_files(
    value_chain: Any,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """読込み先を合成の File の写しへ向ける。**試験ごとに写すので、互いに汚さない。**

    Production は `repo_root / 相対 Path` で読む。絶対 Path を渡せばそちらが選ばれる。
    保存 Package のある木でも、それを開かない。
    """
    from harness.infrastructure import owner_value_recorder as recorder
    from harness.presentation.local_ui import providers, value_input

    copies = tmp_path_factory.mktemp("ui-value-copy")
    package = copies / "value-input.json"
    answers = copies / "values.json"
    proposal = copies / "proposal.json"
    package.write_bytes(value_chain.package.read_bytes())
    answers.write_bytes(value_chain.answers.read_bytes())
    proposal.write_bytes(value_chain.proposal.read_bytes())
    monkeypatch.setattr(recorder, "PACKAGE_RELATIVE", str(package))
    monkeypatch.setattr(value_input, "ANSWERS_RELATIVE", str(answers))
    monkeypatch.setattr(value_input, "PROPOSAL_RELATIVE", str(proposal))
    monkeypatch.setattr(providers, "VALUE_PACKAGE", str(package))
    monkeypatch.setattr(request.module, "PACKAGE", package)
    monkeypatch.setattr(request.module, "ANSWERS", answers)
    monkeypatch.setattr(request.module, "PROPOSAL", proposal)


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


def _get(api: LocalUiApi, path: str) -> tuple[int, Any]:
    response = api.handle(Request(method="GET", path=path, headers={}))
    return response.status, json.loads(response.body)


def _post(api: LocalUiApi, path: str, body: dict[str, Any]) -> tuple[int, Any]:
    headers = {"origin": ORIGIN, "x-harness-session": TOKEN, "content-type": "application/json"}
    response = api.handle(
        Request(
            method="POST",
            path=path,
            headers=headers,
            body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        )
    )
    return response.status, json.loads(response.body)


def _package() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(PACKAGE.read_text(encoding="utf-8"))
    return document


# ---------------------------------------------------------------------------
# 欄を手入力しない
# ---------------------------------------------------------------------------


def test_fields_come_from_the_package() -> None:
    """入力欄が Package の `fields` と一致すること。**画面が欄を作らない。**"""
    interview = build_interview(REPO_ROOT)
    package = _package()
    assert [step["field_id"] for step in interview["steps"]] == [
        entry["field_id"] for entry in package["fields"]
    ]
    assert interview["measured"]["fields_total"] == len(package["fields"])


def test_the_field_count_is_explained_by_the_package() -> None:
    """11 と 12 の差が Package の中で説明できること。

    `pending_owner_values` は 11 件で、入力欄は 12 件ある。差の 1 件は記録済みの
    条件漏れ（`CVR-16 上限`）である。**説明できなくなったら落ちる。**
    """
    interview = build_interview(REPO_ROOT)
    derivation = interview["derivation"]
    answers = json.loads(ANSWERS.read_text(encoding="utf-8"))
    pending = sum(len(names) for names in answers["pending_owner_values"].values())
    added = len(_package()["condition_gaps"]["added_as_field"])
    assert derivation["pending_owner_values"] == pending
    assert derivation["added_as_field"] == added
    assert derivation["fields"] == pending + added == len(_package()["fields"])


def _code_string_literals(path: Path) -> list[str]:
    """Code に現れる文字列 Literal。**docstring は除く。**

    文書が欄名を説明するのは、欄を作ったことではない。見たいのは実行される値。
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


def test_no_field_name_is_hard_coded_in_the_ui() -> None:
    """UI の Code が欄名を持たないこと。**Package から読むこと。**

    docstring が欄名を説明するのは違反ではない。実行される文字列だけを見る。
    """
    names = {entry["name"] for entry in _package()["fields"]}
    for path in sorted(UI_DIR.rglob("*.py")):
        # **完全一致で見る。** 「上限」のような普通の語が説明文へ出るのは、
        # 欄名を持っていることではない。
        literals = set(_code_string_literals(path))
        leaked = sorted(names & literals)
        assert leaked == [], f"{path.name} が欄名を持っている: {leaked}"


def test_the_interview_has_the_nine_parts() -> None:
    """1 欄ごとに 9 つの見出しが揃っていること。"""
    interview = build_interview(REPO_ROOT)
    for step in interview["steps"]:
        for key in (
            "name",
            "purpose",
            "current_state",
            "shape_hint",
            "recommendation",
            "confirmed",
            "checks",
        ):
            assert step[key] is not None, (step["field_id"], key)
        assert step["shows_example_value"] is False, "入力例を値として出している"


# ---------------------------------------------------------------------------
# null を保つ
# ---------------------------------------------------------------------------


def test_all_twelve_fields_are_still_null() -> None:
    """12 欄すべてが未入力のままであること。"""
    package = _package()
    assert len(package["fields"]) == 12
    assert all(entry["value"] is None for entry in package["fields"])
    interview = build_interview(REPO_ROOT)
    assert interview["measured"]["fields_missing"] == 12
    assert interview["measured"]["fields_present"] == 0
    assert interview["measured"]["fields_confirmed"] == 0


def test_the_api_reports_the_same_state(api: LocalUiApi) -> None:
    status, payload = _get(api, "/api/provider-values")
    assert status == 200
    assert payload["measured"]["fields_missing"] == 12
    assert payload["recorder"]["module"] == "harness.infrastructure.owner_value_recorder"


# ---------------------------------------------------------------------------
# 確認していない値を保存しない
# ---------------------------------------------------------------------------


def _reject(body: dict[str, Any], code: str) -> None:
    before = PACKAGE.read_bytes()
    with pytest.raises(ConfirmationRejected) as excinfo:
        confirmed_values(
            REPO_ROOT, body.get("confirmations"), approve_save=bool(body.get("approve_save"))
        )
    assert excinfo.value.code == code, str(excinfo.value)
    assert PACKAGE.read_bytes() == before, "拒否したのに Package が動いた"


def test_saving_without_approval_is_rejected() -> None:
    _reject({"confirmations": {}, "approve_save": False}, "SAVE_NOT_APPROVED")


def test_saving_without_confirmation_is_rejected() -> None:
    _reject({"confirmations": {}, "approve_save": True}, "OWNER_CONFIRMATION_MISSING")


def test_a_partially_confirmed_set_is_rejected() -> None:
    """1 欄でも未確認なら渡さないこと。"""
    package = _package()
    first = package["fields"][0]["field_id"]
    _reject(
        {"confirmations": {first: {"confirmed": True, "value": []}}, "approve_save": True},
        "OWNER_CONFIRMATION_MISSING",
    )


def test_confirmed_without_a_value_is_rejected() -> None:
    package = _package()
    body = {
        "confirmations": {entry["field_id"]: {"confirmed": True} for entry in package["fields"]},
        "approve_save": True,
    }
    _reject(body, "CONFIRMED_WITHOUT_VALUE")


def test_an_unknown_field_is_rejected() -> None:
    _reject(
        {"confirmations": {"F-notafield": {"confirmed": True, "value": 1}}, "approve_save": True},
        "UNKNOWN_FIELD",
    )


def test_extra_keys_in_a_confirmation_are_rejected() -> None:
    package = _package()
    first = package["fields"][0]["field_id"]
    _reject(
        {
            "confirmations": {first: {"confirmed": True, "value": [], "source": "env"}},
            "approve_save": True,
        },
        "NOT_A_CONFIRMATION_FIELD",
    )


@pytest.mark.parametrize(
    "material",
    [
        {"cookie": "sessionid=abc"},
        {"session_token": "x"},
        {"note": "sessionid=deadbeef"},
        {"note": "cli login で認証済み"},
        {"note": "already logged-in"},
    ],
)
def test_session_material_is_rejected(material: dict[str, str]) -> None:
    """Cookie も CLI の Login も設定として受け取らないこと。"""
    package = _package()
    first = package["fields"][0]["field_id"]
    confirmations: dict[str, Any] = {
        entry["field_id"]: {"confirmed": True, "value": []} for entry in package["fields"]
    }
    confirmations[first] = {"confirmed": True, "value": material}
    _reject({"confirmations": confirmations, "approve_save": True}, "SESSION_MATERIAL_REJECTED")


def test_the_api_refuses_and_leaves_the_package_untouched(api: LocalUiApi) -> None:
    before = PACKAGE.read_bytes()
    status, payload = _post(
        api, "/api/provider-values", {"confirmations": {}, "approve_save": True}
    )
    assert status == 422, payload
    assert payload["error"]["code"] == "OWNER_CONFIRMATION_MISSING"
    assert PACKAGE.read_bytes() == before


def test_unknown_request_fields_are_rejected(api: LocalUiApi) -> None:
    status, payload = _post(api, "/api/provider-values", {"values": {}})
    assert status == 400
    assert "values" in payload["error"]["reason"]


# ---------------------------------------------------------------------------
# 保存経路は記録器だけ
# ---------------------------------------------------------------------------


def test_the_ui_never_writes_the_package_itself() -> None:
    """UI の Code が Package へ直接書かないこと。

    書くのは `owner_value_recorder.save_values` だけである。
    """
    for path in sorted(UI_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        writes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            # `mkdir` は保存先の用意である。Package を書くこととは別。
            and node.func.attr in {"write_text", "write_bytes"}
        ]
        assert writes == [], f"{path.name} が File を書いている（{len(writes)} 箇所）"
        opens = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "open"
        ]
        assert opens == [], f"{path.name} が open() を呼んでいる"


def test_the_recorder_is_the_only_writer() -> None:
    """Package を書く箇所が記録器の 1 つだけであること。"""
    core = REPO_ROOT / "src/harness/infrastructure/owner_value_recorder.py"
    source = core.read_text(encoding="utf-8")
    writes = re.findall(r"\.write_text\(", source)
    assert len(writes) == 1, f"記録器の書込みが {len(writes)} 箇所ある"
    assert "def save_values(" in source


# ---------------------------------------------------------------------------
# 用途別の希望
# ---------------------------------------------------------------------------


def test_route_profile_is_a_proposal_only(api: LocalUiApi) -> None:
    """用途別の希望が Active Policy にならないこと。"""
    status, payload = _get(api, "/api/route-profile")
    assert status == 200
    assert payload["status"] == "ROUTE_PROFILE_PROPOSAL_ONLY"
    assert payload["active_route_policy"] is False
    assert payload["owner_decision_required"] is True
    assert payload["usages"], "提案が 1 件も無い"


def test_proposed_providers_are_not_in_the_canon() -> None:
    """提案の Provider が正本に登録されていないと表示されること。"""
    route = yaml.safe_load(ROUTE_POLICY.read_text(encoding="utf-8"))
    registered = {str(entry["id"]).lower() for entry in route["providers"]}
    profile = load_route_profile_proposal(REPO_ROOT)
    for entry in profile["usages"]:
        hint = entry["preferred_provider_hint"].lower()
        assert hint not in registered, f"{hint} が正本に登録されている"
        assert entry["canonical_provider_registry"] == "未登録"
        assert entry["owner_proposal"] == "未確定"
        assert entry["execution"] == "無効"


def test_the_proposal_does_not_reach_the_registry() -> None:
    """提案が Registry へ書き戻されていないこと。"""
    route_text = ROUTE_POLICY.read_text(encoding="utf-8")
    proposal = json.loads(PROPOSAL.read_text(encoding="utf-8"))
    for entry in proposal["usages"]:
        assert entry["preferred_provider_hint"] not in route_text
        assert entry["registered_in_canon"] is False
    assert proposal["active_route_policy"] is False
    assert proposal["owner_decision_required"] is True


def test_the_proposal_is_not_a_failover_order() -> None:
    """列挙順が順位として保存されていないこと。"""
    route = yaml.safe_load(ROUTE_POLICY.read_text(encoding="utf-8"))
    fields = {key for entry in route["providers"] for key in entry}
    assert "priority" not in fields and "order" not in fields
    proposal = json.loads(PROPOSAL.read_text(encoding="utf-8"))
    body = json.dumps(proposal, ensure_ascii=False)
    for word in ("priority", "failover_order", "rank"):
        assert word not in body, f"提案が順位を持っている: {word}"


# ---------------------------------------------------------------------------
# 送信は止まったまま
# ---------------------------------------------------------------------------


def test_send_is_still_refused(api: LocalUiApi) -> None:
    status, payload = _get(api, "/api/providers")
    assert status == 200
    assert payload["any_send_allowed"] is False
    assert payload["value_fields_entered"] == 0

    status, payload = _post(api, "/api/chat/send", {})
    assert status == 409
    assert payload["network_used"] is False
    assert payload["assistant_message_created"] is False


def test_the_ui_shows_configured_and_ready_to_send_separately() -> None:
    """`CONFIGURED` を `READY_TO_SEND` として出さないこと。"""
    source = (UI_DIR / "static/app.js").read_text(encoding="utf-8")
    assert "READY_TO_SEND: " in source
    assert "chat_send_allowed" in source
    # 送信可否は `chat_send_allowed` だけから作る。設定状態から作らない。
    ready_line = next(line for line in source.splitlines() if "READY_TO_SEND: " in line)
    assert "chat_send_allowed" in ready_line


def test_the_new_screens_use_no_inner_html() -> None:
    for name in ("app.js", "index.html"):
        source = (UI_DIR / "static" / name).read_text(encoding="utf-8")
        body = "\n".join(re.sub(r"//.*$", "", line) for line in source.splitlines())
        for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval("):
            assert forbidden not in body, f"{name}: {forbidden}"


# ---------------------------------------------------------------------------
# UI の経路を通した検証
# ---------------------------------------------------------------------------


def _all_confirmed(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """全 12 欄を確認済みにした入力。**値は TEST-ONLY で、正本へは出ない。**"""
    package = _package()
    providers = ["TEST-ONLY-ALPHA", "TEST-ONLY-BRAVO"]
    endpoint = {
        "Scheme": "TEST-ONLY-SCHEME",
        "Host": "test-only.invalid",
        "Port": "443",
        "Path Pattern": "/test-only",
        "Auth Host": "test-only.invalid",
        "Proxy": "TEST-ONLY-NONE",
        "DNS": "TEST-ONLY-DNS",
        "TLS": "TEST-ONLY-TLS",
        "Redirect": "TEST-ONLY-DENY",
        "Certificate Validation": "TEST-ONLY-ENFORCED",
        "Tenant Scope": "TEST-ONLY-TENANT",
    }
    by_shape: dict[str, Any] = {
        "LIST": list(providers),
        "PER_PROVIDER_OBJECT": dict.fromkeys(providers, endpoint),
        "PER_PROVIDER_LIST": {p: ["TEST-ONLY-MODEL"] for p in providers},
        "PER_PROVIDER_VALUE": dict(zip(providers, [1, 2], strict=True)),
        "FORMAT_RULE": "TEST-ONLY-FORMAT",
        "TEXT": "TEST-ONLY-TEXT",
        "SHARED_CAP": 60,
    }
    confirmations: dict[str, Any] = {}
    for entry in package["fields"]:
        value = by_shape[entry["shape"]]
        if entry["constraints"].get("allowed_values"):
            value = [entry["constraints"]["allowed_values"][0]]
        confirmations[entry["field_id"]] = {"confirmed": True, "value": value}
    for field_id, value in (overrides or {}).items():
        confirmations[field_id] = {"confirmed": True, "value": value}
    return confirmations


def _field_id(question: str, part: str) -> str:
    hits = [
        entry["field_id"]
        for entry in _package()["fields"]
        if entry["question"] == question and part in entry["name"]
    ]
    assert len(hits) == 1, f"{question}／{part} が {len(hits)} 件"
    return hits[0]


def _recorder_rejects(confirmations: dict[str, Any], code: str) -> None:
    """UI が組んだ Document を記録器へ渡し、落ちることと Package 不変を測る。"""
    from harness.infrastructure import owner_value_recorder as recorder

    before = PACKAGE.read_bytes()
    document = confirmed_values(REPO_ROOT, confirmations, approve_save=True)
    package = recorder.load_json(PACKAGE)
    canon = recorder.failure_ids(REPO_ROOT)
    with pytest.raises(recorder.ValueRejected) as excinfo:
        recorder.validate(package, document, canon)
    assert excinfo.value.code == code, str(excinfo.value)
    assert PACKAGE.read_bytes() == before, "拒否したのに Package が動いた"


def test_a_confirmed_flag_of_false_is_not_a_confirmation() -> None:
    """値だけ書いて確認していない欄を、保存しないこと。

    **「値があるから確認した」ではない。** 確認は Owner が明示する。
    """
    confirmations = _all_confirmed()
    target = _package()["fields"][0]["field_id"]
    confirmations[target] = {"confirmed": False, "value": ["TEST-ONLY-ALPHA"]}
    _reject({"confirmations": confirmations, "approve_save": True}, "OWNER_CONFIRMATION_MISSING")


def test_an_api_key_shaped_value_is_rejected_through_the_ui_path() -> None:
    """API Key の形をした値を、UI から渡しても落ちること。"""
    target = _field_id("CVR-7", "account")
    _recorder_rejects(
        _all_confirmed({target: "sk-TESTONLYTESTONLYTESTONLYTESTONLY"}),
        "SECRET_VALUE_REJECTED",
    )


def test_a_failure_id_outside_the_canon_is_rejected_through_the_ui_path() -> None:
    """正本に無い障害 ID を、UI から渡しても落ちること。"""
    target = _field_id("CVR-13", "障害 ID")
    _recorder_rejects(_all_confirmed({target: ["TEST-ONLY-FAILURE"]}), "FAILURE_ID_NOT_IN_CANON")


def test_a_fabricated_endpoint_is_rejected_through_the_ui_path() -> None:
    """仮 Endpoint の形の崩れを、UI から渡しても落ちること。"""
    target = _field_id("CVR-2", "Endpoint")
    broken = {
        "Scheme": "TEST-ONLY-SCHEME",
        "Host": "https://test-only.invalid",
        "Port": "443",
        "Path Pattern": "/test-only",
        "Auth Host": "test-only.invalid",
        "Proxy": "TEST-ONLY-NONE",
        "DNS": "TEST-ONLY-DNS",
        "TLS": "TEST-ONLY-TLS",
        "Redirect": "TEST-ONLY-DENY",
        "Certificate Validation": "TEST-ONLY-ENFORCED",
        "Tenant Scope": "TEST-ONLY-TENANT",
    }
    _recorder_rejects(
        _all_confirmed({target: {"TEST-ONLY-ALPHA": broken, "TEST-ONLY-BRAVO": broken}}),
        "ENDPOINT_MALFORMED",
    )


def test_a_web_or_cli_login_is_not_treated_as_secret_resolution(api: LocalUiApi) -> None:
    """Web や CLI の Login を SecretRef 解決済みとして扱わないこと。

    `SECRET_RESOLVABLE` は Adapter が無い限り満たされない。**Login していることは
    Harness が使える認証ではない。**
    """
    status, payload = _get(api, "/api/providers")
    assert status == 200
    for provider in payload["providers"]:
        requirement = next(
            item
            for item in provider["requirements"]
            if item["requirement_id"] == "SECRET_RESOLVABLE"
        )
        assert requirement["met"] is False, f"{provider['provider_id']}: {requirement}"
        assert "未実装" in requirement["reason"], requirement


def test_test_only_values_never_reach_the_canon() -> None:
    """試験の仮値が正本へ 1 つも出ていないこと。"""
    for path in (PACKAGE, PROPOSAL, ROUTE_POLICY, ANSWERS):
        assert "TEST-ONLY-" not in path.read_text(encoding="utf-8"), path.name
