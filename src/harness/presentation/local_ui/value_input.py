"""Owner の具体値を、会話形式で確かめながら入れてもらう。

## 欄を手入力しない

12 欄は `DCR-CHAT-PROVIDER-VALUE-INPUT.json` の `fields` から導く。設問名でも
画面文言でも過去の Report でもない。

その `fields` 自体、回答 Package の `pending_owner_values`（11 件）と、記録済みの
条件漏れ `condition_gaps.added_as_field`（1 件、`CVR-16 上限`）から機械的に
作られている。**11 と 12 の差はこの 1 件で、Package の中に理由ごと残っている。**
差が説明できなくなったら止める。

## 確認していない値を保存しない

各欄は「Owner が明示的に確認した」と言われるまで `null` のままである。入力例も
推奨方針も **画面に出すだけ**で、値として扱わない。確認の無い欄が 1 つでもあれば
保存しない。

## 保存経路は 1 つ

検査と書込みは `harness.infrastructure.owner_value_recorder` が行う。この Module
は Package へ 1 Byte も書かない。**同じ検査を通らない保存経路を作らない。**

## Secret を受け取らない

API Key、Cookie、Session Token、CLI の Login 情報はこの画面の入力欄ではない。
それらしい形が来たら、記録器へ渡す前に落とす。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from harness.infrastructure import owner_value_recorder as recorder

__all__ = [
    "ANSWERS_RELATIVE",
    "PROPOSAL_RELATIVE",
    "ConfirmationRejected",
    "InterviewStep",
    "build_interview",
    "confirmed_values",
    "load_route_profile_proposal",
]

#: 欄の出所を説明する回答 Package。欄数の説明（`pending_owner_values`）だけに使う。
ANSWERS_RELATIVE = "docs/decision/DCR-CHAT-PROVIDER-VALUES.json"

#: Owner の用途別の希望。**正本ではない。** 提案として表示するだけである。
PROPOSAL_RELATIVE = "docs/decision/OWNER-ROUTE-PROFILE-PROPOSAL.json"

#: Web の Login や CLI の Session を設定として受け取らない。Key 名の側。
_SESSION_KEY = re.compile(
    r"(cookie|session[_-]?id|session[_-]?token|csrf|refresh[_-]?token|auth[_-]?header)",
    re.IGNORECASE,
)

#: 同上。値の側。Browser の Cookie 文字列と CLI の Login 記述を落とす。
_SESSION_VALUE = re.compile(
    r"(^|;\s*)(__Secure-|__Host-|sessionid=|csrftoken=|_ga=)"
    r"|\blogged[_ -]?in\b"
    r"|\bcli[ _-](login|session|credential)",
    re.IGNORECASE,
)

#: 画面に出す入力例。**値ではない。** 保存対象にも既定値にもしない。
#: 形を伝えるためだけの記述で、Provider ID も Endpoint も Model も含まない。
_SHAPE_HINTS: Final[dict[str, str]] = {
    "LIST": "1 件以上の並び。重複は許さない",
    "PER_PROVIDER_OBJECT": "Provider ID を Key にした Object。11 項目すべてが要る",
    "PER_PROVIDER_LIST": "Provider ID を Key にした並び",
    "PER_PROVIDER_VALUE": "Provider ID を Key にした 1 つの値",
    "FORMAT_RULE": "形式を述べる文字列",
    "TEXT": "文字列",
    "SHARED_CAP": "全 Provider へ効く 1 つの整数",
}

#: 欄ごとの「何に使う値か」。**Package の分類から引く。** 欄名から作らない。
_CATEGORY_PURPOSE: Final[dict[str, str]] = {
    "PROVIDER": "どの Provider へ、どこへ、どの Model で送るかを決める",
    "SECRET_REF": "資格情報の参照の形と、その参照名の作り方を決める",
    "ERROR_STATE": "どの障害を再送の対象とみなすかを決める",
    "RETRY_TIMEOUT": "再送の回数と待ち時間、その上限を決める",
}


class ConfirmationRejected(Exception):
    """確認の形が受け取れない。**Package を書き換えずに止める。**"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class InterviewStep:
    """1 欄ぶんの問いかけ。**値を持たない。**"""

    field_id: str
    question: str
    name: str
    category: str
    shape: str
    answer: str
    purpose: str
    current_state: str
    shape_hint: str
    recommendation: str
    checks: dict[str, Any]
    confirmed: bool
    derived_from: str

    def projection(self) -> dict[str, Any]:
        return {
            "field_id": self.field_id,
            "question": self.question,
            "name": self.name,
            "category": self.category,
            "shape": self.shape,
            "answer": self.answer,
            "purpose": self.purpose,
            "current_state": self.current_state,
            "shape_hint": self.shape_hint,
            "recommendation": self.recommendation,
            "checks": self.checks,
            "confirmed": self.confirmed,
            "derived_from": self.derived_from,
            "shows_example_value": False,
        }


def _derivation(package: dict[str, Any], answers: dict[str, Any]) -> dict[str, Any]:
    """12 欄がどこから来たかを、Package の中身だけで説明する。

    説明できなくなったら止める。**「たぶんこの 12 件」で進めない。**
    """
    pending = answers.get("pending_owner_values") or {}
    pending_pairs = {(question, name) for question, names in pending.items() for name in names}
    field_pairs = {(entry["question"], entry["name"]) for entry in package["fields"]}
    added = {
        (entry["question"], entry["subject"])
        for entry in package.get("condition_gaps", {}).get("added_as_field", [])
    }
    unexplained = sorted(field_pairs - pending_pairs - added)
    lost = sorted(pending_pairs - field_pairs)
    if unexplained or lost:
        raise ConfirmationRejected(
            "FIELD_DERIVATION_BROKEN",
            f"欄の出所を説明できない（説明不能 {unexplained}／消えた {lost}）",
        )
    return {
        "pending_owner_values": len(pending_pairs),
        "added_as_field": len(added),
        "fields": len(field_pairs),
        "explanation": (
            f"回答 Package の `pending_owner_values` {len(pending_pairs)} 件と、"
            f"記録済みの条件漏れ `condition_gaps.added_as_field` {len(added)} 件で "
            f"{len(field_pairs)} 欄になる。**画面が欄を作っていない**"
        ),
    }


def _state_of(entry: dict[str, Any]) -> str:
    return "OWNER_VALUE_PRESENT" if entry["value"] is not None else "OWNER_VALUE_MISSING"


def build_interview(
    repo_root: Path, *, confirmations: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Package から会話の段取りを組む。**値も欄も作らない。**"""
    try:
        package = recorder.load_json(repo_root / recorder.PACKAGE_RELATIVE)
        answers = recorder.load_json(repo_root / ANSWERS_RELATIVE)
    except FileNotFoundError:
        return {
            "package_id": None,
            "package_status": "NOT_CONFIGURED",
            "steps": [],
            "derivation": {
                "explanation": (
                    "旧Ownerの設計回答は未配布です。"
                    "コード編集のモデル・推論設定を使用してください。"
                )
            },
            "measured": {
                "inventory_available": False,
                "fields_total": 0,
                "fields_present": 0,
                "fields_missing": 0,
                "fields_confirmed": 0,
            },
            "save_allowed": False,
            "not_done": ["設計回答を新しい利用者の設定として作成・再利用しません"],
        }
    derivation = _derivation(package, answers)
    confirmed = confirmations or {}

    steps: list[InterviewStep] = []
    for entry in package["fields"]:
        constraints = dict(entry["constraints"])
        allowed = constraints.get("allowed_values")
        recommendation = (
            f"正本の候補（{len(allowed)} 件）から選ぶ。**新しい語を作らない**"
            if allowed
            else "正本に無い値を作らない。分からなければ保留する"
        )
        steps.append(
            InterviewStep(
                field_id=entry["field_id"],
                question=entry["question"],
                name=entry["name"],
                category=entry["category"],
                shape=entry["shape"],
                answer=entry["answer"],
                purpose=_CATEGORY_PURPOSE.get(entry["category"], "分類が未知である"),
                current_state=_state_of(entry),
                shape_hint=_SHAPE_HINTS.get(entry["shape"], "形が未知である"),
                recommendation=recommendation,
                checks=constraints,
                confirmed=bool(confirmed.get(entry["field_id"], {}).get("confirmed", False)),
                derived_from=derivation["explanation"],
            )
        )

    missing = [step for step in steps if step.current_state == "OWNER_VALUE_MISSING"]
    return {
        "package_id": package["package_id"],
        "package_status": package["status"],
        "derivation": derivation,
        "steps": [step.projection() for step in steps],
        "measured": {
            "fields_total": len(steps),
            "fields_present": len(steps) - len(missing),
            "fields_missing": len(missing),
            "fields_confirmed": sum(1 for step in steps if step.confirmed),
        },
        "save_allowed": False if missing else None,
        "not_done": [
            "入力例も推奨方針も、値として保存しない",
            "Owner が確認していない欄は null のままにする",
            "API Key・Cookie・Session Token をこの画面で受け取らない",
            "Package へはこの Module から 1 Byte も書かない",
        ],
    }


def _reject_session_material(node: Any, where: str) -> None:
    """Web の Login や CLI の Session を設定として受け取らない。"""
    if isinstance(node, dict):
        for key, value in node.items():
            if _SESSION_KEY.search(str(key)):
                raise ConfirmationRejected(
                    "SESSION_MATERIAL_REJECTED", f"{where}.{key} は Session の名である"
                )
            _reject_session_material(value, f"{where}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _reject_session_material(value, f"{where}[{index}]")
    elif isinstance(node, str) and _SESSION_VALUE.search(node):
        raise ConfirmationRejected(
            "SESSION_MATERIAL_REJECTED", f"{where} が Cookie か Login 情報の形をしている"
        )


def confirmed_values(repo_root: Path, confirmations: Any, *, approve_save: bool) -> dict[str, Any]:
    """Owner が確認した値だけを、記録器へ渡す形へ組む。

    **確認の無い欄は含めない。** 1 欄でも未確認なら組まずに止める。値の妥当性は
    記録器が見る。ここで見るのは「Owner が確かに確認したか」だけである。
    """
    if not approve_save:
        raise ConfirmationRejected("SAVE_NOT_APPROVED", "Owner が保存を承認していない")
    if not isinstance(confirmations, dict):
        raise ConfirmationRejected("MALFORMED_CONFIRMATION", "確認が Object ではない")

    try:
        package = recorder.load_json(repo_root / recorder.PACKAGE_RELATIVE)
    except FileNotFoundError as error:
        raise ConfirmationRejected(
            "SETTING_PACKAGE_UNAVAILABLE", "旧Ownerの設計回答は未配布です"
        ) from error
    by_id = {entry["field_id"]: entry for entry in package["fields"]}
    unknown = sorted(set(confirmations) - set(by_id))
    if unknown:
        raise ConfirmationRejected("UNKNOWN_FIELD", f"Package に無い欄がある: {unknown}")

    _reject_session_material(confirmations, "confirmations")

    document = recorder.template_from(package)
    unconfirmed: list[str] = []
    for field_id, entry in by_id.items():
        given = confirmations.get(field_id)
        if not isinstance(given, dict):
            unconfirmed.append(entry["name"])
            continue
        surplus = sorted(set(given) - {"confirmed", "value"})
        if surplus:
            raise ConfirmationRejected("NOT_A_CONFIRMATION_FIELD", f"{field_id} に余分: {surplus}")
        if given.get("confirmed") is not True:
            unconfirmed.append(entry["name"])
            continue
        if "value" not in given:
            raise ConfirmationRejected(
                "CONFIRMED_WITHOUT_VALUE", f"{entry['name']} は確認済みだが値が無い"
            )
        document["values"][field_id]["value"] = given["value"]

    if unconfirmed:
        raise ConfirmationRejected(
            "OWNER_CONFIRMATION_MISSING",
            f"{len(unconfirmed)} 欄が未確認である（{'／'.join(unconfirmed)}）",
        )
    return document


def load_route_profile_proposal(repo_root: Path) -> dict[str, Any]:
    """用途別の希望。**提案であって正本ではない。**

    正本に載っていない Provider を「登録済み」として出さない。載っていないことを
    毎回 `route-policy.yaml` から測り直して添える。
    """
    import yaml

    try:
        proposal = recorder.load_json(repo_root / PROPOSAL_RELATIVE)
    except FileNotFoundError:
        return {
            "status": "NOT_CONFIGURED",
            "active_route_policy": False,
            "owner_decision_required": True,
            "usages": [],
            "what_this_is_not": ["旧Ownerの用途別提案は未配布です。実行設定ではありません"],
        }
    route = yaml.safe_load((repo_root / recorder.ROUTE_POLICY_RELATIVE).read_text(encoding="utf-8"))
    registered = {str(entry["id"]).lower() for entry in route["providers"]}

    usages: list[dict[str, Any]] = []
    for entry in proposal["usages"]:
        hint = str(entry["preferred_provider_hint"])
        in_canon = hint.lower() in registered
        usages.append(
            {
                "usage": entry["usage"],
                "preferred_provider_hint": hint,
                "canonical_provider_registry": "登録済み" if in_canon else "未登録",
                "owner_proposal": "未確定",
                "execution": "無効",
            }
        )
    return {
        "status": "ROUTE_PROFILE_PROPOSAL_ONLY",
        "active_route_policy": False,
        "owner_decision_required": True,
        "provenance": proposal["provenance"],
        "what_this_is_not": list(proposal["what_this_is_not"]),
        "canonical_route_policy": proposal["canonical_route_policy"],
        "usages": usages,
        "registered_in_canon": sorted(registered),
        "to_make_this_canonical": list(proposal["to_make_this_canonical"]),
    }
