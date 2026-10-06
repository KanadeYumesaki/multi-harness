"""TokenCounterPort。決定論的なToken計数を注入するための抽象。

§1.12は`TokenProfileSnapshot`へTokenizer Name／Version／Counting Adapter Versionを
記録することを要求し、`UNKNOWN`・Version不明・ModelとTokenizer不一致を停止条件とする。
したがって計数器は「数」だけを返してはならない。**どのTokenizerが、どの計数Adapterで、
どのAssuranceで数えたか**を必ず名乗る。

`estimate_assurance`の意味を厳密に扱う。

* `EXACT`        : 実Tokenizerによる実数
* `CONSERVATIVE` : 実測が**決してこれを超えない**上界。平均や経験則ではない
* `UNKNOWN`      : 上界を保証できない近似。Application層がFail-Closedで停止させる

「平均的にだいたい合う」推定を`CONSERVATIVE`と名乗ってはならない。過小評価は
Provider上限超過を招き、§0.5「Provider上限超過呼出0件」に反する。

Portであり、`design-source/registries/schemas.yaml`の統制対象ではない。
新しいSchema IDやEvent IDを導入しない。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from harness.domain.context_budget import EstimateAssurance, TokenProfileSnapshot

__all__ = ["TokenCount", "TokenCounterPort"]


@dataclass(frozen=True, slots=True)
class TokenCount:
    """計数結果と、それを出した計数器の同一性。"""

    tokens: int
    tokenizer_name: str
    tokenizer_version: str
    counting_adapter_version: str
    estimate_assurance: EstimateAssurance

    def __post_init__(self) -> None:
        if self.tokens < 0:
            raise ValueError("tokens must not be negative")
        if not self.tokenizer_name or not self.tokenizer_version:
            raise ValueError("token counter must declare its tokenizer identity")
        if not self.counting_adapter_version:
            raise ValueError("token counter must declare its counting adapter version")


class TokenCounterPort(Protocol):
    """Text 1件をToken数へ写す決定論的な関数。

    同一 `(text, profile)` からは常に同一 `TokenCount` を返さなければならない。
    Clock、乱数、Network、Filesystemへ依存してはならない。
    """

    def count(self, text: str, *, profile: TokenProfileSnapshot) -> TokenCount: ...
