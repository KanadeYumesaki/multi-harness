#!/usr/bin/env python3
"""`DCR-CHAT-PROVIDER-VALUE-INPUT` を組む。方式は 24/24 決まり、値だけが残っている。

## 一覧を手入力しない

入力欄は `docs/audit/chat-provider-concrete-values.json` から機械的に作る。
その監査自身が `DCR-CHAT-PROVIDER-VALUES` の `owner_must_supply` と条件式から
取り出したものであり、**この Script は一覧を持たない**。

## 値を作らない

Provider ID、Endpoint、Model、Retry 回数、Timeout、接頭辞、区切り文字を
**1 つも書かない**。書くのは型・形式・単位・重複禁止・範囲の検査規則だけである。
出力の `value` は全欄 `null` である。組む前と組んだ後に自分で確かめる。

## 同義語を黙って捨てない

監査は「選択肢の説明文が求める語が `owner_must_supply` に無い」件を条件漏れとして
出す。そのうち **供給欄が 1 件しかない設問** は、指す先が一意なので同義語である
（機械的に解ける）。残りは `ALIASES` へ理由つきで書くか、新しい入力欄にする。

**黙って落とさない。** 解けなかった語は必ず入力欄になる。

## 冪等

既に回答済みなら止める。未回答なら組み直すが、**入力済みの値を消さない**。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs/decision/DCR-CHAT-PROVIDER-VALUE-INPUT.json"
OUT_MD = ROOT / "docs/decision/DCR-CHAT-PROVIDER-VALUE-INPUT.md"
INVENTORY = ROOT / "docs/audit/chat-provider-concrete-values.json"
ANSWERS_PKG = ROOT / "docs/decision/DCR-CHAT-PROVIDER-VALUES.json"
METHOD = ROOT / "docs/decision/DCR-CHAT-PROVIDER-CONFIG.json"
ROUTE_POLICY = ROOT / "design-source/registries/route-policy.yaml"

#: 監査が条件漏れとして出した語のうち、**供給欄が複数あって機械では解けない**もの。
#: `(設問, 語, 対応する供給欄 or None, 理由)`。対応先が `None` なら新しい入力欄になる。
#: 供給欄が 1 件の設問は機械的に解けるので、ここへ書かない。
ALIASES: tuple[tuple[str, str, str | None, str], ...] = (
    (
        "CVR-16",
        "個別値",
        "Retry 回数",
        "説明文の「個別値」は Provider ごとの Retry 回数と Timeout の総称である。"
        "どちらも供給欄にあるので新しい値ではない",
    ),
    (
        "CVR-16",
        "上限",
        None,
        "説明文は上限を共通 Key で縛ると述べるが、供給欄に対応する項目が無い。"
        "**設問側の書き落としである。** 入力欄を足す",
    ),
)

#: 入力欄の型。値ではなく検査規則である。
_TEXT = {"type": "string", "min_length": 1}
_INT_NONNEG = {"type": "integer", "minimum": 0}
_INT_POSITIVE = {"type": "integer", "minimum": 1}


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _slug(text: str) -> str:
    """入力欄 ID。日本語の見出しから安定した ID を作る。"""
    return "F-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _endpoint_parts(detail: str) -> list[str]:
    """CVR-2 の説明文が列挙する Endpoint 項目。**この Script が決めない。**"""
    head = detail.split("。", 1)[0]
    return [part.strip() for part in head.split("・") if part.strip()]


def _failure_ids() -> list[str]:
    route = yaml.safe_load(ROUTE_POLICY.read_text(encoding="utf-8"))
    failures = route["fallback_eligible_failures"] + route["fallback_forbidden_failures"]
    return [entry["id"] for entry in failures]


def _resolve_aliases(
    inventory: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """条件漏れを、同義語と本当の欠落へ分ける。"""
    supplies = {item["question"]: item["recorded_supplies"] for item in inventory["items"]}
    declared = {(qid, subject): (target, why) for qid, subject, target, why in ALIASES}
    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for gap in inventory["condition_gaps"]:
        qid, subject = gap["question"], gap["subject"]
        entries = supplies[qid]
        if len(entries) == 1:
            resolved.append(
                {
                    "question": qid,
                    "subject": subject,
                    "resolves_to": entries[0],
                    "reason": "供給欄が 1 件しかないので、指す先が一意である",
                }
            )
            continue
        if (qid, subject) not in declared:
            raise SystemExit(
                f"UNCLASSIFIED_CONDITION_GAP: {qid} の「{subject}」を同義語とも欠落とも"
                "決めていない。ALIASES へ理由つきで書くこと"
            )
        target, why = declared[(qid, subject)]
        if target is None:
            unresolved.append({"question": qid, "subject": subject, "reason": why})
        else:
            if target not in entries:
                raise SystemExit(
                    f"BAD_ALIAS: {qid} の「{subject}」の対応先 {target} が供給欄に無い"
                )
            resolved.append(
                {"question": qid, "subject": subject, "resolves_to": target, "reason": why}
            )
    unused = sorted(
        f"{qid}/{subject}"
        for qid, subject, _, _ in ALIASES
        if not any(
            g["question"] == qid and g["subject"] == subject for g in inventory["condition_gaps"]
        )
    )
    if unused:
        raise SystemExit(f"STALE_ALIAS: 監査に無い語を ALIASES が持っている: {unused}")
    return resolved, unresolved


def _fields(inventory: dict[str, Any], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """入力欄を組む。**値を書かない。** 検査規則だけを書く。"""
    answers = json.loads(ANSWERS_PKG.read_text(encoding="utf-8"))["answers"]
    by_question = {item["question"]: item for item in inventory["items"]}
    fields: list[dict[str, Any]] = []

    def add(
        question: str,
        name: str,
        shape: str,
        constraints: dict[str, Any],
        note: str,
    ) -> None:
        fields.append(
            {
                "field_id": _slug(f"{question}/{name}"),
                "question": question,
                # 分類は監査から取る。**この Script が名付けない。**
                "category": by_question[question]["category"],
                "answer": by_question[question]["answer"],
                "name": name,
                "shape": shape,
                "constraints": constraints,
                "note": note,
                "value": None,
            }
        )

    # CVR-1 Provider ID
    add(
        "CVR-1",
        by_question["CVR-1"]["recorded_supplies"][0],
        "LIST",
        {"item": _TEXT, "min_items": 1, "unique": True},
        "以降の Provider 別入力は、ここへ書いた ID をそのまま Key に使う。",
    )
    # CVR-2 Endpoint
    parts = _endpoint_parts(answers["CVR-2"]["detail"])
    add(
        "CVR-2",
        by_question["CVR-2"]["recorded_supplies"][0],
        "PER_PROVIDER_OBJECT",
        {
            "keys_must_equal": "CVR-1 の Provider ID",
            "object_fields": {part: _TEXT for part in parts},
            "required_fields": parts,
        },
        f"{answers['CVR-2']['choice_id']} が挙げる {len(parts)} 項目をすべて埋める。",
    )
    # CVR-3 Model ID
    add(
        "CVR-3",
        by_question["CVR-3"]["recorded_supplies"][0],
        "PER_PROVIDER_LIST",
        {"keys_must_equal": "CVR-1 の Provider ID", "item": _TEXT, "min_items": 1, "unique": True},
        "集合外の Model 要求は拒否される。",
    )
    # CVR-5 Failover 順位
    add(
        "CVR-5",
        by_question["CVR-5"]["recorded_supplies"][0],
        "PER_PROVIDER_VALUE",
        {
            "keys_must_equal": "CVR-1 の Provider ID",
            "item": _INT_POSITIVE,
            "unique": True,
            "gaps_allowed": True,
            "order": "小さいほど先に試す",
        },
        f"{answers['CVR-5']['choice_id']}。同点を許さない。欠番は許す。",
    )
    # CVR-7 account の形式
    add(
        "CVR-7",
        by_question["CVR-7"]["recorded_supplies"][0],
        "FORMAT_RULE",
        dict(_TEXT),
        "Keyring の account 側に何を入れるかの**規則**である。実際の account 値ではない。",
    )
    # CVR-10 接頭辞・区切り文字
    for name in by_question["CVR-10"]["recorded_supplies"]:
        add(
            "CVR-10",
            name,
            "TEXT",
            dict(_TEXT),
            f"{answers['CVR-10']['choice_id']} の導出式に使う。Keyring service 名を"
            "この 2 つと Provider の識別子から組む。",
        )
    # CVR-13 Retry 可能な障害 ID
    add(
        "CVR-13",
        by_question["CVR-13"]["recorded_supplies"][0],
        "LIST",
        {
            "item": _TEXT,
            "unique": True,
            "allowed_values_from": "design-source/registries/route-policy.yaml の障害 ID",
            "allowed_values": _failure_ids(),
        },
        "Fallback の区分とは独立に列挙する。正本に無い ID は書けない。",
    )
    # CVR-16 Retry 回数・Timeout・単位（＋条件漏れで足りた上限）
    shapes = {
        "Retry 回数": (
            "PER_PROVIDER_VALUE",
            {"keys_must_equal": "CVR-1 の Provider ID", "item": _INT_NONNEG},
        ),
        "Timeout の値": (
            "PER_PROVIDER_VALUE",
            {"keys_must_equal": "CVR-1 の Provider ID", "item": _INT_POSITIVE},
        ),
        "単位": ("TEXT", dict(_TEXT)),
    }
    for name in by_question["CVR-16"]["recorded_supplies"]:
        shape, constraints = shapes[name]
        add("CVR-16", name, shape, constraints, f"{answers['CVR-16']['choice_id']}。")
    for gap in extra:
        add(
            gap["question"],
            gap["subject"],
            "SHARED_CAP",
            {"item": _INT_POSITIVE, "applies_to": "Retry 回数 と Timeout の値"},
            f"**設問側の書き落とし。** {gap['reason']}",
        )
    return fields


def build() -> dict[str, Any]:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    answers_pkg = json.loads(ANSWERS_PKG.read_text(encoding="utf-8"))
    snap = json.loads((ROOT / "registry-snapshot.json").read_text(encoding="utf-8"))

    if answers_pkg["status"] != "ANSWERED" or answers_pkg["unanswered"]:
        raise SystemExit("METHOD_ANSWERS_NOT_FINAL: 24 問が回答済みでない")
    if inventory["answer_package"]["sha256"] != _sha(ANSWERS_PKG):
        raise SystemExit("INVENTORY_STALE: 監査が指す回答 Package と実測が違う")

    resolved, unresolved = _resolve_aliases(inventory)
    fields = _fields(inventory, unresolved)

    ids = [field["field_id"] for field in fields]
    if len(ids) != len(set(ids)):
        raise SystemExit(f"DUPLICATE_FIELD_ID: {sorted({i for i in ids if ids.count(i) > 1})}")
    names = [f"{field['question']}/{field['name']}" for field in fields]
    if len(names) != len(set(names)):
        raise SystemExit(
            f"DUPLICATE_FIELD_NAME: {sorted({n for n in names if names.count(n) > 1})}"
        )
    filled = [field["name"] for field in fields if field["value"] is not None]
    if filled:
        raise SystemExit(f"VALUE_PREFILLED: 値を書いた入力欄がある: {filled}")

    covered = {field["question"] for field in fields}
    expected = {item["question"] for item in inventory["items"]}
    if covered != expected:
        raise SystemExit(f"ITEM_NOT_COVERED: {sorted(covered ^ expected)}")

    return {
        "document_version": "1.0",
        "package_id": "DCR-CHAT-PROVIDER-VALUE-INPUT",
        "task_id": "TASK-LLM-CHAT-PROVIDER-CONTRACT-CONCRETE-VALUE-DCR-002",
        "status": "DECISION_REQUIRED",
        "design_version": snap["design_version"],
        "design_sha256": snap["design_sha256"],
        "registry_snapshot_hash": snap["registry_snapshot_hash"],
        "schema_catalog_hash": snap["schema_catalog_hash"],
        "raised_because": (
            f"方式は {len(answers_pkg['answers'])}/{len(answers_pkg['questions'])} 決まったが、"
            f"{len(inventory['items'])} 設問に具体値が残っている。"
            "選択肢では埋まらない値であり、Owner が直接入力する必要がある。"
            "**推測で埋めれば正本が推測になる。**"
        ),
        "method_decision": {
            "path": str(METHOD.relative_to(ROOT)),
            "sha256": _sha(METHOD),
        },
        "answer_package": {
            "path": str(ANSWERS_PKG.relative_to(ROOT)),
            "sha256": _sha(ANSWERS_PKG),
            "answers": {qid: a["choice_id"] for qid, a in sorted(answers_pkg["answers"].items())},
        },
        "inventory": {
            "path": str(INVENTORY.relative_to(ROOT)),
            "sha256": _sha(INVENTORY),
            "value_required_questions": len(inventory["items"]),
            "categories": inventory["items_by_category"],
            "unclassified_items": inventory["unclassified_items"],
        },
        # 値が残っていない設問。**一覧に無いことを書き忘れと読ませない。**
        "settled_without_values": inventory["settled_without_values"],
        "condition_gaps": {
            "measured": inventory["condition_gaps"],
            "resolved_as_synonym": resolved,
            "added_as_field": unresolved,
        },
        "fields": fields,
        "answers": {},
        "unanswered": [field["field_id"] for field in fields],
        "blocked_until_answered": [
            "route-policy.yaml への Provider 設定値の追加",
            "Secret 参照 Core Schema の発行",
            "Error Code の採番",
            "Provider Router と Failover の実装",
        ],
        "not_done": [
            "Owner 回答を記録していない",
            "設計書・Registry・Schema・Production・Adapter・Evidence を変更していない",
            "Provider ID・Endpoint・Model・Retry 回数・Timeout・接頭辞・区切り文字を"
            "1 つも書いていない",
            "Network 通信・Keyring アクセス・Provider 接続をしていない",
            "API Key と Secret 値を作っていない",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DCR-CHAT-PROVIDER-VALUE-INPUT を組む")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)

    if OUT.is_file():
        prev = json.loads(OUT.read_text(encoding="utf-8"))
        if prev.get("answers") or prev.get("status") != "DECISION_REQUIRED":
            raise SystemExit(f"ALREADY_ANSWERED: {OUT.relative_to(ROOT)} は回答済み")
        entered = [f["name"] for f in prev.get("fields", []) if f.get("value") is not None]
        if entered:
            raise SystemExit(f"VALUES_ALREADY_ENTERED: 入力済みの値を消さない: {entered}")

    pkg = build()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(pkg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    L: list[str] = []
    w = L.append
    w("# DCR: Chat の Provider 契約の具体値（入力欄）")
    w("")
    w("**未入力である。** 値を推測で埋めずに止めている。")
    w("")
    w("| 項目 | 値 |")
    w("|---|---|")
    w(f"| 設計書 | v{pkg['design_version']} `{pkg['design_sha256'][:23]}…` |")
    w(f"| 値が要る設問 | {pkg['inventory']['value_required_questions']} 件 |")
    w(f"| 入力欄 | {len(pkg['fields'])} 件 |")
    w(f"| 未入力 | {len(pkg['unanswered'])} 件 |")
    w(f"| 方式決定 | `{pkg['method_decision']['sha256'][:23]}…` |")
    w(f"| 回答 Package | `{pkg['answer_package']['sha256'][:23]}…` |")
    w("")
    w(pkg["raised_because"])
    w("")
    w("## 入力欄")
    w("")
    w("**値は空欄である。既定値を置いていない。**")
    w("")
    w("| 欄 | 分類 | 設問 | 回答 | 形 | 検査 |")
    w("|---|---|---|---|---|---|")
    for field in pkg["fields"]:
        checks = json.dumps(field["constraints"], ensure_ascii=False)
        w(
            f"| {field['name']} | `{field['category']}` | `{field['question']}` "
            f"| `{field['answer']}` | `{field['shape']}` | `{checks}` |"
        )
    w("")
    w("## 条件漏れの扱い")
    w("")
    w(f"監査は条件漏れを **{len(pkg['condition_gaps']['measured'])} 件** 出した。")
    w("同義語と本当の書き落としを分けている。**黙って落としていない。**")
    w("")
    w("| 設問 | 語 | 扱い | 理由 |")
    w("|---|---|---|---|")
    for entry in pkg["condition_gaps"]["resolved_as_synonym"]:
        w(
            f"| `{entry['question']}` | {entry['subject']} | 同義語（{entry['resolves_to']}） | "
            f"{entry['reason']} |"
        )
    for entry in pkg["condition_gaps"]["added_as_field"]:
        w(f"|  | {entry['subject']} | **入力欄を足した** | {entry['reason']} |")
    w("")
    w("## 値が残っていない設問")
    w("")
    w("回答で規則が決まり、Owner が与える値が無い。**一覧に無いのは書き落としではない。**")
    w("")
    w("| 設問 | 分類 | 回答 | 理由 |")
    w("|---|---|---|---|")
    for entry in pkg["settled_without_values"]:
        w(
            f"| `{entry['question']}` | `{entry['category']}` "
            f"| `{entry['answer']}` | {entry['reason']} |"
        )
    w("")
    w("## この Task で実施していないこと")
    w("")
    for entry in pkg["not_done"]:
        w(f"* {entry}")
    w("")
    OUT_MD.write_text("\n".join(L) + "\n", encoding="utf-8")

    digest = hashlib.sha256(args.out.read_bytes()).hexdigest()
    try:
        shown = args.out.relative_to(ROOT)
    except ValueError:
        shown = args.out
    print(f"{shown}  sha256:{digest}")
    print(f"入力欄 {len(pkg['fields'])} 件 / 未入力 {len(pkg['unanswered'])} 件")
    print(
        f"条件漏れ {len(pkg['condition_gaps']['measured'])} 件 → "
        f"同義語 {len(pkg['condition_gaps']['resolved_as_synonym'])} / "
        f"入力欄を足した {len(pkg['condition_gaps']['added_as_field'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
