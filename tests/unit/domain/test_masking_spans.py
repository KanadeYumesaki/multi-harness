"""ADR-007 Domain層（Span検証・Union・決定論Rewriter）の試験。

I/Oを持たない層であるため、Registry読込みもFilesystemも介さない。
制約値は各Testが明示的に組み立てる。
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from harness.domain.errors import ErrorCode
from harness.domain.masking import (
    MaskingRejection,
    RejectionReason,
    Span,
    SpanConstraints,
    apply_mask_tokens,
    contains_risky_codepoint,
    llm_added_length,
    merge_candidate_and_llm_spans,
    validate_spans,
)
from harness.infrastructure.masking.pictographic import ExtendedPictographicTable

_SUPPORT = Path(__file__).resolve().parents[3] / "support"
sys.path.insert(0, str(_SUPPORT))
from case_probe import observe_unit_case  # noqa: E402
from masking_probe import masking_evidence_payload  # noqa: E402

from harness.infrastructure.masking.mock_masker import (  # noqa: E402
    StaticPhraseMasker,
)
from harness.infrastructure.masking.pipeline import MaskingPipeline  # noqa: E402
from harness.infrastructure.masking.policy import MaskingPolicy  # noqa: E402


def _observe_via_pipeline(
    observation: Any, case_id: str, masker: Any, text: str, input_label: str
) -> None:
    """Canonical 経路（MaskingPipeline）で State / Error Code を観測する。

    unit 試験が直接触る部品は Subject の生産者ではない。Registry の
    `MASKING_RESULT` を生むのは Pipeline である。

    Pipeline は Ledger Port を持たない。観測する対象が無いので**読まない**。
    読まなかったことを Evidence へ残す（Owner Decision MASK-EVT-2）。
    偽の Probe が返す 0 を「見て0件」の根拠にしない。
    """
    repo_root = Path(__file__).resolve().parents[3]
    policy = MaskingPolicy.load(repo_root)
    report = MaskingPipeline(policy, masker, repo_root=repo_root).run(text)
    observe_unit_case(
        observation,
        case_id,
        state=str(report.result),
        subject_id=f"masking-{case_id.split('/')[1].lower().replace('_', '-')}",
        error_code=report.error_code.value if report.error_code is not None else None,
        payload=masking_evidence_payload(report, input_label=input_label),
    )


pytestmark = pytest.mark.unit

RISKY = frozenset(
    {
        "COMBINING_MARK",
        "SPACING_MARK",
        "HANGUL_JAMO",
        "VARIATION_SELECTOR",
        "ZERO_WIDTH_JOINER",
        "EMOJI_MODIFIER",
        "EXTENDED_PICTOGRAPHIC",
        "REGIONAL_INDICATOR",
        "TAG_CHARACTER",
        "FORMAT_JOINER",
    }
)

REPO_ROOT = Path(__file__).resolve().parents[3]

# 判定表は実Artifactから作る。Domain試験だがGrapheme判定はUnicodeの
# 実データに依存するため、模造の表を使うと本番と違う判定になる。
PICTOGRAPHIC = ExtendedPictographicTable(
    REPO_ROOT / "src/harness/masking/ucd/14.0.0-extended-pictographic",
    expected_sha256=("sha256:423a40891a4dfff16f2a922384ad862532836ceaec512512718720551c32ac5e"),
)

CONSTRAINTS = SpanConstraints(
    max_spans=512,
    max_mask_ratio=0.60,
    max_single_llm_addition_ratio=0.60,
    min_span_length=1,
    allow_overlap=False,
    allow_nesting=False,
    require_sorted_by_start=True,
    risky_classes=RISKY,
    pictographic=PICTOGRAPHIC,
)

CATEGORIES = frozenset({"EMAIL", "PERSON_NAME", "PHONE_NUMBER"})

TOKENS = {
    "EMAIL": "[MASKED:EMAIL]",
    "PERSON_NAME": "[MASKED:PERSON_NAME]",
    "PHONE_NUMBER": "[MASKED:PHONE]",
}


# ---------------------------------------------------------------------------
# Span 値オブジェクト
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end"),
    [(-1, 3), (5, 5), (5, 4), (0, 0)],
    ids=["negative_start", "empty", "reversed", "zero_length"],
)
def test_span_rejects_degenerate_intervals(start: int, end: int) -> None:
    """半開区間で`end <= start`は範囲を表さない。構築時に落とす。"""
    with pytest.raises(ValueError, match="invalid span"):
        Span(start=start, end=end, category="EMAIL")


def test_span_rejects_empty_category() -> None:
    with pytest.raises(ValueError, match="category"):
        Span(start=0, end=1, category="")


# ---------------------------------------------------------------------------
# Union（Containment Expansion）
# ---------------------------------------------------------------------------


def test_llm_span_containing_same_category_candidate_expands_it() -> None:
    """同一カテゴリの包含は安全方向。包含Spanへ正規化する。"""
    candidates = (Span(10, 20, "EMAIL"),)
    llm = (Span(5, 25, "EMAIL"),)
    merged = merge_candidate_and_llm_spans(candidates, llm)
    assert merged == (Span(5, 25, "EMAIL"),)


def test_identical_span_is_deduplicated() -> None:
    candidates = (Span(10, 20, "EMAIL"),)
    merged = merge_candidate_and_llm_spans(candidates, (Span(10, 20, "EMAIL"),))
    assert merged == (Span(10, 20, "EMAIL"),)


def test_llm_span_containing_two_candidates_is_conflict() -> None:
    """1つのSpanで複数候補を飲み込む提案は、どの候補への拡大か決まらない。"""
    candidates = (Span(10, 20, "EMAIL"), Span(30, 40, "EMAIL"))
    merged = merge_candidate_and_llm_spans(candidates, (Span(5, 45, "EMAIL"),))
    assert isinstance(merged, MaskingRejection)
    assert merged.reason is RejectionReason.SPAN_CONFLICT
    assert merged.error_code is ErrorCode.MASKING_SPAN_CONFLICT


def test_cross_category_overlap_is_conflict() -> None:
    """カテゴリが違えばMask Tokenも違う。どちらを充てるか決められない。"""
    candidates = (Span(10, 20, "EMAIL"),)
    merged = merge_candidate_and_llm_spans(candidates, (Span(5, 25, "PERSON_NAME"),))
    assert isinstance(merged, MaskingRejection)
    assert merged.reason is RejectionReason.SPAN_CONFLICT


def test_partial_overlap_without_containment_is_conflict() -> None:
    candidates = (Span(10, 20, "EMAIL"),)
    merged = merge_candidate_and_llm_spans(candidates, (Span(15, 25, "EMAIL"),))
    assert isinstance(merged, MaskingRejection)
    assert merged.reason is RejectionReason.SPAN_CONFLICT


def test_llm_cannot_remove_a_candidate() -> None:
    """**LLMは決定論候補を減らせない。** 無関係なSpanを返しても候補は残る。"""
    candidates = (Span(10, 20, "EMAIL"),)
    merged = merge_candidate_and_llm_spans(candidates, (Span(50, 60, "PERSON_NAME"),))
    assert not isinstance(merged, MaskingRejection)
    assert Span(10, 20, "EMAIL") in merged


def test_empty_llm_proposal_keeps_candidates_untouched() -> None:
    candidates = (Span(30, 40, "EMAIL"), Span(10, 20, "PHONE_NUMBER"))
    merged = merge_candidate_and_llm_spans(candidates, ())
    assert merged == (Span(10, 20, "PHONE_NUMBER"), Span(30, 40, "EMAIL"))


# ---------------------------------------------------------------------------
# Mask Ratio（分子はLLM追加分だけ）
# ---------------------------------------------------------------------------


def test_scan1_candidates_are_excluded_from_the_ratio_numerator() -> None:
    """決定論候補は分子に入らない（`deterministic_scan1_excluded: true`）。

    個人情報密度の高い文書を、決定論スキャナ単独の検出量で拒否しない。
    Circuit BreakerはLLMの暴走を止めるためのものである。
    """
    candidates = (Span(0, 90, "EMAIL"),)
    assert llm_added_length(candidates, candidates) == 0

    text = "x" * 100
    assert (
        validate_spans(
            text,
            candidates,
            candidates,
            known_categories=CATEGORIES,
            constraints=CONSTRAINTS,
        )
        is None
    )


def test_llm_addition_beyond_max_ratio_is_rejected() -> None:
    text = "x" * 100
    candidates: tuple[Span, ...] = ()
    merged = (Span(0, 61, "PERSON_NAME"),)
    rejection = validate_spans(
        text,
        merged,
        candidates,
        known_categories=CATEGORIES,
        constraints=CONSTRAINTS,
    )
    assert rejection is not None
    assert rejection.error_code is ErrorCode.MASKING_RATIO_EXCEEDED


@pytest.mark.case("AT-MASKING-001/MASK_RATIO_BOUNDARY")
@pytest.mark.unit_subject("MASKING_RESULT", durability="NOT_APPLICABLE")
def test_ratio_exactly_at_the_limit_is_accepted(case_observation: Any) -> None:
    """受入Case `MASK_RATIO_BOUNDARY`。ちょうど`max_mask_ratio`は通す。

    比較は `ratio > max` であり `>=` ではない。境界を試験していないと、
    どちらの意図だったのかがコードからしか読めなくなる。上限値を
    「そこまでは許す」と読むか「そこから拒否」と読むかで挙動が1件分ずれる。
    """
    text = "x" * 100
    merged = (Span(0, 60, "PERSON_NAME"),)  # 60/100 = 0.60 ちょうど
    assert (
        validate_spans(text, merged, (), known_categories=CATEGORIES, constraints=CONSTRAINTS)
        is None
    )

    _observe_via_pipeline(
        case_observation,
        "AT-MASKING-001/MASK_RATIO_BOUNDARY",
        StaticPhraseMasker(phrases={"山田太郎": "PERSON_NAME"}),
        "担当は山田太郎さんです",
        "PERSON_NAME_AT_RATIO_LIMIT",
    )


def test_ratio_one_codepoint_over_the_limit_is_rejected() -> None:
    """境界の外側。1符号位置多いだけで拒否する。"""
    text = "x" * 100
    merged = (Span(0, 61, "PERSON_NAME"),)  # 61/100 = 0.61
    rejection = validate_spans(
        text, merged, (), known_categories=CATEGORIES, constraints=CONSTRAINTS
    )
    assert rejection is not None
    assert rejection.error_code is ErrorCode.MASKING_RATIO_EXCEEDED


def test_single_addition_ratio_boundary_is_accepted() -> None:
    """`max_single_llm_addition_ratio` 側も同じ境界の扱いにする。"""
    text = "x" * 100
    merged = (Span(0, 60, "PERSON_NAME"),)
    assert (
        validate_spans(text, merged, (), known_categories=CATEGORIES, constraints=CONSTRAINTS)
        is None
    )


def test_containment_expansion_counts_only_the_added_part() -> None:
    """包含拡大では、候補の外側だけが分子になる。"""
    candidates = (Span(10, 60, "EMAIL"),)
    merged = (Span(5, 65, "EMAIL"),)
    assert llm_added_length(merged, candidates) == 10


# ---------------------------------------------------------------------------
# Span 検証
# ---------------------------------------------------------------------------


def test_span_beyond_text_length_is_invalid() -> None:
    rejection = validate_spans(
        "0123456789",
        (Span(5, 20, "EMAIL"),),
        (),
        known_categories=CATEGORIES,
        constraints=CONSTRAINTS,
    )
    assert rejection is not None
    assert rejection.error_code is ErrorCode.MASKING_SPAN_INVALID


def test_unknown_category_is_rejected() -> None:
    rejection = validate_spans(
        "0123456789",
        (Span(0, 3, "CREDIT_CARD"),),
        (),
        known_categories=CATEGORIES,
        constraints=CONSTRAINTS,
    )
    assert rejection is not None
    assert rejection.error_code is ErrorCode.MASKING_CATEGORY_UNKNOWN


def test_unsorted_spans_are_rejected() -> None:
    rejection = validate_spans(
        "0123456789",
        (Span(5, 7, "EMAIL"), Span(1, 3, "EMAIL")),
        (),
        known_categories=CATEGORIES,
        constraints=CONSTRAINTS,
    )
    assert rejection is not None
    assert rejection.error_code is ErrorCode.MASKING_SPAN_INVALID


def test_overlapping_spans_are_rejected() -> None:
    rejection = validate_spans(
        "0123456789",
        (Span(1, 5, "EMAIL"), Span(3, 8, "EMAIL")),
        (),
        known_categories=CATEGORIES,
        constraints=CONSTRAINTS,
    )
    assert rejection is not None
    assert rejection.error_code is ErrorCode.MASKING_SPAN_INVALID


def test_nested_spans_are_rejected_when_overlap_is_allowed() -> None:
    """`allow_nesting: false` が単独で効くこと。

    **`allow_overlap` が false の間、包含の検査は一度も実行されない。**
    包含は重なりの特殊形であり、重なりの検査が先に落とすためである。
    現行Policyは両方 false なので、`allow_nesting` は実質的に効いていない。

    つまり `allow_nesting: true` へ変えても包含は通らない。設定と挙動が
    一致していないが、緩む方向ではないので実害は無い。ここでは
    `allow_overlap` を true にして、包含の検査そのものが機能することを
    確かめる。両方の役割を分けて固定しておかないと、将来 `allow_overlap`
    を緩めた瞬間に包含も一緒に通り始める。

    包含を通すと、同じ範囲へMask Tokenが二重に当たり、Rewriterの
    置換結果が適用順に依存する。
    """
    constraints = replace(CONSTRAINTS, allow_overlap=True, allow_nesting=False)
    rejection = validate_spans(
        "0123456789",
        (Span(1, 8, "EMAIL"), Span(3, 5, "EMAIL")),
        (),
        known_categories=CATEGORIES,
        constraints=constraints,
    )
    assert rejection is not None
    assert rejection.error_code is ErrorCode.MASKING_SPAN_INVALID
    assert "nested" in rejection.detail


def test_overlap_check_runs_before_the_nesting_check() -> None:
    """現行Policy（両方false）では包含も「重なり」として落ちることを固定する。

    上の試験と対にして、どちらの検査が働いたのかを取り違えないようにする。
    """
    rejection = validate_spans(
        "0123456789",
        (Span(1, 8, "EMAIL"), Span(3, 5, "EMAIL")),
        (),
        known_categories=CATEGORIES,
        constraints=CONSTRAINTS,
    )
    assert rejection is not None
    assert "overlap" in rejection.detail


def test_span_shorter_than_min_length_is_rejected() -> None:
    """`min_span_length` を下回るSpan。

    長さ0のSpanは何も隠さないが、Mask Tokenだけが挿入される。
    「マスクした」という記録だけが残り、原文はそのまま通る。
    """
    constraints = replace(CONSTRAINTS, min_span_length=3)
    rejection = validate_spans(
        "0123456789",
        (Span(1, 2, "EMAIL"),),
        (),
        known_categories=CATEGORIES,
        constraints=constraints,
    )
    assert rejection is not None
    assert rejection.error_code is ErrorCode.MASKING_SPAN_INVALID
    assert "min_span_length" in rejection.detail


def test_empty_text_with_no_spans_is_accepted() -> None:
    """空文にSpanが無いなら、隠すものが無いので通す。

    Registryの `ratio_empty_text: REJECT` は「空文で比率を計算するな」
    という指示である。比率の計算まで到達しないこの経路は、その対象では
    ない。**Registryの文言と実際の分岐が1対1に対応していない**箇所なので、
    実挙動の側を固定しておく。
    """
    assert validate_spans("", (), (), known_categories=CATEGORIES, constraints=CONSTRAINTS) is None


def test_span_beyond_the_end_of_empty_text_is_rejected() -> None:
    """空文にSpanを付けた場合。比率検査より前で落ちる。

    `Span` は長さ0を許さないので、空文に付くSpanは必ず範囲外になる。
    結果として `RATIO_EXCEEDED` ではなく `SPAN_INVALID` で止まる。
    """
    rejection = validate_spans(
        "",
        (Span(0, 1, "EMAIL"),),
        (),
        known_categories=CATEGORIES,
        constraints=CONSTRAINTS,
    )
    assert rejection is not None
    assert rejection.error_code is ErrorCode.MASKING_SPAN_INVALID


def test_zero_length_span_cannot_be_constructed() -> None:
    """長さ0のSpanを型の側で作らせない。

    上の2件はこの前提の上に立っている。前提が崩れれば、
    上の試験は別のものを測り始める。
    """
    with pytest.raises(ValueError, match="invalid span"):
        Span(0, 0, "EMAIL")


def test_single_llm_addition_over_its_own_limit_is_rejected() -> None:
    """1件のLLM追加Spanだけで上限を超える場合。

    合計比率とは別の上限である（`max_single_llm_addition_ratio`）。
    合計だけを見ていると、1件で大半を覆うSpanが他に候補が無いときに通る。
    LLMが「全部個人情報です」と言えばそれが通る形になる。
    """
    constraints = replace(CONSTRAINTS, max_mask_ratio=0.90, max_single_llm_addition_ratio=0.20)
    rejection = validate_spans(
        "0123456789",
        (Span(0, 5, "EMAIL"),),  # 10文字中5文字 = 0.5 > 0.20
        (),
        known_categories=CATEGORIES,
        constraints=constraints,
    )
    assert rejection is not None
    assert rejection.error_code is ErrorCode.MASKING_RATIO_EXCEEDED
    assert "single LLM addition" in rejection.detail


def test_span_count_over_max_spans_is_rejected() -> None:
    constraints = SpanConstraints(
        max_spans=2,
        max_mask_ratio=0.60,
        max_single_llm_addition_ratio=0.60,
        min_span_length=1,
        allow_overlap=False,
        allow_nesting=False,
        require_sorted_by_start=True,
        risky_classes=RISKY,
        pictographic=PICTOGRAPHIC,
    )
    text = "x" * 100
    spans = tuple(Span(i * 10, i * 10 + 2, "EMAIL") for i in range(3))
    rejection = validate_spans(
        text, spans, spans, known_categories=CATEGORIES, constraints=constraints
    )
    assert rejection is not None
    assert rejection.error_code is ErrorCode.MASKING_SPAN_INVALID


def test_empty_text_with_spans_is_rejected() -> None:
    """`ratio_empty_text: REJECT`。分母0を0除算にも既定値にもしない。"""
    rejection = validate_spans("", (), (), known_categories=CATEGORIES, constraints=CONSTRAINTS)
    assert rejection is None  # Spanが無ければ検査対象ではない

    rejection = validate_spans(
        "abc",
        (Span(0, 3, "EMAIL"),),
        (),
        known_categories=CATEGORIES,
        constraints=CONSTRAINTS,
    )
    assert rejection is not None  # 3文字中3文字は ratio 1.0


# ---------------------------------------------------------------------------
# Grapheme Guard
# ---------------------------------------------------------------------------


def test_span_ending_before_a_combining_mark_is_rejected() -> None:
    """結合文字の直前で切るとClusterが割れる。Spanの**外**も見る。"""
    # 分解形 c + U+0301 をEscapeで書く。Source上に生の結合文字を置くと、
    # 何かがNFC正規化した瞬間に試験の意味が静かに変わる。
    text = "abc\u0301def"  # a0 b1 c2 U+0301@3 d4 e5 f6
    assert (
        contains_risky_codepoint(text, Span(0, 3, "EMAIL"), RISKY, PICTOGRAPHIC) == "COMBINING_MARK"
    )

    rejection = validate_spans(
        text, (Span(0, 3, "EMAIL"),), (), known_categories=CATEGORIES, constraints=CONSTRAINTS
    )
    assert rejection is not None
    assert rejection.error_code is ErrorCode.MASKING_GRAPHEME_SPLIT


def test_span_over_zwj_emoji_sequence_is_rejected() -> None:
    # x, MAN, ZWJ, WOMAN, x。Span(1,3)はMANとZWJだけを含み、WOMANを置き去りにする。
    text = "x\U0001f468\u200d\U0001f469x"
    rejection = validate_spans(
        text, (Span(1, 3, "EMAIL"),), (), known_categories=CATEGORIES, constraints=CONSTRAINTS
    )
    assert rejection is not None
    assert rejection.error_code is ErrorCode.MASKING_GRAPHEME_SPLIT


def test_plain_ascii_span_passes_the_grapheme_guard() -> None:
    """保守側の近似であっても、通常のASCII文字列を巻き込まない。"""
    text = "contact: alice@example.com for details"
    assert contains_risky_codepoint(text, Span(9, 26, "EMAIL"), RISKY, PICTOGRAPHIC) is None


# ---------------------------------------------------------------------------
# 決定論Rewriter
# ---------------------------------------------------------------------------


def test_rewriter_uses_registry_tokens_not_llm_text() -> None:
    text = "mail alice@example.com now"
    result = apply_mask_tokens(text, (Span(5, 22, "EMAIL"),), TOKENS)
    assert result == "mail [MASKED:EMAIL] now"


def test_rewriter_is_independent_of_span_order() -> None:
    """`start`降順で適用するため、入力順が結果を変えない。"""
    text = "A alice@example.com B 090-1234-5678 C"
    ascending = (Span(2, 19, "EMAIL"), Span(22, 35, "PHONE_NUMBER"))
    descending = tuple(reversed(ascending))
    assert apply_mask_tokens(text, ascending, TOKENS) == apply_mask_tokens(text, descending, TOKENS)


def test_rewriter_output_contains_no_original_value() -> None:
    text = "alice@example.com"
    result = apply_mask_tokens(text, (Span(0, 17, "EMAIL"),), TOKENS)
    assert "alice" not in result
    assert "example.com" not in result


def test_rewriter_refuses_category_without_a_registered_token() -> None:
    """Tokenが無いカテゴリで置換しない。空文字などで代替すると値が消える。"""
    with pytest.raises(KeyError):
        apply_mask_tokens("abcdef", (Span(0, 3, "UNREGISTERED"),), TOKENS)


def test_rewriter_on_empty_span_set_returns_input_unchanged() -> None:
    text = "nothing to mask here"
    assert apply_mask_tokens(text, (), TOKENS) == text
