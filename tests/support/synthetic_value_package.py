"""合成の Provider 具体値 Package 一式を作る（公開側の試験用）。

## なぜ要るか

`DCR-CHAT-PROVIDER-CONFIG`／`-VALUES`／`-VALUE-INPUT`、具体値の棚卸し、用途別の
提案、v1 の準備状況 Report は Owner の回答・監査記録であり、公開用の配布コピーに
収録しない。ところが Production の記録器・ローカル UI・準備状況の監査器は、
その形の入力を読む。入力が無いと拒否の規則を 1 つも試せない。

## 正規の生成器を通す

ここでは上流（方式決定・回答・棚卸し）だけを **明示的な合成値** で書き、
`VALUE-INPUT` Package は正規の生成器 `tools/build_chat_provider_value_input.py` に
組ませる。欄の形・検査規則は生成器が導き、値の検査は Production の記録器が行う。
試験が見るのは、保存 Package と同じ生成器・検査器を通った入力である。

## 何ではないか

Owner の回答ではない。全 File に `synthetic_test_input: true` を付け、一時
Directory の外へ出さない。Runtime Evidence でもない。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILDER = REPO_ROOT / "tools/build_chat_provider_value_input.py"
ROUTE_POLICY = "design-source/registries/route-policy.yaml"

MARK = "SYNTHETIC-TEST-INPUT"

#: 保存 Package と同じ相対 Path。合成の Root の下に置く。
METHOD_RELATIVE = "docs/decision/DCR-CHAT-PROVIDER-CONFIG.json"
ANSWERS_RELATIVE = "docs/decision/DCR-CHAT-PROVIDER-VALUES.json"
PACKAGE_RELATIVE = "docs/decision/DCR-CHAT-PROVIDER-VALUE-INPUT.json"
PACKAGE_MD_RELATIVE = "docs/decision/DCR-CHAT-PROVIDER-VALUE-INPUT.md"
PROPOSAL_RELATIVE = "docs/decision/OWNER-ROUTE-PROFILE-PROPOSAL.json"
INVENTORY_RELATIVE = "docs/audit/chat-provider-concrete-values.json"
READINESS_V1_RELATIVE = "docs/audit/chat-provider-readiness.json"

#: 生成器・記録器・UI が設問番号で読む項目。**設問番号と欄名の断片は公開 Code に既出。**
ENDPOINT_PARTS = (
    "Scheme",
    "Host",
    "Port",
    "Path Pattern",
    "Auth Host",
    "Proxy",
    "DNS",
    "TLS",
    "Redirect",
    "Certificate Validation",
    "Tenant Scope",
)
VALUE_ITEMS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("CVR-1", "PROVIDER", ("合成 Provider ID の一覧",)),
    ("CVR-2", "PROVIDER", ("合成 Endpoint の各項目",)),
    ("CVR-3", "PROVIDER", ("合成 Model ID の集合",)),
    ("CVR-5", "PROVIDER", ("合成 Failover の順位",)),
    ("CVR-7", "SECRET_REF", ("合成 Keyring account の形式",)),
    ("CVR-10", "SECRET_REF", ("合成 service 名の接頭辞", "合成 service 名の区切り文字")),
    ("CVR-13", "ERROR_STATE", ("合成 Retry 可能な障害 ID",)),
    # 生成器は CVR-16 の欄名で形を選ぶ。**この 3 語は生成器の表と一致させる。**
    ("CVR-16", "RETRY_TIMEOUT", ("Retry 回数", "Timeout の値", "単位")),
)
SETTLED_ITEMS: tuple[tuple[str, str], ...] = (
    ("CVR-4", "PROVIDER"),
    ("CVR-6", "PROVIDER"),
    ("CVR-8", "SECRET_REF"),
    ("CVR-9", "SECRET_REF"),
    ("CVR-11", "SECRET_REF"),
    ("CVR-12", "ERROR_STATE"),
    ("CVR-14", "ERROR_STATE"),
    ("CVR-15", "RETRY_TIMEOUT"),
    ("CVR-17", "RETRY_TIMEOUT"),
    ("CVR-18", "RETRY_TIMEOUT"),
    ("CVR-19", "IDEMPOTENCY"),
    ("CVR-20", "IDEMPOTENCY"),
    ("CVR-21", "IDEMPOTENCY"),
    ("CVR-22", "IDEMPOTENCY"),
    ("CVR-23", "MVP0C_SCOPE"),
    ("CVR-24", "MVP0C_SCOPE"),
)
#: 条件漏れ。CVR-16 の 2 語は生成器の `ALIASES` が扱う（使われない別名は生成器が拒む）。
#: CVR-1 は供給欄が 1 件なので、生成器が機械的に同義語として解く。
CONDITION_GAPS: tuple[tuple[str, str], ...] = (
    ("CVR-1", "識別子"),
    ("CVR-16", "個別値"),
    ("CVR-16", "上限"),
)
#: 用途別の提案。**正本に無い Provider 名だけ**を置く（提案は正本ではない）。
PROPOSAL_HINTS = (
    ("SYNTHETIC-USAGE-CODE", "synthetic-unregistered-alpha"),
    ("SYNTHETIC-USAGE-REVIEW", "synthetic-unregistered-bravo"),
)


@dataclass(frozen=True)
class SyntheticValueChain:
    root: Path
    method: Path
    answers: Path
    package: Path
    proposal: Path
    inventory: Path
    readiness_v1: Path

    def package_document(self) -> dict[str, Any]:
        document: dict[str, Any] = json.loads(self.package.read_text(encoding="utf-8"))
        return document


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _question_ids() -> list[str]:
    return sorted(
        [q for q, _, _ in VALUE_ITEMS] + [q for q, _ in SETTLED_ITEMS],
        key=lambda qid: int(qid.split("-")[1]),
    )


def _answers() -> dict[str, Any]:
    answers = {
        qid: {"choice_id": f"{qid}-A", "label": MARK, "detail": f"{MARK} の選択肢。"}
        for qid in _question_ids()
    }
    # 生成器は CVR-2 の説明文の先頭文を「・」で区切って Endpoint 項目にする。
    answers["CVR-2"]["detail"] = "・".join(ENDPOINT_PARTS) + f"。{MARK} の Endpoint 項目。"
    return {
        "document_version": "1.0",
        "package_id": "DCR-CHAT-PROVIDER-VALUES",
        "synthetic_test_input": True,
        "status": "ANSWERED",
        "questions": [{"id": qid, "title": f"{MARK} {qid}"} for qid in _question_ids()],
        "answers": answers,
        "unanswered": [],
        "pending_owner_values": {qid: list(names) for qid, _, names in VALUE_ITEMS},
    }


def _inventory(answers_path: Path) -> dict[str, Any]:
    by_category: dict[str, list[str]] = {}
    for qid, category, _ in VALUE_ITEMS:
        by_category.setdefault(category, []).append(qid)
    return {
        "audit_id": MARK,
        "synthetic_test_input": True,
        "answer_package": {"path": ANSWERS_RELATIVE, "sha256": _sha(answers_path)},
        "items": [
            {
                "question": qid,
                "category": category,
                "title": f"{MARK} {qid}",
                "answer": f"{qid}-A",
                "recorded_supplies": list(names),
            }
            for qid, category, names in VALUE_ITEMS
        ],
        "items_by_category": dict(sorted(by_category.items())),
        "settled_without_values": [
            {
                "question": qid,
                "category": category,
                "title": f"{MARK} {qid}",
                "answer": f"{qid}-A",
                "reason": f"{MARK}: 回答で規則が決まり、値が残らない",
            }
            for qid, category in SETTLED_ITEMS
        ],
        "condition_gaps": [
            {"question": qid, "subject": subject, "detail": f"{MARK}: 説明文が求める語"}
            for qid, subject in CONDITION_GAPS
        ],
        "unclassified_items": [],
        "measured_counts": {"value_required_questions": len(VALUE_ITEMS)},
    }


def _proposal() -> dict[str, Any]:
    return {
        "document_version": "1.0",
        "synthetic_test_input": True,
        "provenance": f"{MARK}: 合成の用途別提案。Owner の希望ではない",
        "active_route_policy": False,
        "owner_decision_required": True,
        "what_this_is_not": [f"{MARK}: 実行設定ではない", f"{MARK}: 正本ではない"],
        "canonical_route_policy": ROUTE_POLICY,
        "usages": [
            {"usage": usage, "preferred_provider_hint": hint, "registered_in_canon": False}
            for usage, hint in PROPOSAL_HINTS
        ],
        "to_make_this_canonical": [f"{MARK}: 正本へ入れるには別の Owner Decision が要る"],
    }


def load_builder(chain: SyntheticValueChain, name: str = "synthetic_value_builder") -> ModuleType:
    """正規の生成器を読み込み、入出力を合成の Root へ向ける。**保存 Package を読まない。**"""
    spec = importlib.util.spec_from_file_location(name, BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = chain.root
    module.OUT = chain.package
    module.OUT_MD = chain.root / PACKAGE_MD_RELATIVE
    module.INVENTORY = chain.inventory
    module.ANSWERS_PKG = chain.answers
    module.METHOD = chain.method
    module.ROUTE_POLICY = chain.root / ROUTE_POLICY
    return module


def _git(root: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    subprocess.run(  # noqa: S603 - 固定 argv、shell 不使用
        ["git", "-C", str(root), *args],  # noqa: S607
        check=True,
        capture_output=True,
        env=env,
        timeout=30,
    )


def build_value_chain(root: Path) -> SyntheticValueChain:
    """合成の上流を書き、正規の生成器で `VALUE-INPUT` Package を組む。"""
    root.mkdir(parents=True, exist_ok=False)
    (root / ROUTE_POLICY).parent.mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / ROUTE_POLICY, root / ROUTE_POLICY)
    shutil.copyfile(REPO_ROOT / "registry-snapshot.json", root / "registry-snapshot.json")
    chain = SyntheticValueChain(
        root=root,
        method=root / METHOD_RELATIVE,
        answers=root / ANSWERS_RELATIVE,
        package=root / PACKAGE_RELATIVE,
        proposal=root / PROPOSAL_RELATIVE,
        inventory=root / INVENTORY_RELATIVE,
        readiness_v1=root / READINESS_V1_RELATIVE,
    )
    _write_json(
        chain.method,
        {"package_id": "DCR-CHAT-PROVIDER-CONFIG", "synthetic_test_input": True, "status": MARK},
    )
    _write_json(chain.answers, _answers())
    _write_json(chain.inventory, _inventory(chain.answers))
    _write_json(chain.proposal, _proposal())
    _write_json(chain.readiness_v1, {"synthetic_test_input": True, "report_version": 1})
    builder = load_builder(chain)
    builder.main(["--out", str(chain.package)])
    # 準備状況の監査器は作業 Commit を記録する。合成の Root を Git にしておく。
    _git(root, "init", "--quiet", "--initial-branch=synthetic")
    _git(root, "config", "user.name", "Synthetic value fixture")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "add", "--all")
    _git(root, "commit", "--quiet", "-m", "test: synthetic value-input chain")
    return chain
