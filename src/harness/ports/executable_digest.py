"""実行体の SHA-256 を測る Port。

`RuntimeEnvelopeSpec` は「Executable は絶対 Path ＋ SHA-256」を要求する
（§6 制約）。宣言された SHA-256 をそのまま信じると、宣言と実物がずれた
まま Plan が立つ。**宣言を実測と突き合わせる**ために測る側を Port にする。

Application 層は Filesystem を直接触らない（層の依存規則）。実測できない場合は
`None` を返さず送出する。読めなかったことを「一致した」に倒さない（不変条件#9）。
"""

from __future__ import annotations

from typing import Protocol

from harness.domain.hashing import ContentHash

__all__ = ["ExecutableDigestPort", "ExecutablePathMissing"]


class ExecutablePathMissing(Exception):
    """宣言された実行体を測れなかった。**一致とみなさない。**"""


class ExecutableDigestPort(Protocol):
    def digest(self, absolute_path: str) -> ContentHash:
        """絶対 Path の実行体の SHA-256。

        読めない場合は `ExecutablePathMissing` を送出する。`None` を返さない。
        `None` を返すと呼出側が「測れなかった」を「差が無かった」へ倒しうる。
        """
        ...
