#!/usr/bin/env python3
"""Readiness Report を読んでいる箇所を数え、将来の判定経路がどれを見るか測る。

v1 は凍結してある。だが凍結しただけでは足りない。**v1 の中身を読んで判定して
いる Code が残っていれば、直したはずの測り方が別の入口から生き続ける。**

そこで参照箇所を 2 つに分ける。

| 区分 | 何をしているか |
|---|---|
| `PROVENANCE_ONLY` | Path と Hash だけを記録する。中身を読まない |
| `READS_CONTENT` | `json.loads` して項目や件数を取り出し、判定に使う |

`READS_CONTENT` がすべて悪いわけではない。**凍結済みの成果物を再現する Code は、
v1 を読み続けるのが正しい。** v3 で作り直すと、回答済み DCR や過去の step
summary の Bytes が動いてしまう。だから出力先も測る。

| 出力先 | 意味 |
|---|---|
| `REPRODUCES_FROZEN` | 回答済み DCR か過去の step summary を作る。v1 のままが正しい |
| `FORWARD_DECISION` | それ以外。将来の判定に効く。v3 を見るべき |

判定材料は AST の Node と Path 文字列だけである。docstring と Comment は見ない。
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
V1_PATH = "docs/audit/chat-provider-readiness.json"
V2_PATH = "docs/audit/provider-readiness-v2.json"
V3_PATH = "docs/audit/provider-readiness-v3.json"
REPORTS = {V1_PATH: "v1", V2_PATH: "v2", V3_PATH: "v3"}

#: 凍結済みの成果物。ここを作る Code は v1 を読み続けるのが正しい。
FROZEN_OUTPUTS = ("docs/decision/DCR-CHAT-PROVIDER-CONFIG.json",)
FROZEN_OUTPUT_PREFIX = "runtime-evidence/DECISION-APPLY/step"

#: 走査する場所。生成物と仮想環境は入れない。
SCAN_DIRS = ("tools", "tests", "src", "runtime-evidence/DECISION-APPLY")

DEFAULT_OUT = ROOT / "docs/audit/readiness-consumer-gap.json"
DEFAULT_MD = ROOT / "docs/audit/readiness-consumer-gap.md"


def _string_literals(tree: ast.AST) -> list[str]:
    """Code 中の文字列 Literal。docstring は除く。

    Path を突き合わせるためだけに使う。**文言の意味は読まない。**
    """
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    docstrings.add(id(value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


#: 中身を読む合図になる呼出。**解析だけを数える。**
#:
#: `read_text` と `read_bytes` は入れない。`hashlib.sha256(P.read_bytes())` は
#: Hash を取っているだけで、判定に中身を使っていない。素性の記録と、中身からの
#: 判定は別のことである。
LOADERS = frozenset({"loads", "load", "load_json", "safe_load"})


def _report_bindings(tree: ast.AST) -> dict[str, str]:
    """`NAME = ROOT / "docs/audit/....json"` を追う。名前 → Report ID。"""
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        value = node.value
        if value is None:
            continue
        found = [lit for lit in _string_literals(value) if lit in REPORTS]
        if not found:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                bindings[target.id] = REPORTS[found[0]]
    return bindings


def _names_in(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def _reports_read(tree: ast.AST, bindings: dict[str, str]) -> set[str]:
    """中身を読まれている Report。

    **File のどこかに `json.loads` があるか**ではなく、**その Report を指す名前が
    読込みの引数に出るか**で決める。前者だと、別の File を読むために `json.loads`
    を使っているだけの Code まで巻き込む。
    """
    read: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in LOADERS:
            continue
        # 呼出の中に現れる名前と Path Literal の両方を見る。
        subtree_names = _names_in(node)
        for bound, report in bindings.items():
            if bound in subtree_names:
                read.add(report)
        for lit in _string_literals(node):
            if lit in REPORTS:
                read.add(REPORTS[lit])
    return read


def _writes(literals: list[str]) -> list[str]:
    """出力先らしき Path。文字列 Literal の中から拾う。"""
    found = [lit for lit in literals if lit in FROZEN_OUTPUTS]
    found += [lit for lit in literals if lit.startswith(FROZEN_OUTPUT_PREFIX)]
    return sorted(set(found))


#: 監査器自身。検出用の定数を自分の出力先と読み違えるので走査から外す。
SELF = "tools/audit_readiness_consumers.py"


def _python_files() -> list[Path]:
    files: list[Path] = []
    for name in SCAN_DIRS:
        base = ROOT / name
        if base.is_dir():
            files.extend(sorted(base.rglob("*.py")))
    return [p for p in files if str(p.relative_to(ROOT)) != SELF]


def measure(sources: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    # 一度列挙した同じ入力から、解析結果と件数を作る。履歴も同じ解析器を使う。
    if sources is None:
        sources = [
            (str(p.relative_to(ROOT)), p.read_text(encoding="utf-8")) for p in _python_files()
        ]
    consumers: list[dict[str, Any]] = []
    for path, source in sources:
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as exc:
            raise ValueError("AUDIT_SOURCE_INVALID") from exc
        literals = _string_literals(tree)
        referenced = sorted({REPORTS[lit] for lit in literals if lit in REPORTS})
        if not referenced:
            continue
        read = _reports_read(tree, _report_bindings(tree))
        frozen_outputs = _writes(literals)
        consumers.append(
            {
                "path": path,
                "reports": referenced,
                "reports_read": sorted(read),
                "access": "READS_CONTENT" if read else "PROVENANCE_ONLY",
                "output": "REPRODUCES_FROZEN" if frozen_outputs else "FORWARD_DECISION",
                "frozen_outputs": frozen_outputs,
            }
        )

    def _provenance(report: str) -> list[str]:
        return [
            c["path"]
            for c in consumers
            if report in c["reports"] and report not in c["reports_read"]
        ]

    v1_content = [c for c in consumers if "v1" in c["reports_read"]]
    v3_content = [c for c in consumers if "v3" in c["reports_read"]]
    v3_forward = sorted(c["path"] for c in v3_content if c["output"] == "FORWARD_DECISION")
    forward_on_v1 = sorted(
        c["path"]
        for c in v1_content
        if c["output"] == "FORWARD_DECISION" and "v3" not in c["reports_read"]
    )

    gaps: list[dict[str, Any]] = []
    if forward_on_v1:
        gaps.append(
            {
                "gap_id": "READINESS-CONSUMER-V1-FORWARD",
                "severity": "OPEN",
                "paths": forward_on_v1,
                "why": (
                    "凍結済み成果物の再現ではないのに v1 の中身から判定している。"
                    "測り方を直した v3 ではなく、文字列一致で測った v1 が将来の判断へ流れる"
                ),
            }
        )
    if not v3_content:
        gaps.append(
            {
                "gap_id": "READINESS-CONSUMER-V3-UNUSED",
                "severity": "OPEN",
                "paths": [],
                "why": (
                    "v3 の判定を読む Consumer が 1 件も無い。Report は在るが、"
                    "判定経路がまだ v3 へ繋がっていない"
                ),
            }
        )
    if not v3_forward:
        gaps.append(
            {
                "gap_id": "READINESS-CONSUMER-V3-NO-FORWARD-PATH",
                "severity": "OPEN",
                "paths": [],
                "why": (
                    "v3 を読む Consumer はすべて検証用（凍結の確認と再現の確認）であり、"
                    "将来の判定を動かす経路が 1 件も v3 を見ていない。"
                    "Provider 実装の可否を判断する Code を書くときに、v1 ではなく v3 を"
                    "読ませる必要がある。**この Task では直さない。**"
                ),
            }
        )

    return {
        "audit_id": "READINESS-CONSUMER-GAP",
        "task_id": "TASK-GITHUB-ACTIONS-POST-RESET-VERIFICATION-001",
        "method": {
            "basis": "ast",
            "accepted": [
                "文字列 Literal と Report の Path の完全一致",
                "解析呼出（json.loads / json.load / load_json / safe_load）の引数に出ること",
            ],
            "rejected": [
                "docstring",
                "Comment",
                "変数名や関数名からの推測",
                "read_text / read_bytes だけの参照（Hash 取得は素性の記録である）",
                "File のどこかに解析呼出があること（Report ごとに見る）",
            ],
        },
        "scanned_dirs": list(SCAN_DIRS),
        "scanned_files": len(sources),
        "consumers": consumers,
        "counts": {
            "total": len(consumers),
            "v1_provenance_only": len(_provenance("v1")),
            "v1_reads_content": len(v1_content),
            "v1_reads_content_reproducing_frozen": len(
                [c for c in v1_content if c["output"] == "REPRODUCES_FROZEN"]
            ),
            "v1_reads_content_forward": len(forward_on_v1),
            "v3_provenance_only": len(_provenance("v3")),
            "v3_reads_content": len(v3_content),
            "v3_reads_content_forward": len(v3_forward),
        },
        "gaps": gaps,
        "not_changed": {
            "v1_report": "1 Byte も変更していない",
            "consumers": "1 件も修正していない。別 Task の Gap として記録するだけである",
            "v3_to_v1_backfill": "v3 の判定を v1 へ逆反映していない",
        },
    }


def _markdown(report: dict[str, Any]) -> str:
    L: list[str] = []
    w = L.append
    counts = report["counts"]

    w("# Readiness Report の Consumer 監査")
    w("")
    w("**v1 を凍結しただけでは足りない。** 中身を読んで判定している Code が残って")
    w("いれば、直したはずの測り方が別の入口から生き続ける。参照箇所を数えた。")
    w("")
    w("この監査は **1 件も修正しない**。見つけた不足は別 Task の Gap として残す。")
    w("")

    w("## 測り方")
    w("")
    method = report["method"]
    w(f"根拠は `{method['basis']}` だけである。")
    w("")
    w("| 根拠にする | 根拠にしない |")
    w("|---|---|")
    accepted, rejected = method["accepted"], method["rejected"]
    for row in range(max(len(accepted), len(rejected))):
        left = accepted[row] if row < len(accepted) else ""
        right = rejected[row] if row < len(rejected) else ""
        w(f"| {left} | {right} |")
    w("")
    dirs = "・".join(f"`{d}`" for d in report["scanned_dirs"])
    w(f"走査対象は {dirs} の Python File {report['scanned_files']} 件。")
    w("")

    w("## 数えた結果")
    w("")
    w("| 区分 | 件数 |")
    w("|---|---|")
    w(f"| Report を参照する File | {counts['total']} |")
    w(f"| v1 の Path と Hash だけ | {counts['v1_provenance_only']} |")
    w(f"| v1 の中身を読む | {counts['v1_reads_content']} |")
    w(f"| ├ うち凍結済み成果物の再現 | {counts['v1_reads_content_reproducing_frozen']} |")
    w(f"| └ **うち将来の判定に効く** | **{counts['v1_reads_content_forward']}** |")
    w(f"| v3 の Path と Hash だけ | {counts['v3_provenance_only']} |")
    w(f"| v3 の中身を読む | {counts['v3_reads_content']} |")
    w(f"| └ **うち将来の判定に効く** | **{counts['v3_reads_content_forward']}** |")
    w("")

    w("## 参照箇所")
    w("")
    w("| Path | 参照 | 中身を読む | 読み方 | 出力先 |")
    w("|---|---|---|---|---|")
    for entry in report["consumers"]:
        reports = "・".join(entry["reports"])
        read = "・".join(entry["reports_read"]) or "—"
        w(f"| `{entry['path']}` | {reports} | {read} | `{entry['access']}` | `{entry['output']}` |")
    w("")
    w("`REPRODUCES_FROZEN` は回答済み DCR か過去の step summary を作る Code である。")
    w("**これらは v1 を読み続けるのが正しい。** v3 で作り直すと、回答の束縛と過去の")
    w("記録の Bytes が動いてしまう。直さないことが正解である。")
    w("")

    w("## Gap")
    w("")
    if not report["gaps"]:
        w("無し。")
    else:
        w("| Gap ID | 状態 | 内容 |")
        w("|---|---|---|")
        for gap in report["gaps"]:
            w(f"| `{gap['gap_id']}` | `{gap['severity']}` | {gap['why']} |")
        w("")
        for gap in report["gaps"]:
            if gap["paths"]:
                w(f"`{gap['gap_id']}` の該当 Path:")
                w("")
                for path in gap["paths"]:
                    w(f"* `{path}`")
                w("")
    w("")

    w("## 触れていないもの")
    w("")
    w("| 事項 | 扱い |")
    w("|---|---|")
    for name, note in report["not_changed"].items():
        w(f"| `{name}` | {note} |")
    w("")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--md-out", type=Path, default=DEFAULT_MD)
    args = parser.parse_args(argv)

    # 保存 Report は Package の履歴入力。通常実行でも別名出力だけを許す。
    targets = [args.out.resolve(), args.md_out.resolve()]
    if any(p in {DEFAULT_OUT.resolve(), DEFAULT_MD.resolve()} for p in targets):
        parser.exit(1, "FROZEN_REPORT: 出力先を別名で指定してください\n")
    if targets[0] == targets[1] or any(p.exists() for p in targets):
        parser.exit(1, "OUTPUT_ALREADY_EXISTS: 未使用の出力先を指定してください\n")
    try:
        report = measure()
    except (ValueError, UnicodeError, OSError):
        parser.exit(1, "AUDIT_SOURCE_INVALID: 監査入力を解析できません\n")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.md_out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with args.out.open("x", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        with args.md_out.open("x", encoding="utf-8", newline="\n") as output:
            output.write(_markdown(report))
    except OSError:
        parser.exit(1, "AUDIT_OUTPUT_FAILED: 出力先へ新規保存できません\n")

    counts = report["counts"]
    print(f"{args.out.name}")
    print(f"参照 {counts['total']} 件 / v1 の中身を読む {counts['v1_reads_content']} 件")
    print(f"うち将来の判定に効く {counts['v1_reads_content_forward']} 件 / ", end="")
    print(f"v3 の中身を読む {counts['v3_reads_content']} 件")
    print(f"Gap {len(report['gaps'])} 件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
