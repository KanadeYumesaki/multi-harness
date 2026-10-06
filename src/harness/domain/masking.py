"""ADR-007 マスキングのDomain部分（純粋関数）。

**LLMは座標（Span）しか返さない。置換は本Moduleの決定論Rewriterが行う。**
LLMに本文を返させる方式は、欠落・重複・並べ替え・意味改変を決定論的に検出できない
（断片を原文の短い部分文字列へ縮める改変が順序・部分文字列検査を通過する）。
座標だけなら、LLMが影響できるのは「どこを隠すか」であって
「何が書かれるか」ではない。

Span検証・Union・Rewriteはすべてここで完結し、I/Oを持たない。
Scannerとの接続、Policy読込み、LLM呼出しは`infrastructure/masking/`が行う。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Final

from harness.domain.errors import ErrorCode

# 決定論Rewriterの版数。置換規則を変えたら上げる。
# Receiptへ記録し、同じSpan集合から同じ本文が再現できることの根拠にする。
REWRITER_VERSION: Final[str] = "DETERMINISTIC_SPAN_REWRITER_V1"

# 符号位置 -> Extended_Pictographic か否か。
PictographicLookup = Callable[[int], bool]

__all__ = [
    "REWRITER_VERSION",
    "MaskingRejection",
    "PictographicLookup",
    "RejectionReason",
    "Span",
    "SpanConstraints",
    "apply_mask_tokens",
    "contains_risky_codepoint",
    "merge_candidate_and_llm_spans",
    "validate_spans",
]


class RejectionReason(Enum):
    """§ADR-007 §3 の拒否理由。対応するError Codeを持つ。"""

    SPAN_INVALID = ErrorCode.MASKING_SPAN_INVALID
    SPAN_CONFLICT = ErrorCode.MASKING_SPAN_CONFLICT
    CATEGORY_UNKNOWN = ErrorCode.MASKING_CATEGORY_UNKNOWN
    RATIO_EXCEEDED = ErrorCode.MASKING_RATIO_EXCEEDED
    GRAPHEME_SPLIT = ErrorCode.MASKING_GRAPHEME_SPLIT
    OUTPUT_MALFORMED = ErrorCode.MASKER_OUTPUT_MALFORMED
    VERIFICATION_FAILED = ErrorCode.MASKING_VERIFICATION_FAILED

    @property
    def error_code(self) -> ErrorCode:
        return self.value


@dataclass(frozen=True, slots=True)
class MaskingRejection:
    """拒否。`MASKING_RESULT` 名前空間の `REJECTED`。"""

    reason: RejectionReason
    detail: str

    @property
    def state(self) -> str:
        return "REJECTED"

    @property
    def error_code(self) -> ErrorCode:
        return self.reason.error_code


@dataclass(frozen=True, slots=True)
class Span:
    """マスク範囲。NFC正規化済みテキスト上のUnicode scalar value index、半開区間。

    Byte offsetもUTF-16 code unit indexも使わない。前者は多バイト境界の誤りを、
    後者はSurrogate Pairの扱いを持ち込む。
    """

    start: int
    end: int
    category: str

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError(f"invalid span [{self.start}, {self.end})")
        if not self.category:
            raise ValueError("span category must not be empty")

    @property
    def length(self) -> int:
        return self.end - self.start

    def contains(self, other: Span) -> bool:
        return self.start <= other.start and other.end <= self.end

    def overlaps(self, other: Span) -> bool:
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True, slots=True)
class SpanConstraints:
    """`masking-policy.yaml` の `span_constraints` を写した制約。

    既定値を持たせない。Policyに項目が無ければ呼出側がFail-Closedにする
    （`ratio_unset: REJECT`）。
    """

    max_spans: int
    max_mask_ratio: float
    max_single_llm_addition_ratio: float
    min_span_length: int
    allow_overlap: bool
    allow_nesting: bool
    require_sorted_by_start: bool
    risky_classes: frozenset[str]
    # `Extended_Pictographic` 判定表。Policyから作られてここに載る。
    pictographic: PictographicLookup


# ---------------------------------------------------------------------------
# 危険符号位置Guard（CONSERVATIVE_RISKY_CODEPOINT_GUARD_V1）
# ---------------------------------------------------------------------------

_HANGUL_JAMO_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x1100, 0x11FF),
    (0xA960, 0xA97F),
    (0xD7B0, 0xD7FF),
)
_VARIATION_SELECTOR_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0xFE00, 0xFE0F),
    (0xE0100, 0xE01EF),
)
_EMOJI_MODIFIER_RANGE: Final[tuple[int, int]] = (0x1F3FB, 0x1F3FF)
_REGIONAL_INDICATOR_RANGE: Final[tuple[int, int]] = (0x1F1E6, 0x1F1FF)
_TAG_CHARACTER_RANGE: Final[tuple[int, int]] = (0xE0020, 0xE007F)
_ZERO_WIDTH_JOINER: Final[int] = 0x200D

# `Extended_Pictographic` はUnicodeの実データを引く。
#
# 以前は `U+2190–U+2BFF` と `U+1F000–U+1FAFF` の範囲で近似していた。
# 「絵文字はこのあたりに固まっている」という当て推量であり、実際には
# 矢印・星・囲み数字を丸ごと含んでいた。箇条書きの丸数字
# （`①山田太郎②佐藤花子`）が `MASKING_GRAPHEME_SPLIT` で拒否される。
# Grapheme Clusterと無関係な文字まで巻き込む判定だった。
#
# 近似の幅を手で調整しても、根拠が「たぶんこの辺」のままでは同じことが起きる。
# 判定表はinfrastructure側が読み込んで注入する（domainはI/Oを持たない）。
# 判定表はModule Globalに置かない。**呼出しごとに明示的に渡す。**
#
# 一度Globalへ置いたが、注入した別の試験が先に走ったかどうかで結果が変わる
# 順序依存を生んだ。ローカルでは通り、CIで11件落ちた。隠れた状態は
# 「たまたま動いていた」を作る。引数にすれば渡し忘れは型検査で止まる。


# `_risky_class` が返しうる名前の全体。Registryの `grapheme_risky_classes` と
# 一致していなければならない。
#
# 一致していないと**拒否が静かに消える**。判定は
# `_risky_class(...) in risky_classes` の形であり、Registryに無い名前を
# 返した瞬間、その文字は「危険ではない」ものとして通る。Typoでも、
# Registryへ追加したのに実装を足し忘れた場合でも、症状は同じ「何も起きない」
# になる。Guardが緩む方向へ黙って倒れる形なので、名前の集合をここに固定し、
# Policy読込み時に突き合わせる。
RISKY_CLASS_NAMES: Final[frozenset[str]] = frozenset(
    {
        "COMBINING_MARK",
        "SPACING_MARK",
        "ZERO_WIDTH_JOINER",
        "VARIATION_SELECTOR",
        "HANGUL_JAMO",
        "EMOJI_MODIFIER",
        "REGIONAL_INDICATOR",
        "TAG_CHARACTER",
        "EXTENDED_PICTOGRAPHIC",
        "FORMAT_JOINER",
    }
)


def _in_ranges(codepoint: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(low <= codepoint <= high for low, high in ranges)


def _risky_class(character: str, pictographic: PictographicLookup) -> str | None:
    """危険クラス名を返す。該当しなければNone。

    **具体的な符号位置・範囲を、一般カテゴリより先に見る。**

    初版は `Mn`／`Mc` を先頭に置いていた。ところが Variation Selector
    （`U+FE00–FE0F`、`U+E0100–E01EF`）は全て `Mn` である。そのため
    `VARIATION_SELECTOR` の分岐へは**一度も到達しなかった**。
    Registryが宣言しているクラスを実装が返せない状態にあたる。

    拒否そのものは `COMBINING_MARK` として起きていたので挙動は安全側に
    留まっていたが、Receiptへ記録されるクラス名は実態とずれる。
    「なぜ拒否されたか」を後から読む側が、結合文字の話だと誤読する。

    どのクラスを返しても拒否されることに変わりはない（10種すべてが
    `risky_classes` に入っていることをPolicy読込みで強制している）。
    ここで決まるのは**名前の精度**である。
    """
    codepoint = ord(character)
    category = unicodedata.category(character)

    # 具体（符号位置・範囲）
    if codepoint == _ZERO_WIDTH_JOINER:
        return "ZERO_WIDTH_JOINER"
    if _in_ranges(codepoint, _VARIATION_SELECTOR_RANGES):
        return "VARIATION_SELECTOR"
    if _TAG_CHARACTER_RANGE[0] <= codepoint <= _TAG_CHARACTER_RANGE[1]:
        return "TAG_CHARACTER"
    if _EMOJI_MODIFIER_RANGE[0] <= codepoint <= _EMOJI_MODIFIER_RANGE[1]:
        return "EMOJI_MODIFIER"
    if _REGIONAL_INDICATOR_RANGE[0] <= codepoint <= _REGIONAL_INDICATOR_RANGE[1]:
        return "REGIONAL_INDICATOR"
    if _in_ranges(codepoint, _HANGUL_JAMO_RANGES):
        return "HANGUL_JAMO"

    # 一般（Unicode General Category と Vendor表）
    if category == "Mn":
        return "COMBINING_MARK"
    if category == "Mc":
        return "SPACING_MARK"
    if pictographic(codepoint):
        return "EXTENDED_PICTOGRAPHIC"
    if category == "Cf":
        return "FORMAT_JOINER"
    return None


def contains_risky_codepoint(
    text: str,
    span: Span,
    risky_classes: frozenset[str],
    pictographic: PictographicLookup,
) -> str | None:
    """SpanがGrapheme Clusterを割り得るか、保守的に判定する。

    UAX #29の完全実装はstdlibに無く、追加依存も入れない（ADR-007）。
    代わりに「危険クラスを含むSpanを拒否する」保守側の近似を使う。
    通常のASCII／安全文字列は通る。

    Span内の符号位置に加え、**直後の符号位置**も見る。結合文字の直前で切ると
    Clusterを割るためである。
    """
    for index in range(span.start, min(span.end, len(text))):
        found = _risky_class(text[index], pictographic)
        if found is not None and found in risky_classes:
            return found
    if span.end < len(text):
        found = _risky_class(text[span.end], pictographic)
        if found is not None and found in risky_classes:
            return found
    return None


# ---------------------------------------------------------------------------
# Union（Scan#1候補 と LLM追加Span）
# ---------------------------------------------------------------------------


def require_sorted_llm_spans(
    llm_spans: tuple[Span, ...], constraints: SpanConstraints
) -> MaskingRejection | None:
    """Masker が返した Span が開始位置の昇順であることを確かめる（§1.16.3.1）。

    **Union より前に呼ぶ。** Union は結果を `sorted()` するので、後から見ても
    順序は必ず整っている。順序が壊れた出力を返す Masker を、そこでは検出できない。

    正規化して受理すると「順序不正を検出する」という要件そのものが消える。
    Owner Decision MASK-3-A は正規化ではなく拒否を選んだ。
    """
    if not constraints.require_sorted_by_start:
        return None
    previous: Span | None = None
    for span in llm_spans:
        if previous is not None and span.start < previous.start:
            return MaskingRejection(
                RejectionReason.SPAN_INVALID,
                f"masker spans are not sorted by start: "
                f"[{previous.start},{previous.end}) then [{span.start},{span.end})",
            )
        previous = span
    return None


def merge_candidate_and_llm_spans(
    candidates: tuple[Span, ...], llm_spans: tuple[Span, ...]
) -> tuple[Span, ...] | MaskingRejection:
    """決定論候補とLLM Spanを統合する（ADR-007 §3）。

    * 同一座標・同一カテゴリ → 1件へDeduplicate
    * LLM Spanが同一カテゴリの候補を**完全に包含** → 安全方向の
      Containment Expansion として包含Spanへ正規化する
    * 1つのLLM Spanが複数候補を包含、カテゴリ相違、部分重複 → `SPAN_CONFLICT`

    LLMは候補を**減らせない**。包含による拡大だけが許される。
    """
    merged: list[Span] = list(candidates)
    for llm in llm_spans:
        if llm in merged:
            continue  # 完全一致はDeduplicate

        contained = [
            candidate
            for candidate in candidates
            if llm.contains(candidate) and llm.category == candidate.category
        ]
        overlapping = [candidate for candidate in candidates if llm.overlaps(candidate)]

        if len(contained) > 1:
            return MaskingRejection(
                RejectionReason.SPAN_CONFLICT,
                f"LLM span [{llm.start},{llm.end}) contains {len(contained)} candidates",
            )
        if contained:
            # Containment Expansion。候補を包含Spanへ置き換える。
            merged = [span for span in merged if span is not contained[0]]
            merged.append(llm)
            continue
        if overlapping:
            reasons = ", ".join(f"[{c.start},{c.end}):{c.category}" for c in overlapping)
            return MaskingRejection(
                RejectionReason.SPAN_CONFLICT,
                f"LLM span [{llm.start},{llm.end}):{llm.category} partially overlaps "
                f"or conflicts in category with {reasons}",
            )
        merged.append(llm)

    return tuple(sorted(merged, key=lambda span: (span.start, span.end)))


def llm_added_length(merged: tuple[Span, ...], candidates: tuple[Span, ...]) -> int:
    """LLMが追加した分の符号位置数。

    Mask Ratioの分子はここだけとする（`ratio_basis:
    LLM_ADDITIONAL_UNION_CODE_POINTS`、`deterministic_scan1_excluded: true`）。
    決定論Scan#1候補を分子へ含めると、個人情報密度の高い文書を
    決定論スキャナ単独の判断で拒否してしまう。Circuit BreakerはLLMの
    暴走検出が目的であり、権限を持つ決定論側の判断を却下してはならない。
    """
    covered: set[int] = set()
    for span in candidates:
        covered.update(range(span.start, span.end))
    added = 0
    for span in merged:
        added += sum(1 for index in range(span.start, span.end) if index not in covered)
    return added


# ---------------------------------------------------------------------------
# 検証
# ---------------------------------------------------------------------------


def validate_spans(
    text: str,
    merged: tuple[Span, ...],
    candidates: tuple[Span, ...],
    *,
    known_categories: frozenset[str],
    constraints: SpanConstraints,
) -> MaskingRejection | None:
    """ADR-007 §3 の Span 検証。合格なら`None`。

    順序・非重複・非入れ子を強制するのは、Rewriterの結果が順序依存に
    ならないようにするためである。
    """
    if not merged:
        return None

    if len(merged) > constraints.max_spans:
        return MaskingRejection(
            RejectionReason.SPAN_INVALID,
            f"{len(merged)} spans exceed max_spans={constraints.max_spans}",
        )

    previous: Span | None = None
    for span in merged:
        if span.end > len(text):
            return MaskingRejection(
                RejectionReason.SPAN_INVALID,
                f"span [{span.start},{span.end}) exceeds text length {len(text)}",
            )
        if span.length < constraints.min_span_length:
            return MaskingRejection(
                RejectionReason.SPAN_INVALID,
                f"span [{span.start},{span.end}) is shorter than "
                f"min_span_length={constraints.min_span_length}",
            )
        if span.category not in known_categories:
            return MaskingRejection(
                RejectionReason.CATEGORY_UNKNOWN,
                f"category {span.category!r} is not registered in masking-policy.yaml",
            )
        if previous is not None:
            if constraints.require_sorted_by_start and span.start < previous.start:
                return MaskingRejection(
                    RejectionReason.SPAN_INVALID, "spans are not sorted by start"
                )
            if not constraints.allow_overlap and previous.overlaps(span):
                return MaskingRejection(
                    RejectionReason.SPAN_INVALID,
                    f"spans [{previous.start},{previous.end}) and "
                    f"[{span.start},{span.end}) overlap",
                )
            if not constraints.allow_nesting and previous.contains(span):
                return MaskingRejection(RejectionReason.SPAN_INVALID, "spans are nested")
        risky = contains_risky_codepoint(
            text, span, constraints.risky_classes, constraints.pictographic
        )
        if risky is not None:
            return MaskingRejection(
                RejectionReason.GRAPHEME_SPLIT,
                f"span [{span.start},{span.end}) touches {risky}; may split a grapheme cluster",
            )
        previous = span

    # Mask Ratio。分子はLLM追加分だけ（`deterministic_scan1_excluded: true`）。
    #
    # `ratio_empty_text: REJECT`。0除算を避けて比率検査を飛ばすと、
    # **空文だけが上限検査を通り抜ける**。飛ばすのではなく拒否する。
    #
    # 現行の制約下でここへ到達する入力は無い。`Span` は長さ0を許さないので
    # 空文に付くSpanは必ず範囲外になり、上の `span.end > len(text)` で
    # `SPAN_INVALID` として落ちる。Spanが1件も無ければ冒頭で `None` を返す。
    # それでも残すのは、上の検査順序を将来入れ替えたときに**ここが最後の
    # 砦になる**ためである。到達しないことを確かめたのは
    # `test_empty_text_with_no_spans_is_accepted` ほか。
    if not text:
        return MaskingRejection(RejectionReason.RATIO_EXCEEDED, "empty text does not admit spans")
    added = llm_added_length(merged, candidates)
    ratio = added / len(text)
    if ratio > constraints.max_mask_ratio:
        return MaskingRejection(
            RejectionReason.RATIO_EXCEEDED,
            f"LLM-added mask ratio {ratio:.4f} exceeds {constraints.max_mask_ratio}",
        )
    covered: set[int] = set()
    for span in candidates:
        covered.update(range(span.start, span.end))
    for span in merged:
        span_added = sum(1 for index in range(span.start, span.end) if index not in covered)
        if span_added / len(text) > constraints.max_single_llm_addition_ratio:
            return MaskingRejection(
                RejectionReason.RATIO_EXCEEDED,
                f"single LLM addition ratio {span_added / len(text):.4f} exceeds "
                f"{constraints.max_single_llm_addition_ratio}",
            )
    return None


# ---------------------------------------------------------------------------
# 決定論Rewriter
# ---------------------------------------------------------------------------


def apply_mask_tokens(text: str, spans: tuple[Span, ...], mask_tokens: dict[str, str]) -> str:
    """Spanを Mask Token へ置換する。

    **LLMが返した文字列は一切使わない。** Tokenは`masking-policy.yaml`が
    カテゴリごとに固定した値だけである。

    置換は`start`降順で適用してindex shiftを排除する。昇順で適用すると
    2件目以降の座標が1件目の置換長でずれ、結果が順序依存になる。
    """
    for span in spans:
        if span.category not in mask_tokens:
            raise KeyError(f"no mask token registered for category {span.category!r}")

    result = text
    for span in sorted(spans, key=lambda item: item.start, reverse=True):
        result = result[: span.start] + mask_tokens[span.category] + result[span.end :]
    return result
