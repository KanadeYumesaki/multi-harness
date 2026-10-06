#!/usr/bin/env python3
"""Owner の具体値を記録する CLI。

**検査と保存の実体は `harness.infrastructure.owner_value_recorder` にある。**
ここはその薄い入口で、Path の既定値と引数の解釈だけを持つ。ローカル UI の
入力支援画面も同じ中核を呼ぶ。実装を 2 つ持つと、片方だけ検査が緩んでも
気付けない。

Owner が値を渡さなければ、未回答のまま止める。値は 1 つも作らない。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from harness.infrastructure.owner_value_recorder import (  # noqa: E402
    ENTRY_KEYS,
    MUTABLE_TOP_LEVEL,
    PACKAGE_RELATIVE,
    ROUTE_POLICY_RELATIVE,
    SETTLED_QUESTIONS,
    STATUS_MD_RELATIVE,
    STATUS_RELATIVE,
    TEMPLATE_RELATIVE,
    TOP_LEVEL_KEYS,
    ValueRejected,
    load_json,
    record,
    save_values,
    status_markdown,
    status_report,
    template_from,
    validate,
)

__all__ = [
    "ENTRY_KEYS",
    "MUTABLE_TOP_LEVEL",
    "SETTLED_QUESTIONS",
    "TOP_LEVEL_KEYS",
    "ValueRejected",
    "failure_ids",
    "main",
    "record",
    "save_values",
    "status_report",
    "template_from",
    "validate",
]

PKG = ROOT / PACKAGE_RELATIVE
ROUTE_POLICY = ROOT / ROUTE_POLICY_RELATIVE
TEMPLATE = ROOT / TEMPLATE_RELATIVE
STATUS = ROOT / STATUS_RELATIVE
STATUS_MD = ROOT / STATUS_MD_RELATIVE


def failure_ids() -> list[str]:
    """障害 ID の正本。Repository Root を既定にして中核へ委ねる。"""
    from harness.infrastructure import owner_value_recorder

    return owner_value_recorder.failure_ids(ROOT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Owner の具体値を記録する")
    parser.add_argument("--pkg", type=Path, default=PKG)
    parser.add_argument("--out", type=Path, default=None, help="既定は --pkg と同じ")
    parser.add_argument("--values", type=Path, default=None, help="Owner が埋めた入力 File")
    parser.add_argument("--emit-template", type=Path, default=None, help="入力用紙を書き出す")
    parser.add_argument("--status-out", type=Path, default=STATUS)
    parser.add_argument("--status-md", type=Path, default=STATUS_MD)
    parser.add_argument("--check", action="store_true", help="記録済みの値を再検査する")
    args = parser.parse_args(argv)

    # 回答 Package が無いのは「Owner が値を記録していない」初期状態である。検査する
    # 記録値が無いことを明示して止める。**File の欠落だけ**を扱い、壊れた JSON や
    # 読めない File を未設定に見せない。記録・様式の出力は Package を要求する。
    if args.check and not args.pkg.exists() and not args.pkg.is_symlink():
        print("OWNER_VALUES_UNSET: 回答 Package が無い初期状態。検査する記録値は無い")
        return 0

    pkg = load_json(args.pkg)
    canon = failure_ids()

    if args.emit_template is not None:
        template = template_from(pkg)
        args.emit_template.parent.mkdir(parents=True, exist_ok=True)
        args.emit_template.write_text(
            json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"入力用紙: {args.emit_template} （{len(template['values'])} 欄、値は現状の写し）")
        return 0

    if args.check:
        validate(pkg, template_from(pkg), canon)
        print(f"CHECK_OK: 記録済みの値は検査を通る（未入力 {len(pkg['unanswered'])} 欄）")
        return 0

    if args.values is not None:
        # 書くのは記録器の `save_values` だけである。CLI は Path を渡すだけ。
        pkg = save_values(args.pkg, load_json(args.values), canon, out=args.out)
        entered = sum(1 for f in pkg["fields"] if f["value"] is not None)
        print(f"記録した欄: {entered} / {len(pkg['fields'])}")

    report = status_report(pkg, canon)
    args.status_out.parent.mkdir(parents=True, exist_ok=True)
    args.status_out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.status_md.write_text(status_markdown(report), encoding="utf-8")

    measured = report["measured"]
    print(
        f"設問 {measured['questions_answered']}/{measured['questions_total']}   "
        f"欄 {measured['fields_answered']}/{measured['fields_total']}"
    )
    if report["unanswered_questions"]:
        print("OWNER_VALUES_MISSING: 未入力の設問 " + "／".join(report["unanswered_questions"]))
        print("値を推測しない。Owner の入力を待って止める。")
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
