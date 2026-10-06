#!/usr/bin/env python3
"""受入Caseと試験の対応を検査する（レビュー MAJOR）。

## なぜ必要か

Registryには110件のMVP0-A Caseがあるが、試験との間に**形式的な紐付けが
無かった**。試験名をCase名に寄せているだけで、機械的には追えない。

その状態では次が区別できない。

  * Caseに対応する試験がある
  * 試験が無いが、まだ実装していないと分かっている
  * 試験が無いことに誰も気付いていない

3つ目が最も危ない。「110件のうち何件を実際に検証したのか」を
人手の記憶に頼ることになる。

## 仕組み

試験へ `@pytest.mark.case("AT-XXX-001/CASE_ID", ...)` を付ける。
本Toolが試験Fileを構文解析してMarkerを集め、Registryと突き合わせる。

`case-coverage.yaml` の `not_implemented` は**手で維持する**。
未実装であること自体は問題ではない。問題なのは、未実装だと
気付かれていないことである。明示的に書けば「意図して未着手」になる。

  covered ∪ not_implemented == MVP0-A の全Case
  covered ∩ not_implemented == 空

どちらにも入っていないCaseがあれば落ちる。試験を足してMarkerを付ければ
自動的に covered へ移り、`not_implemented` から消し忘れれば重複で落ちる。

## Evidence とは別物

ここで確認するのは「対応する試験が存在するか」だけである。
`evidence_status` は `UNVERIFIED` のまま据え置く。Evidence収集の仕組みは
Tier 5 の対象であり、対応表とは別に整備する。
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import defaultdict
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS = REPO_ROOT / "tests"
REGISTRIES = REPO_ROOT / "design-source" / "registries"
# 試験側の状態であって設計書由来の正本ではない。`design-source/registries/` へ
# 置くと spec-manifest の Hash に入り、試験を1件足すたびに設計書側の
# 生成物Hashが動く。正本と進捗を同じ場所へ混ぜない。
COVERAGE = REPO_ROOT / "tests" / "case-coverage.yaml"
PHASE = "MVP0-A"


def registry_cases() -> set[str]:
    """`AT-XXX-001/CASE_ID` 形式でMVP0-A Caseを返す。"""
    rows = yaml.safe_load((REGISTRIES / "tests.yaml").read_text(encoding="utf-8"))["test_cases"]
    return {
        f"{row['test_id']}/{row['case_id']}"
        for row in rows
        if PHASE in (row.get("phase_scope") or [])
    }


def _marker_case_ids(decorator: ast.expr) -> list[str]:
    """`@pytest.mark.case("A", "B")` から文字列引数を取り出す。"""
    if not isinstance(decorator, ast.Call):
        return []
    func = decorator.func
    if not (isinstance(func, ast.Attribute) and func.attr == "case"):
        return []
    inner = func.value
    if not (isinstance(inner, ast.Attribute) and inner.attr == "mark"):
        return []
    found: list[str] = []
    for argument in decorator.args:
        if not (isinstance(argument, ast.Constant) and isinstance(argument.value, str)):
            raise ValueError("pytest.mark.case takes string literals only")
        found.append(argument.value)
    return found


def scan_markers() -> dict[str, list[str]]:
    """Case ID -> それを主張する試験（`file::function`）。"""
    covered: dict[str, list[str]] = defaultdict(list)
    for path in sorted(TESTS.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for decorator in node.decorator_list:
                for case_id in _marker_case_ids(decorator):
                    covered[case_id].append(f"{path.relative_to(REPO_ROOT)}::{node.name}")
    return dict(covered)


def declared_unimplemented() -> dict[str, str]:
    """未実装宣言を読む。理由の無い宣言と重複行は受け付けない。

    理由を書かせるのが本Registryの要点である。Case IDの羅列だけなら、
    「未着手」と「諦めた」と「忘れた」が同じ見た目になる。
    """
    if not COVERAGE.is_file():
        return {}
    document = yaml.safe_load(COVERAGE.read_text(encoding="utf-8")) or {}
    rows = document.get("not_implemented") or []

    declared: dict[str, str] = {}
    for row in rows:
        case_id = str(row["case"])
        reason = str(row.get("reason") or "").strip()
        if not reason:
            raise ValueError(f"not_implemented の理由が空: {case_id}")
        if case_id in declared:
            raise ValueError(f"not_implemented に重複がある: {case_id}")
        declared[case_id] = reason
    return declared


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true", help="対応状況を一覧表示する")
    args = parser.parse_args()

    cases = registry_cases()
    covered = scan_markers()
    pending = declared_unimplemented()

    problems: list[str] = []

    unknown = sorted(set(covered) - cases)
    if unknown:
        problems.append(
            "Registryに無いCaseをMarkerが主張している（誤記か、Phase外）:\n  "
            + "\n  ".join(unknown)
        )

    unknown_pending = sorted(set(pending) - cases)
    if unknown_pending:
        problems.append(
            "not_implemented にRegistry外のCaseがある:\n  " + "\n  ".join(unknown_pending)
        )

    both = sorted(set(covered) & set(pending))
    if both:
        problems.append(
            "試験があるのに not_implemented へ残っている（消し忘れ）:\n  " + "\n  ".join(both)
        )

    uncovered = sorted(cases - set(covered) - set(pending))
    if uncovered:
        problems.append(
            "試験もnot_implemented宣言も無いCase:\n  "
            + "\n  ".join(uncovered)
            + "\n（試験へ @pytest.mark.case を付けるか、case-coverage.yaml へ理由付きで登録する）"
        )

    if args.report:
        print(f"{PHASE}: {len(cases)} Case")
        print(f"  試験あり       : {len(covered)}")
        print(f"  未実装（宣言） : {len(pending)}")
        print(f"  未分類         : {len(uncovered)}")
        for case_id in sorted(covered):
            print(f"    [covered] {case_id}  <- {', '.join(covered[case_id])}")

    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 3

    print(
        f"case coverage ok: {len(covered)} covered / {len(pending)} declared unimplemented "
        f"/ {len(cases)} total ({PHASE})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
