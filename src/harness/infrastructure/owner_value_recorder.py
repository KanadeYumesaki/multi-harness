"""Owner の具体値を検査して Package へ記録する中核。**保存経路はここ 1 つ。**

`tools/record_chat_provider_values.py`（CLI）と、ローカル UI の入力支援画面が
どちらもここを呼ぶ。実装を 2 つ持つと、片方だけ検査が緩んでも気付けない。

## 値を持たない

Provider ID も Endpoint も Model も Retry 回数も、この Module は 1 つも持たない。
値は呼出側が渡す File の中にしか無い。渡されなければ何も書かない。

## Secret 本体を受け取らない

API Key・Token・Secret 値は入力欄ではない。それらしい Key 名や値が来たら
`SECRET_VALUE_REJECTED` で止める。資格情報 Store へ接続せず、Network も使わない。
Endpoint は **形だけ**を見る。名前解決も接続もしない。

## 全部通ってから書く

検査は全欄と交差条件を先に済ませる。1 つでも落ちたら **何も書かない**。
Package は Byte 単位で不変のままになる。書く直前に「値以外が動いていない」
ことを自分で確かめ、動いていたら `PACKAGE_FIELD_CHANGED` で止める。

## CVR-10 は資格情報 Store の service 名の導出式である

Idempotency Key ではない。入力 File が欄の設問・分類・形を書き換えていたら
`FIELD_METADATA_CHANGED` で止める。`CVR-19`／`CVR-20`／`CVR-21`／`CVR-22` は
既決で入力欄を持たず、値を寄越されたら `SETTLED_QUESTION_NOT_OPEN` で止める。
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "ENTRY_KEYS",
    "MUTABLE_TOP_LEVEL",
    "PACKAGE_RELATIVE",
    "ROUTE_POLICY_RELATIVE",
    "SETTLED_QUESTIONS",
    "STATUS_MD_RELATIVE",
    "STATUS_RELATIVE",
    "TEMPLATE_RELATIVE",
    "TOP_LEVEL_KEYS",
    "ValueRejected",
    "failure_ids",
    "load_json",
    "record",
    "save_values",
    "status_markdown",
    "status_report",
    "template_from",
    "validate",
]

#: Repository Root からの相対 Path。**Root は呼出側が持つ。**
PACKAGE_RELATIVE = "docs/decision/DCR-CHAT-PROVIDER-VALUE-INPUT.json"
ROUTE_POLICY_RELATIVE = "design-source/registries/route-policy.yaml"
TEMPLATE_RELATIVE = "docs/decision/OWNER-VALUE-TEMPLATE.chat-provider.json"
STATUS_RELATIVE = "docs/decision/OWNER-VALUE-STATUS.chat-provider.json"
STATUS_MD_RELATIVE = "docs/decision/OWNER-VALUE-STATUS.chat-provider.md"


SETTLED_QUESTIONS = ("CVR-19", "CVR-20", "CVR-21", "CVR-22")

#: 入力 File の最上位に置いてよい Key。これ以外は受け取らない。
TOP_LEVEL_KEYS = frozenset({"document_version", "package_id", "instructions", "values"})

#: 1 欄の記述に置いてよい Key。`value` 以外は Package の写しで、書き換えを許さない。
ENTRY_KEYS = frozenset({"question", "name", "category", "shape", "value"})

#: Package のうち、記録で動いてよい Key。それ以外が動いたら止める。
MUTABLE_TOP_LEVEL = frozenset({"fields", "unanswered", "status"})

#: Secret 本体の受け取りを拒む。Key 名の側。
_SECRET_KEY = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|passphrase|credential|bearer|private[_-]?key)"
)

#: Secret 本体の受け取りを拒む。値の側。既知の接頭辞と、区切りの無い長い不透明列。
_SECRET_VALUE = re.compile(
    r"(?i)^(sk-|xox[baprs]-|ghp_|github_pat_|bearer\s|-----begin)"
    r"|^[A-Za-z0-9+/=_-]{40,}$"
)

#: Host 名の形。**接続しない。** Label は英数と `-`、先頭末尾は英数。
_HOST = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*$"
)

#: 単位に数量を書かせない。`30 秒` は単位ではなく値である。
_HAS_DIGIT = re.compile(r"\d")


class ValueRejected(Exception):
    """入力値を受け取らずに止める。**Package を書き換えない。**"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# 正本の読み込み
# ---------------------------------------------------------------------------


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def failure_ids(repo_root: Path) -> list[str]:
    """障害 ID の正本。`route-policy.yaml` から毎回読み直す。写しを信じない。"""
    route = yaml.safe_load((repo_root / ROUTE_POLICY_RELATIVE).read_text(encoding="utf-8"))
    failures = route["fallback_eligible_failures"] + route["fallback_forbidden_failures"]
    return [entry["id"] for entry in failures]


# ---------------------------------------------------------------------------
# 入力用紙
# ---------------------------------------------------------------------------


def template_from(pkg: dict[str, Any]) -> dict[str, Any]:
    """入力用紙を Package から作る。**値は Package の現状をそのまま写す。**

    既定値も例も置かない。未入力なら `null` のままである。既に入っている値は
    残す。用紙を作り直しても Owner の入力は消えない。
    """
    return {
        "document_version": 1,
        "package_id": pkg["package_id"],
        "instructions": [
            "`value` へ実値を書く。書けない欄は null のまま残す。",
            "API Key・Token・Secret 本体を書かない。参照だけを扱う。",
            "`question`／`name`／`category`／`shape` を書き換えない。",
            "Endpoint は形だけを検査する。接続確認はしない。",
        ],
        "values": {
            field["field_id"]: {
                "question": field["question"],
                "name": field["name"],
                "category": field["category"],
                "shape": field["shape"],
                "value": field["value"],
            }
            for field in pkg["fields"]
        },
    }


# ---------------------------------------------------------------------------
# 値の形
# ---------------------------------------------------------------------------


def _scan_secret(node: Any, where: str) -> None:
    """Secret 本体を受け取らない。Key 名と値の両方を見る。"""
    if isinstance(node, dict):
        for key, value in node.items():
            if _SECRET_KEY.search(str(key)):
                raise ValueRejected("SECRET_VALUE_REJECTED", f"{where}.{key} は Secret の名である")
            _scan_secret(value, f"{where}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _scan_secret(value, f"{where}[{index}]")
    elif isinstance(node, str) and _SECRET_VALUE.search(node):
        raise ValueRejected("SECRET_VALUE_REJECTED", f"{where} が Secret 本体の形をしている")


def _check_str(value: Any, rule: dict[str, Any], where: str) -> str:
    if not isinstance(value, str):
        raise ValueRejected("TYPE_MISMATCH", f"{where} は文字列ではない: {value!r}")
    if any(ch in value for ch in "\n\r\t"):
        raise ValueRejected("MALFORMED_TEXT", f"{where} に改行か Tab が入っている")
    if value != value.strip():
        raise ValueRejected("MALFORMED_TEXT", f"{where} の前後に空白がある: {value!r}")
    if len(value) < int(rule.get("min_length", 0)):
        raise ValueRejected("TOO_SHORT", f"{where} が短すぎる: {value!r}")
    return value


def _check_int(value: Any, rule: dict[str, Any], where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueRejected("TYPE_MISMATCH", f"{where} は整数ではない: {value!r}")
    minimum = rule.get("minimum")
    if minimum is not None and value < int(minimum):
        raise ValueRejected("OUT_OF_RANGE", f"{where} が {minimum} 未満である: {value}")
    # isinstance で int へ絞ってあるが、引数は Any なので明示して返す。
    checked: int = value
    return checked


def _check_item(value: Any, rule: dict[str, Any], where: str) -> Any:
    if rule.get("type") == "integer":
        return _check_int(value, rule, where)
    return _check_str(value, rule, where)


def _check_list(value: Any, constraints: dict[str, Any], where: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueRejected("TYPE_MISMATCH", f"{where} は配列ではない: {value!r}")
    minimum = constraints.get("min_items")
    if minimum is not None and len(value) < int(minimum):
        raise ValueRejected("TOO_FEW_ITEMS", f"{where} は {minimum} 件以上が要る: {len(value)} 件")
    items = [
        _check_item(item, constraints["item"], f"{where}[{index}]")
        for index, item in enumerate(value)
    ]
    if constraints.get("unique"):
        duplicated = sorted({item for item in items if items.count(item) > 1})
        if duplicated:
            raise ValueRejected("DUPLICATED_ITEM", f"{where} に重複がある: {duplicated}")
    return items


def _check_endpoint(fields: dict[str, Any], where: str) -> None:
    """Endpoint の **形だけ**を見る。名前解決も接続もしない。"""
    host = fields["Host"]
    if "://" in host or "/" in host:
        raise ValueRejected("ENDPOINT_MALFORMED", f"{where}.Host に Scheme か Path が混ざる")
    if not _HOST.match(host):
        raise ValueRejected("ENDPOINT_MALFORMED", f"{where}.Host が Host 名の形でない: {host}")
    port = fields["Port"]
    if not (port.isascii() and port.isdigit()) or not 1 <= int(port) <= 65535:
        raise ValueRejected("ENDPOINT_MALFORMED", f"{where}.Port が 1..65535 でない: {port}")
    if not fields["Path Pattern"].startswith("/"):
        raise ValueRejected("ENDPOINT_MALFORMED", f"{where}.Path Pattern が / で始まらない")


def _check_per_provider(value: Any, providers: list[str] | None, where: str) -> dict[str, Any]:
    if providers is None:
        raise ValueRejected("PROVIDER_LIST_REQUIRED", f"{where} より先に Provider ID が要る")
    if not isinstance(value, dict):
        raise ValueRejected("TYPE_MISMATCH", f"{where} は Provider ごとの Object ではない")
    missing = sorted(set(providers) - set(value))
    extra = sorted(set(value) - set(providers))
    if missing or extra:
        raise ValueRejected(
            "PROVIDER_KEY_MISMATCH",
            f"{where} の Key が Provider ID と一致しない（不足 {missing} ／ 余り {extra}）",
        )
    return value


def _check_field(field: dict[str, Any], value: Any, providers: list[str] | None) -> Any:
    """1 欄を形どおりに検査する。Provider 別の欄は Key の一致まで見る。"""
    shape = field["shape"]
    constraints = field["constraints"]
    where = f"{field['question']}／{field['name']}"

    if shape == "LIST":
        return _check_list(value, constraints, where)
    if shape in {"TEXT", "FORMAT_RULE"}:
        return _check_str(value, constraints, where)
    if shape == "SHARED_CAP":
        return _check_int(value, constraints["item"], where)
    if shape == "PER_PROVIDER_LIST":
        table = _check_per_provider(value, providers, where)
        return {key: _check_list(table[key], constraints, f"{where}[{key}]") for key in table}
    if shape == "PER_PROVIDER_VALUE":
        table = _check_per_provider(value, providers, where)
        checked = {
            key: _check_item(table[key], constraints["item"], f"{where}[{key}]") for key in table
        }
        if constraints.get("unique"):
            values = list(checked.values())
            duplicated = sorted({v for v in values if values.count(v) > 1})
            if duplicated:
                raise ValueRejected("DUPLICATED_RANK", f"{where} に同点がある: {duplicated}")
        return checked
    if shape == "PER_PROVIDER_OBJECT":
        table = _check_per_provider(value, providers, where)
        required = constraints["required_fields"]
        out: dict[str, Any] = {}
        for key in table:
            entry = table[key]
            if not isinstance(entry, dict):
                raise ValueRejected("TYPE_MISMATCH", f"{where}[{key}] が Object ではない")
            missing = [name for name in required if name not in entry]
            extra = sorted(set(entry) - set(constraints["object_fields"]))
            if missing:
                raise ValueRejected("ENDPOINT_FIELD_MISSING", f"{where}[{key}] に不足: {missing}")
            if extra:
                raise ValueRejected("ENDPOINT_FIELD_UNKNOWN", f"{where}[{key}] に余り: {extra}")
            for name in required:
                _check_str(
                    entry[name], constraints["object_fields"][name], f"{where}[{key}].{name}"
                )
            _check_endpoint(entry, f"{where}[{key}]")
            out[key] = entry
        return out
    raise ValueRejected("UNKNOWN_SHAPE", f"{where} の形を知らない: {shape}")


# ---------------------------------------------------------------------------
# 交差条件
# ---------------------------------------------------------------------------


def _cap_check(pkg: dict[str, Any], values: dict[str, Any]) -> None:
    """共通上限と単位を見る。**上限が適用される欄は設問から機械的に決める。**

    散文の `applies_to` を解釈しない。同じ設問の Provider 別の値がすべて対象で
    ある。適用先が 0 件なら上限が宙に浮いているので止める。
    """
    caps = [f for f in pkg["fields"] if f["shape"] == "SHARED_CAP"]
    for cap_field in caps:
        question = cap_field["question"]
        capped = [
            f
            for f in pkg["fields"]
            if f["question"] == question and f["shape"] == "PER_PROVIDER_VALUE"
        ]
        if not capped:
            raise ValueRejected("SHARED_CAP_APPLIES_TO_NOTHING", f"{question} の上限に適用先が無い")
        units = [f for f in pkg["fields"] if f["question"] == question and f["shape"] == "TEXT"]
        supplied = [f for f in capped if values.get(f["field_id"]) is not None]
        if not supplied:
            continue
        if not all(values.get(f["field_id"]) is not None for f in units):
            raise ValueRejected("UNIT_MISSING", f"{question} の値を入れるなら単位も要る")
        for unit_field in units:
            unit = values[unit_field["field_id"]]
            if _HAS_DIGIT.search(str(unit)):
                raise ValueRejected(
                    "UNIT_NOT_A_UNIT", f"{question}／{unit_field['name']} に数量が入る: {unit!r}"
                )
        cap = values.get(cap_field["field_id"])
        if cap is None:
            raise ValueRejected("SHARED_CAP_REQUIRED", f"{question} の値を入れるなら上限も要る")
        for field in supplied:
            for provider, value in values[field["field_id"]].items():
                if value > cap:
                    raise ValueRejected(
                        "EXCEEDS_SHARED_CAP",
                        f"{question}／{field['name']}[{provider}] が上限 {cap} を超える: {value}",
                    )


def _allowed_values_check(pkg: dict[str, Any], values: dict[str, Any], canon: list[str]) -> None:
    """列挙から選ぶ欄を見る。Package の写しが正本と食い違っていたら先に止める。"""
    for field in pkg["fields"]:
        allowed = field["constraints"].get("allowed_values")
        if allowed is None:
            continue
        if sorted(allowed) != sorted(canon):
            raise ValueRejected(
                "ALLOWED_VALUES_DRIFTED",
                f"{field['question']} の候補が route-policy.yaml と食い違う",
            )
        value = values.get(field["field_id"])
        if value is None:
            continue
        outside = sorted(set(value) - set(canon))
        if outside:
            raise ValueRejected(
                "FAILURE_ID_NOT_IN_CANON",
                f"{field['question']} に正本に無い障害 ID がある: {outside}",
            )


# ---------------------------------------------------------------------------
# 検査の入口
# ---------------------------------------------------------------------------


def validate(pkg: dict[str, Any], doc: Any, canon: list[str]) -> dict[str, Any]:
    """入力 File を全部検査し、記録すべき値を返す。**1 つでも落ちたら例外。**"""
    if not isinstance(doc, dict):
        raise ValueRejected("MALFORMED_INPUT", "入力 File が Object ではない")
    extra = sorted(set(doc) - TOP_LEVEL_KEYS)
    if extra:
        raise ValueRejected("NOT_A_VALUE_FIELD", f"最上位に余分な Key がある: {extra}")
    if doc.get("package_id") != pkg["package_id"]:
        raise ValueRejected("PACKAGE_MISMATCH", f"別の Package 宛である: {doc.get('package_id')!r}")
    entries = doc.get("values")
    if not isinstance(entries, dict):
        raise ValueRejected("MALFORMED_INPUT", "`values` が Object ではない")

    # `values` の外へ隠しても拾う。入力 File 全体を見る。
    _scan_secret(doc, "input")

    by_id = {field["field_id"]: field for field in pkg["fields"]}
    unknown = sorted(set(entries) - set(by_id))
    if unknown:
        raise ValueRejected("UNKNOWN_FIELD", f"Package に無い欄がある: {unknown}")
    missing = sorted(set(by_id) - set(entries))
    if missing:
        raise ValueRejected("FIELD_MISSING", f"入力 File に欠けている欄がある: {missing}")

    values: dict[str, Any] = {}
    for field_id, entry in entries.items():
        field = by_id[field_id]
        if not isinstance(entry, dict):
            raise ValueRejected("MALFORMED_INPUT", f"{field_id} の記述が Object ではない")
        surplus = sorted(set(entry) - ENTRY_KEYS)
        if surplus:
            raise ValueRejected("NOT_A_VALUE_FIELD", f"{field_id} に余分な Key: {surplus}")
        for key in ("question", "name", "category", "shape"):
            if entry.get(key) != field[key]:
                raise ValueRejected(
                    "FIELD_METADATA_CHANGED",
                    f"{field_id} の {key} が書き換わっている: {entry.get(key)!r} ≠ {field[key]!r}",
                )
        if field["question"] in SETTLED_QUESTIONS:
            raise ValueRejected(
                "SETTLED_QUESTION_NOT_OPEN", f"{field['question']} は既決で入力欄を持たない"
            )
        values[field_id] = entry["value"]

    provider_fields = [
        f for f in pkg["fields"] if f["shape"] == "LIST" and f["category"] == "PROVIDER"
    ]
    if len(provider_fields) != 1:
        raise ValueRejected(
            "PROVIDER_FIELD_AMBIGUOUS", f"Provider ID の欄が {len(provider_fields)} 件ある"
        )
    provider_field = provider_fields[0]
    providers: list[str] | None = None
    if values.get(provider_field["field_id"]) is not None:
        providers = _check_list(
            values[provider_field["field_id"]],
            provider_field["constraints"],
            f"{provider_field['question']}／{provider_field['name']}",
        )

    checked: dict[str, Any] = {}
    for field_id, value in values.items():
        if value is None:
            continue
        field = by_id[field_id]
        if field_id == provider_field["field_id"]:
            checked[field_id] = providers
            continue
        checked[field_id] = _check_field(field, value, providers)

    _allowed_values_check(pkg, checked, canon)
    _cap_check(pkg, checked)
    return checked


# ---------------------------------------------------------------------------
# 記録
# ---------------------------------------------------------------------------


def _assert_only_values_changed(before: dict[str, Any], after: dict[str, Any]) -> None:
    """値以外が動いていないことを書く直前に確かめる。"""
    frozen_before = {k: v for k, v in before.items() if k not in MUTABLE_TOP_LEVEL}
    frozen_after = {k: v for k, v in after.items() if k not in MUTABLE_TOP_LEVEL}
    if frozen_before != frozen_after:
        moved = sorted(k for k in frozen_before if frozen_before[k] != frozen_after.get(k))
        raise ValueRejected("PACKAGE_FIELD_CHANGED", f"値以外の Field が動いた: {moved}")
    if len(before["fields"]) != len(after["fields"]):
        raise ValueRejected("PACKAGE_FIELD_CHANGED", "入力欄の数が動いた")
    for old, new in zip(before["fields"], after["fields"], strict=True):
        if {k: v for k, v in old.items() if k != "value"} != {
            k: v for k, v in new.items() if k != "value"
        }:
            raise ValueRejected("PACKAGE_FIELD_CHANGED", f"{old['field_id']} の記述が動いた")


def save_values(
    package_path: Path,
    document: Any,
    canon: list[str],
    *,
    out: Path | None = None,
) -> dict[str, Any]:
    """検査して書く。**Package を書く箇所はここ 1 つである。**

    CLI もローカル UI もここを呼ぶ。書く場所が 2 つあると、片方だけ順序が変わって
    も気付けない。検査に落ちたら `ValueRejected` が飛び、**何も書かない**。
    """
    package = load_json(package_path)
    values = validate(package, document, canon)
    updated = record(package, values)
    (out or package_path).write_text(
        json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return updated


def record(pkg: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """検査済みの値を Package へ載せる。**未入力の欄は触らない。**"""
    out = copy.deepcopy(pkg)
    for field in out["fields"]:
        supplied = values.get(field["field_id"])
        if supplied is not None:
            field["value"] = supplied
    out["unanswered"] = [f["field_id"] for f in out["fields"] if f["value"] is None]
    out["status"] = "DECISION_REQUIRED" if out["unanswered"] else "ANSWERED"
    _assert_only_values_changed(pkg, out)
    return out


# ---------------------------------------------------------------------------
# 状態の報告
# ---------------------------------------------------------------------------


def status_report(pkg: dict[str, Any], canon: list[str]) -> dict[str, Any]:
    """設問単位と欄単位の両方で、入っている／入っていないを数える。"""
    by_question: dict[str, list[dict[str, Any]]] = {}
    for field in pkg["fields"]:
        by_question.setdefault(field["question"], []).append(field)

    questions = []
    for question in sorted(by_question, key=lambda q: int(q.split("-")[1])):
        fields = by_question[question]
        questions.append(
            {
                "question": question,
                "category": fields[0]["category"],
                "answer": fields[0]["answer"],
                "fields": [
                    {"name": f["name"], "shape": f["shape"], "answered": f["value"] is not None}
                    for f in fields
                ],
                "answered": all(f["value"] is not None for f in fields),
            }
        )

    answered_fields = [f for f in pkg["fields"] if f["value"] is not None]
    return {
        "document_version": 1,
        "task_id": "TASK-LLM-CHAT-PROVIDER-CONTRACT-CONCRETE-VALUE-OWNER-ANSWER-001",
        "package_id": pkg["package_id"],
        "package_status": pkg["status"],
        "design_version": pkg["design_version"],
        "design_sha256": pkg["design_sha256"],
        "registry_snapshot_hash": pkg["registry_snapshot_hash"],
        "failure_id_canon": canon,
        "measured": {
            "questions_total": len(questions),
            "questions_answered": sum(1 for q in questions if q["answered"]),
            "fields_total": len(pkg["fields"]),
            "fields_answered": len(answered_fields),
        },
        "questions": questions,
        "unanswered_questions": [q["question"] for q in questions if not q["answered"]],
        "unanswered_fields": [
            f"{f['question']}／{f['name']}" for f in pkg["fields"] if f["value"] is None
        ],
        "not_done": [
            "Network 通信・資格情報 Store へのアクセス・Provider 接続をしていない",
            "API Key・Token・Secret 本体を受け取っても保存してもいない",
            "Provider ID・Endpoint・Model の仮値を作っていない",
            "設計書・Registry・Schema・Production・Adapter・Evidence を変更していない",
        ],
    }


def status_markdown(report: dict[str, Any]) -> str:
    measured = report["measured"]
    L: list[str] = []
    w = L.append
    w("# Owner 具体値の入力状況（Chat の Provider 契約）")
    w("")
    if report["unanswered_questions"]:
        w("**未入力である。** 値を推測で埋めずに止めている。")
    else:
        w("**全欄が入力済みである。**")
    w("")
    w("| 項目 | 実測 |")
    w("|---|---|")
    w(f"| 設問 | {measured['questions_answered']} / {measured['questions_total']} 件 |")
    w(f"| 入力欄 | {measured['fields_answered']} / {measured['fields_total']} 件 |")
    w(f"| Package | `{report['package_status']}` |")
    w("")
    w("## 設問ごと")
    w("")
    w("| 設問 | 分類 | 回答 | 欄 | 状態 |")
    w("|---|---|---|---|---|")
    for question in report["questions"]:
        names = "／".join(f["name"] for f in question["fields"])
        state = "入力済み" if question["answered"] else "**未入力**"
        w(
            f"| `{question['question']}` | `{question['category']}` "
            f"| `{question['answer']}` | {names} | {state} |"
        )
    w("")
    w("## この Task で実施していないこと")
    w("")
    for entry in report["not_done"]:
        w(f"* {entry}")
    w("")
    return "\n".join(L) + "\n"
