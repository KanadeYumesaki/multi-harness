"""Token計数Strategy。実Tokenizer導入までの安全な上界と、分析専用の近似。

## なぜ「4文字=1 Token」を`CONSERVATIVE`と呼んではいけないか

初版は「ASCII 4文字=1 Token、非ASCII 1符号位置=1 Token」で数え、
`estimate_assurance=CONSERVATIVE`を名乗っていた。これは誤りである。

* 4文字=1 Tokenは英語の**平均**であって上界ではない。記号列、識別子、
  Base64、URL、稀な語は1文字1 Tokenへ近づき、実測が推定を上回る。
* 非ASCII 1符号位置=1 Tokenも上界ではない。Vocabularyに無い符号位置は
  Byte Fallbackで**複数**Tokenへ分解される。UTF-8で3 byteのCJKは
  最悪3 Tokenになり得る。

過小評価した推定でContext Limitを判断すると、Provider呼出が上限超過で失敗する。
§0.5「Token統制：Provider上限超過呼出0件」に反する。

## 2つのStrategyを分ける

| Strategy | Assurance | 用途 |
|---|---|---|
| :class:`ByteBoundTokenCounter` | `CONSERVATIVE` | 実Tokenizer導入前の既定。安全な上界 |
| :class:`HeuristicTokenCounter` | `UNKNOWN` | 分析・比較専用。**Runtimeで使えない** |

`HeuristicTokenCounter`は`UNKNOWN`を名乗るため、
:meth:`TokenProfileSnapshot.require_usable` とApplication層のAssurance検査が
Fail-Closedで停止させる。「近似を本番で使わない」ことを注意書きではなく型と
判定で強制する。

### Byte上界の根拠と限界

Byte-level BPE（GPT-2／cl100k／o200k系）とByte Fallback付きSentencePieceでは、
Vocabularyに256個の単byte Tokenが必ず含まれる。Mergeは常にToken数を**減らす**
方向にしか働かないため、Token数がUTF-8 byte数を超えることはない。
よってUTF-8 byte数は上界である。

上界であって近似ではない。英語では実測の約4倍を見積もるため予算を浪費する。
これは安全側の浪費であり、§6「安全側に倒す。可用性より安全停止を優先する」に従う。

**Byte Fallbackを持たないTokenizerには、この上界は成り立たない。**
実Tokenizerを導入するTASK-LLM-002では、本Moduleではなく実Tokenizerを
`TokenCounterPort`実装として注入し、`estimate_assurance=EXACT`を名乗らせる。

## 正規化を行わない

不変条件#22は「正規化はUCD 14.0割当済み符号位置Guardの後に標準NFCを使う」と定める。
本Moduleはこの Guard を持たないため、正規化を一切行わない。与えられた`str`を
そのまま数える。NFC正規化はMasking Pipeline（ADR-007）の責務であり、
ここで独自に前処理を挟むとGuardを迂回した正規化経路を1本増やすことになる。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from harness.domain.context_budget import EstimateAssurance, TokenProfileSnapshot
from harness.ports.token_counter import TokenCount

__all__ = [
    "BYTE_BOUND_TOKENIZER_NAME",
    "BYTE_BOUND_TOKENIZER_VERSION",
    "COUNTING_ADAPTER_VERSION",
    "HEURISTIC_TOKENIZER_NAME",
    "HEURISTIC_TOKENIZER_VERSION",
    "ByteBoundTokenCounter",
    "HeuristicTokenCounter",
]

# Strategy ID。`TokenProfileSnapshot`がこの名前を宣言した場合だけ当該計数器を使える。
BYTE_BOUND_TOKENIZER_NAME: Final[str] = "harness-utf8-byte-upper-bound"
BYTE_BOUND_TOKENIZER_VERSION: Final[str] = "1.0.0"

HEURISTIC_TOKENIZER_NAME: Final[str] = "harness-approx-codepoint-class"
HEURISTIC_TOKENIZER_VERSION: Final[str] = "1.0.0"

# §1.12「Counting Library／Adapter Version」。Snapshotへ束縛する。
COUNTING_ADAPTER_VERSION: Final[str] = "harness-token-counter/2"

# ASCII語1 Tokenあたりの平均文字数。**平均であって上界ではない**（上記参照）。
_ASCII_CHARS_PER_TOKEN: Final[int] = 4

_ASCII_WORD: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
)


@dataclass(frozen=True, slots=True)
class ByteBoundTokenCounter:
    """UTF-8 byte数を返す`TokenCounterPort`実装。実Tokenizer導入前の既定。

    Byte Fallbackを持つTokenizerに対する安全な上界であり、
    `estimate_assurance=CONSERVATIVE`を名乗る資格がある。
    """

    def count(self, text: str, *, profile: TokenProfileSnapshot) -> TokenCount:
        profile.require_counter_identity(
            tokenizer_name=BYTE_BOUND_TOKENIZER_NAME,
            tokenizer_version=BYTE_BOUND_TOKENIZER_VERSION,
            counting_adapter_version=COUNTING_ADAPTER_VERSION,
        )
        return TokenCount(
            tokens=len(text.encode("utf-8")),
            tokenizer_name=BYTE_BOUND_TOKENIZER_NAME,
            tokenizer_version=BYTE_BOUND_TOKENIZER_VERSION,
            counting_adapter_version=COUNTING_ADAPTER_VERSION,
            estimate_assurance=EstimateAssurance.CONSERVATIVE,
        )


@dataclass(frozen=True, slots=True)
class HeuristicTokenCounter:
    """文字種別の近似計数。**Runtimeで使えない**（`UNKNOWN`を名乗る）。

    実Tokenizerの傾向に近い数を出すため、Byte上界がどれだけ過大かを測る用途、
    および実Tokenizer導入時の比較基準として残す。
    `estimate_assurance=UNKNOWN`であるため、Context組立へ渡すと§1.12により停止する。
    """

    def count(self, text: str, *, profile: TokenProfileSnapshot) -> TokenCount:
        profile.require_counter_identity(
            tokenizer_name=HEURISTIC_TOKENIZER_NAME,
            tokenizer_version=HEURISTIC_TOKENIZER_VERSION,
            counting_adapter_version=COUNTING_ADAPTER_VERSION,
        )
        return TokenCount(
            tokens=_heuristic_tokens(text),
            tokenizer_name=HEURISTIC_TOKENIZER_NAME,
            tokenizer_version=HEURISTIC_TOKENIZER_VERSION,
            counting_adapter_version=COUNTING_ADAPTER_VERSION,
            estimate_assurance=EstimateAssurance.UNKNOWN,
        )


def _heuristic_tokens(text: str) -> int:
    """符号位置の種別ごとに数える純粋関数。上界ではない。

    * ASCII語（英数と`_`）の連続 : `ceil(長さ / 4)` Token
    * 空白 : 0 Token（隣接語へ吸収される）
    * それ以外の符号位置 : 1文字あたり1 Token

    走査順は文字列の並び順であり、`set`や`dict`の反復順へ依存しない（不変条件#6）。
    """
    tokens = 0
    ascii_run = 0
    for char in text:
        if char in _ASCII_WORD:
            ascii_run += 1
            continue
        if ascii_run:
            tokens += _ceil_div(ascii_run, _ASCII_CHARS_PER_TOKEN)
            ascii_run = 0
        if char.isspace():
            continue
        tokens += 1
    if ascii_run:
        tokens += _ceil_div(ascii_run, _ASCII_CHARS_PER_TOKEN)
    return tokens


def _ceil_div(value: int, divisor: int) -> int:
    return -(-value // divisor)
