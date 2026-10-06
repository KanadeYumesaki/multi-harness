"""Provider の状態を **読み取り専用**で見せる。

## 値を作らない

Provider ID も Endpoint も Model も、この Module は 1 つも持たない。すべて
`design-source/registries/route-policy.yaml` と
`docs/decision/DCR-CHAT-PROVIDER-VALUE-INPUT.json` から読む。**書き込まない。**

## 登録されていない Provider を登録済みとして描かない

`route-policy.yaml` に載っているのは `mock` だけである。OpenAI／Anthropic／
Gemini はどの正本にも無い。無いものを「登録済みだが未設定」として並べると、
**画面が正本より先に進む**。ここでは「外部 Provider は 0 件」と述べ、値が
入っていない入力欄の数を添える。

## Web ログインを認証として扱わない

Browser や IDE Plugin の Login 状態は、この Harness が使える API 認証では
ない。Cookie も Session も読まない。読める場所も持たない。

## Secret を映さない

API Key も Secret 値も、資格情報 Store の中身も表示しない。持ってもいない。
SecretRef が **設定済みかどうか**だけを真偽で出す。

## 送ってよいかの判定もここに置く

実行方式と送信可否の判定は、Provider の状態そのものである。**別 Module へ
分けない。** 分ければ Module が 1 つ増え、答え済みの Decision Package が束縛
している Module 数が動く。答えた後の Package を書き換えないために、既にある
場所へ置く。

## 資格情報 Store の名を Code へ書かない

許可条件の名前と説明は `route-policy.yaml` から読む。`SECRET_RESOLVABLE` の
description が具体的な Store 名を述べるが、それは Registry の文言であって
この Module の値ではない。**正本にある語を、正本から読んで出す。**
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final

import yaml

__all__ = [
    "MOCK_PROVIDER_ID",
    "PROPOSE_CAPABILITY",
    "ExecutionMode",
    "ProviderStatusView",
    "ProviderView",
    "RequirementView",
    "SendDecision",
    "evaluate_mock_turn",
    "execution_mode_views",
    "load_provider_status",
]

ROUTE_POLICY = "design-source/registries/route-policy.yaml"
VALUE_PACKAGE = "docs/decision/DCR-CHAT-PROVIDER-VALUE-INPUT.json"

#: API の状態語彙。**この 3 つだけを使う。**
API_AVAILABLE: Final[str] = "AVAILABLE"
API_NOT_CONFIGURED: Final[str] = "NOT_CONFIGURED"
API_NOT_IMPLEMENTED: Final[str] = "NOT_IMPLEMENTED"

#: 入力欄がどの設定項目に当たるか。設問 ID は Package の正本に一致する。
_ENDPOINT_QUESTION: Final[str] = "CVR-2"
_MODEL_QUESTION: Final[str] = "CVR-3"
_SECRET_QUESTIONS: Final[tuple[str, ...]] = ("CVR-7", "CVR-10")


@dataclass(frozen=True, slots=True)
class RequirementView:
    """送信許可条件 1 件の判定。**判定できないものを「満たした」にしない。**"""

    requirement_id: str
    description: str
    met: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ProviderView:
    """1 Provider の読み取り専用の姿。"""

    provider_id: str
    route_class: str
    enabled: bool
    contract_state: str
    contract_may_send: bool
    capabilities: tuple[str, ...]
    endpoint_configured: bool
    model_configured: bool
    secret_ref_configured: bool
    api_status: str
    chat_send_allowed: bool
    note: str
    requirements: tuple[RequirementView, ...]

    def projection(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "route_class": self.route_class,
            "enabled": self.enabled,
            "contract_state": self.contract_state,
            "contract_may_send": self.contract_may_send,
            "capabilities": list(self.capabilities),
            "endpoint_configured": self.endpoint_configured,
            "model_configured": self.model_configured,
            "secret_ref_configured": self.secret_ref_configured,
            "api_status": self.api_status,
            "chat_send_allowed": self.chat_send_allowed,
            "note": self.note,
            "requirements": [
                {
                    "requirement_id": item.requirement_id,
                    "description": item.description,
                    "met": item.met,
                    "reason": item.reason,
                }
                for item in self.requirements
            ],
        }


@dataclass(frozen=True, slots=True)
class ProviderStatusView:
    """画面へ渡す Provider 状態のすべて。"""

    providers: tuple[ProviderView, ...]
    external_provider_count: int
    value_fields_total: int
    value_fields_entered: int
    value_package_status: str
    blocking_reasons: tuple[str, ...]

    @property
    def any_send_allowed(self) -> bool:
        return any(provider.chat_send_allowed for provider in self.providers)

    def projection(self) -> dict[str, Any]:
        return {
            "providers": [provider.projection() for provider in self.providers],
            "external_provider_count": self.external_provider_count,
            "value_fields_total": self.value_fields_total,
            "value_fields_entered": self.value_fields_entered,
            "value_package_status": self.value_package_status,
            "blocking_reasons": list(self.blocking_reasons),
            "any_send_allowed": self.any_send_allowed,
        }


def _configured(fields: list[dict[str, Any]], questions: tuple[str, ...]) -> bool:
    """該当設問の入力欄がすべて埋まっているか。**1 つでも空なら未設定。**"""
    relevant = [field for field in fields if field["question"] in questions]
    if not relevant:
        return False
    return all(field["value"] is not None for field in relevant)


def load_provider_status(repo_root: Path) -> ProviderStatusView:
    """正本 2 つを読んで、いまの Provider 状態を組む。**書き込まない。**"""
    route = yaml.safe_load((repo_root / ROUTE_POLICY).read_text(encoding="utf-8"))
    try:
        package = json.loads((repo_root / VALUE_PACKAGE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ProviderStatusView(
            providers=(),
            external_provider_count=0,
            value_fields_total=0,
            value_fields_entered=0,
            value_package_status="NOT_CONFIGURED",
            blocking_reasons=(
                "旧Ownerの設計回答は未配布です。コード編集はWorkbenchで設定します。",
            ),
        )
    fields: list[dict[str, Any]] = package["fields"]

    may_send_by_state = {entry["id"]: bool(entry["may_send"]) for entry in route["contract_states"]}
    descriptions = {entry["id"]: entry["description"] for entry in route["enablement_requirements"]}
    gate_required = {
        entry["id"]: bool(entry["masking_gate_required"]) for entry in route["route_classes"]
    }

    endpoint_configured = _configured(fields, (_ENDPOINT_QUESTION,))
    model_configured = _configured(fields, (_MODEL_QUESTION,))
    secret_configured = _configured(fields, _SECRET_QUESTIONS)

    providers: list[ProviderView] = []
    for entry in route["providers"]:
        contract_state = str(entry["contract_state"])
        enabled = bool(entry["enabled"])
        capabilities = tuple(str(c) for c in entry.get("capabilities", ()))
        may_send = may_send_by_state.get(contract_state, False)
        requirements = (
            RequirementView(
                "LISTED_IN_ALLOWLIST",
                descriptions["LISTED_IN_ALLOWLIST"],
                True,
                "route-policy.yaml の providers に載っている",
            ),
            RequirementView(
                "EXPLICITLY_ENABLED",
                descriptions["EXPLICITLY_ENABLED"],
                enabled,
                f"enabled={enabled}",
            ),
            RequirementView(
                "CONTRACT_ACTIVE",
                descriptions["CONTRACT_ACTIVE"],
                may_send,
                f"contract_state={contract_state}",
            ),
            RequirementView(
                "CAPABILITY_DECLARED",
                descriptions["CAPABILITY_DECLARED"],
                bool(capabilities),
                f"capabilities={list(capabilities)}",
            ),
            RequirementView(
                "SECRET_RESOLVABLE",
                descriptions["SECRET_RESOLVABLE"],
                False,
                "SecretRef を解決する Adapter が未実装で、参照の値も未入力である",
            ),
            RequirementView(
                "MASKING_GATE_PASSED",
                descriptions["MASKING_GATE_PASSED"],
                not gate_required.get(str(entry["route_class"]), True),
                (
                    "route_class が Masking Gate を要する。送信時に判定する"
                    if gate_required.get(str(entry["route_class"]), True)
                    else "route_class は外部へ出ないため Gate 不要"
                ),
            ),
        )
        # **Chat 送信は 1 件も許さない。** Adapter が無いか、あっても Chat の
        # 応答を作れば架空の Assistant Message になる。
        note = (
            "Adapter は存在するが Chat 応答へ使わない。"
            "使えば架空の Assistant Message を作ることになる"
            if entry["id"] == "mock"
            else "Chat 用 Provider Adapter が未実装である"
        )
        providers.append(
            ProviderView(
                provider_id=str(entry["id"]),
                route_class=str(entry["route_class"]),
                enabled=enabled,
                contract_state=contract_state,
                contract_may_send=may_send,
                capabilities=capabilities,
                endpoint_configured=endpoint_configured,
                model_configured=model_configured,
                secret_ref_configured=secret_configured,
                api_status=API_NOT_IMPLEMENTED,
                chat_send_allowed=False,
                note=note,
                requirements=requirements,
            )
        )

    external = [entry for entry in route["providers"] if entry["route_class"] != "LOCAL_ONLY"]
    entered = [field for field in fields if field["value"] is not None]
    blocking = (
        f"外部 Provider が {len(external)} 件しか登録されていない",
        f"具体値の入力欄が {len(entered)} / {len(fields)} 件しか埋まっていない",
        "SecretRef を解決する Adapter が未実装である",
        "Chat 用 Provider Adapter が未実装である",
    )
    return ProviderStatusView(
        providers=tuple(providers),
        external_provider_count=len(external),
        value_fields_total=len(fields),
        value_fields_entered=len(entered),
        value_package_status=str(package["status"]),
        blocking_reasons=blocking,
    )


# --------------------------------------------------------------------------
# 実行方式と送信可否
# --------------------------------------------------------------------------
ROUTE_POLICY = "design-source/registries/route-policy.yaml"
VALUE_PACKAGE = "docs/decision/DCR-CHAT-PROVIDER-VALUE-INPUT.json"

#: MVP0-A で唯一許される Provider。**`route-policy.yaml` に載っている ID である。**
#:
#: ここで新しい Provider ID を作らない。正本に無い ID を渡されたら拒む。
MOCK_PROVIDER_ID: Final[str] = "mock"

#: 応答を作る Capability。`route-policy.yaml` の `capabilities` にある語である。
PROPOSE_CAPABILITY: Final[str] = "PROPOSE"

#: 外部へ出ない経路区分。`route-policy.yaml` の `route_classes` にある ID である。
LOCAL_ONLY: Final[str] = "LOCAL_ONLY"


class ExecutionMode(Enum):
    """実行方式。**値は Registry へ書かない。画面と API の語彙である。**"""

    MOCK = "MOCK"
    CLI = "CLI"
    API = "API"


@dataclass(frozen=True, slots=True)
class SendDecision:
    """送ってよいかの判定。**理由を必ず持つ。**"""

    allowed: bool
    mode: ExecutionMode
    provider_id: str
    reasons: tuple[str, ...]

    def projection(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "execution_mode": self.mode.value,
            "provider_id": self.provider_id,
            "reasons": list(self.reasons),
        }


def _route_policy(repo_root: Path) -> dict[str, Any]:
    loaded = yaml.safe_load((repo_root / ROUTE_POLICY).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):  # pragma: no cover - 正本が壊れていない限り来ない
        raise ValueError("route-policy.yaml is not a mapping")
    return loaded


def execution_mode_views(repo_root: Path) -> list[dict[str, Any]]:
    """画面へ出す実行方式の一覧。**実装状況を偽らない。**

    `MOCK` だけが動く。`CLI` と `API` は、何が足りないかを述べる。足りないものの
    件数は入力用紙から数える。**「あと少し」と書かない。**
    """
    try:
        package = json.loads((repo_root / VALUE_PACKAGE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [
            {
                "mode": mode.value,
                "implemented": mode is ExecutionMode.MOCK,
                "external_egress": mode is not ExecutionMode.MOCK,
                "note": "旧Chatは未構成です。コード編集CLIはWorkbenchで設定します。",
                "blocking": [] if mode is ExecutionMode.MOCK else ["SETTING_PACKAGE_UNAVAILABLE"],
            }
            for mode in ExecutionMode
        ]
    fields = package["fields"]
    missing = [field for field in fields if field["value"] is None]
    return [
        {
            "mode": ExecutionMode.MOCK.value,
            "implemented": True,
            "external_egress": False,
            "note": ("外部へ出ない決定論の応答を作る。Network・Keyring・subprocess を呼ばない"),
            "blocking": [],
        },
        {
            "mode": ExecutionMode.CLI.value,
            "implemented": False,
            "external_egress": True,
            "note": "外部 CLI の実行方式。Adapter が無い",
            "blocking": [
                "CLI Path の allowlist が未入力である",
                "終了 Code と障害 ID の対応表が正本に無い",
                f"Provider 具体値が {len(missing)} / {len(fields)} 件未入力である",
            ],
        },
        {
            "mode": ExecutionMode.API.value,
            "implemented": False,
            "external_egress": True,
            "note": "外部 API の実行方式。Adapter が無い",
            "blocking": [
                "Endpoint と Model が未入力である",
                "SecretRef を解決する Adapter が未実装である",
                f"Provider 具体値が {len(missing)} / {len(fields)} 件未入力である",
            ],
        },
    ]


def evaluate_mock_turn(repo_root: Path, *, mode: ExecutionMode, provider_id: str) -> SendDecision:
    """Mock の 1 往復を実行してよいかを判定する。

    **外部へ出る経路は 1 つも通さない。** `MOCK` 以外の実行方式は、Adapter が
    無いという理由で必ず止まる。`MOCK` でも、正本の許可条件を満たさなければ
    止まる。
    """
    reasons: list[str] = []

    if mode is not ExecutionMode.MOCK:
        reasons.append(
            f"実行方式 {mode.value} の Adapter が未実装である。外部 Provider へは送らない"
        )
        return SendDecision(False, mode, provider_id, tuple(reasons))

    route = _route_policy(repo_root)
    entries = {str(entry["id"]): entry for entry in route["providers"]}
    entry = entries.get(provider_id)
    if entry is None:
        reasons.append(
            f"provider_id={provider_id} が route-policy.yaml の providers に無い。"
            "正本に無い Provider を実行しない"
        )
        return SendDecision(False, mode, provider_id, tuple(reasons))

    if provider_id != MOCK_PROVIDER_ID:
        reasons.append(
            f"MVP0-A で実行できるのは {MOCK_PROVIDER_ID} だけである。"
            "ほかの Provider は Adapter が無い"
        )

    if not bool(entry["enabled"]):
        reasons.append("enabled が true でない。既定値による有効化を認めない")

    may_send = {str(state["id"]): bool(state["may_send"]) for state in route["contract_states"]}
    contract_state = str(entry["contract_state"])
    if not may_send.get(contract_state, False):
        reasons.append(f"contract_state={contract_state} は送信を許さない")

    capabilities = [str(item) for item in entry.get("capabilities", ())]
    if PROPOSE_CAPABILITY not in capabilities:
        reasons.append(
            f"{PROPOSE_CAPABILITY} が capabilities に無い（capabilities={capabilities}）"
        )

    route_class = str(entry["route_class"])
    if route_class != LOCAL_ONLY:
        reasons.append(f"route_class={route_class} は外部へ出る。Mock の実行方式では通さない")

    egress = {str(item["id"]): bool(item["external_egress"]) for item in route["route_classes"]}
    if egress.get(route_class, True):
        reasons.append(f"route_class={route_class} は外部送出を伴う")

    return SendDecision(not reasons, mode, provider_id, tuple(reasons))
