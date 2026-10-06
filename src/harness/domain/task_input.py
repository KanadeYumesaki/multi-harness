"""合成 Task 入力の形式判定。**Source もデータとして扱う。**

## 何を決める規則か

指示書 §6 CC-03 は「Markdown／Text／JSON／Source を入力する Task Loader」と
「Source もデータとして扱い、実行や Control Role 昇格をさせない」を求める。

ここで決めるのは**形式の名前と、その形式として読めるかどうか**だけである。
中身の意味を解釈しない。とくに Source を「実行できるもの」として扱わない。
判定結果は `ContextFragment` の Role へ影響しない。Role は §3.6 手順2 の
Control／Data 検証が決めるものであり、拡張子が決めてよいものではない。

## 拡張子で決める理由

拡張子は**要求 Path に人が書いた宣言**であって、中身の推測ではない。
中身から形式を当てにいくと、同じ Bytes が実行環境や Library 版で違う形式に
見えうる。Plan Content は決定的でなければならない（不変条件#4）。

## 読めない入力を通さない

* UTF-8 として復号できない Bytes は、どの形式でも拒否する。
* `.json` を名乗って JSON として読めない Bytes は拒否する。

どちらも「読めなかった」を「空だった」へ倒さない。倒せば、壊れた入力から
Plan が立ち、その Plan が承認されうる。
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Final

from harness.domain.errors import ErrorCode, HarnessError

__all__ = ["TaskFormat", "classify_task_bytes", "decode_task_text"]


class TaskFormat(Enum):
    """Task 入力の形式。`INPUT_ARTIFACT_CLASSIFIED` の分類値として使う。"""

    MARKDOWN = "TASK_MARKDOWN"
    TEXT = "TASK_TEXT"
    JSON = "TASK_JSON"
    SOURCE = "TASK_SOURCE"


#: 拡張子から形式への写像。**Code Point 昇順で持ち、反復順に依存しない**（不変条件#6）。
_SUFFIX_FORMATS: Final[tuple[tuple[str, TaskFormat], ...]] = (
    (".json", TaskFormat.JSON),
    (".md", TaskFormat.MARKDOWN),
    (".markdown", TaskFormat.MARKDOWN),
    (".py", TaskFormat.SOURCE),
    (".sh", TaskFormat.SOURCE),
    (".sql", TaskFormat.SOURCE),
    (".text", TaskFormat.TEXT),
    (".toml", TaskFormat.SOURCE),
    (".ts", TaskFormat.SOURCE),
    (".txt", TaskFormat.TEXT),
    (".yaml", TaskFormat.SOURCE),
    (".yml", TaskFormat.SOURCE),
)


def decode_task_text(payload: bytes, *, where: str) -> str:
    """UTF-8 として厳密に復号する。置換文字で埋めない。

    `errors="replace"` を使うと、壊れた Byte 列が U+FFFD になって「読めた」
    ことになる。読めない入力は読めないまま止める。
    """
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HarnessError(
            ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
            f"{where} is not valid UTF-8: {exc.reason} at byte {exc.start}",
        ) from None


def classify_task_bytes(relative_path: str, payload: bytes) -> TaskFormat:
    """要求 Path の拡張子で形式を決め、その形式として読めることを確かめる。

    未知の拡張子は**既定値へ倒さない**。倒すと、想定していない形式が
    黙って Text として Context へ入る。
    """
    lowered = relative_path.lower()
    matched: TaskFormat | None = None
    for suffix, task_format in _SUFFIX_FORMATS:
        if lowered.endswith(suffix):
            matched = task_format
            break
    if matched is None:
        raise HarnessError(
            ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
            f"task input {relative_path!r} has no supported format suffix "
            f"({sorted(suffix for suffix, _ in _SUFFIX_FORMATS)})",
        )

    text = decode_task_text(payload, where=f"task input {relative_path!r}")
    if matched is TaskFormat.JSON:
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                f"task input {relative_path!r} is not valid JSON: {exc.msg} at line {exc.lineno}",
            ) from None
    return matched
