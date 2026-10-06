"""準備状況の監査器が、散文を判定材料へ戻さないことを固定する。

実装判定の根拠は呼出経路だけであり続ける。散文を根拠にする書き方が 1 箇所でも
戻ると、`src/` の docstring を直すだけで監査の結論が動く状態へ戻ってしまう。

v1 後片付けの保存 Report（非公開の監査記録）と、それを作る Tool の検査は
公開用の配布コピーに収録しない。`tests/private_history/` の試験が確かめる。
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ANALYZER = REPO_ROOT / "tools/audit_chat_provider_readiness.py"


def test_the_analyzer_reads_no_source_text() -> None:
    """監査器が Source の本文を丸ごと読み込んでいないこと。

    `read_text()` して `in` で探す書き方が戻ると、docstring や Comment が判定材料
    へ戻る。`src/` を読む経路は AST 解析だけに限る。

    見るのは AST である。「`read_text` という語が出てこない」ではなく、
    **`read_text()` の戻りに対して部分一致を掛けている箇所**を探す。
    """
    tree = ast.parse(ANALYZER.read_text(encoding="utf-8"), filename=str(ANALYZER))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, ast.In | ast.NotIn) for op in node.ops):
            continue
        for side in node.comparators:
            call = side.func if isinstance(side, ast.Call) else None
            if isinstance(call, ast.Attribute) and call.attr in {"read_text", "lower", "read"}:
                offenders.append(f"line {node.lineno}")
    assert offenders == [], f"Source 本文への部分一致が残っている: {offenders}"
