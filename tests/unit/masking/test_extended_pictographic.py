"""`Extended_Pictographic` 判定表（ADR-007 §3、レビュー MAJOR）。

## 範囲近似をやめた

初版は `U+2190–U+2BFF` と `U+1F000–U+1FAFF` の範囲で近似していた。
実測すると矢印・囲み数字を丸ごと含み、次が拒否されていた。

    ①山田太郎②佐藤花子  -> MASKING_GRAPHEME_SPLIT
    担当山田太郎→承認    -> MASKING_GRAPHEME_SPLIT

箇条書きの丸数字は日本語の業務文書で頻出する。**Grapheme Clusterを割らない
ための保守判定が、Clusterと無関係な文字まで巻き込んでいた。**

Unicodeの `emoji-data.txt`（UCD 14.0）から生成した表を引く形へ変えた。
判定の根拠が「たぶんこの辺」から典拠のあるデータになる。

## 過剰Rejectが消えたわけではない

`★` U+2605 や `☀` U+2600 は Unicode が実際に `Extended_Pictographic` と
定めている。これらが拒否されるのは**正しい判定**であり、近似の粗さとは別である。
残る過剰Rejectは「Unicodeがそう定義している」という説明ができる。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

import pytest

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.masking import Span, contains_risky_codepoint
from harness.infrastructure.masking.pictographic import ExtendedPictographicTable
from harness.infrastructure.masking.policy import MaskingPolicy

_SUPPORT = Path(__file__).resolve().parents[3] / "support"
sys.path.insert(0, str(_SUPPORT))
from case_probe import observe_unit_case  # noqa: E402
from masking_probe import masking_evidence_payload  # noqa: E402

from harness.infrastructure.masking.mock_masker import (  # noqa: E402
    RawSpanMasker,
)
from harness.infrastructure.masking.pipeline import MaskingPipeline  # noqa: E402


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

REPO_ROOT = Path(__file__).resolve().parents[3]
BITMAP_BYTES = 0x110000 // 8
RISKY = frozenset({"EXTENDED_PICTOGRAPHIC", "COMBINING_MARK", "ZERO_WIDTH_JOINER"})


@pytest.fixture(scope="module")
def policy() -> MaskingPolicy:
    return MaskingPolicy.load(REPO_ROOT)


@pytest.fixture(scope="module")
def table(policy: MaskingPolicy) -> ExtendedPictographicTable:
    return ExtendedPictographicTable(
        REPO_ROOT / policy.grapheme.pictographic_artifact_path,
        expected_sha256=policy.grapheme.pictographic_artifact_sha256,
    )


# ---------------------------------------------------------------------------
# 表の内容
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "char"),
    [
        ("emoji_grinning", "\U0001f600"),
        ("emoji_watch", "⌚"),
        ("emoji_sun", "☀"),
        ("emoji_star", "★"),
        ("emoji_man", "\U0001f468"),
    ],
)
def test_real_pictographs_are_detected(
    table: ExtendedPictographicTable, label: str, char: str
) -> None:
    assert table(ord(char)) is True, f"{label} を Extended_Pictographic と判定できていない"


@pytest.mark.parametrize(
    ("label", "char"),
    [
        ("circled_digit_one", "①"),
        ("rightwards_arrow", "→"),
        ("leftwards_arrow", "←"),
        ("reference_mark", "※"),
        ("latin_a", "A"),
        ("kanji", "山"),
        ("hiragana", "あ"),
    ],
)
def test_non_pictographs_are_not_detected(
    table: ExtendedPictographicTable, label: str, char: str
) -> None:
    """**囲み数字と矢印はここに入らない。** 旧近似が誤って含めていた。"""
    assert table(ord(char)) is False, f"{label} を誤って Extended_Pictographic と判定している"


def test_codepoints_outside_the_unicode_space_are_false(
    table: ExtendedPictographicTable,
) -> None:
    assert table(0x110000) is False
    assert table(-1) is False


# ---------------------------------------------------------------------------
# 業務文書が通ること
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "text", "span"),
    [
        ("numbered_list", "①山田太郎②佐藤花子", Span(1, 5, "PERSON_NAME")),
        ("arrow_after", "担当山田太郎→承認", Span(2, 6, "PERSON_NAME")),
        ("reference_mark_after", "担当山田太郎※注記", Span(2, 6, "PERSON_NAME")),
        ("plain", "担当山田太郎の承認", Span(2, 6, "PERSON_NAME")),
    ],
)
def test_ordinary_business_text_is_not_flagged(
    table: ExtendedPictographicTable, label: str, text: str, span: Span
) -> None:
    """レビューで拒否されていた入力が通ること。"""
    assert contains_risky_codepoint(text, span, RISKY, table) is None, label


@pytest.mark.parametrize(
    ("label", "text", "span"),
    [
        ("emoji_after", "担当山田太郎\U0001f600です", Span(2, 6, "PERSON_NAME")),
        ("emoji_inside", "x\U0001f600y", Span(0, 2, "PERSON_NAME")),
    ],
)
@pytest.mark.case("AT-MASKING-001/GRAPHEME_SPLIT")
@pytest.mark.unit_subject("MASKING_RESULT", durability="NOT_APPLICABLE")
def test_real_emoji_still_blocks_the_span(
    table: ExtendedPictographicTable,
    label: str,
    text: str,
    span: Span,
    case_observation: Any,
) -> None:
    """緩めすぎていないこと。本物の絵文字は引き続き拒否する。"""
    assert contains_risky_codepoint(text, span, RISKY, table) == "EXTENDED_PICTOGRAPHIC", label

    _observe_via_pipeline(
        case_observation,
        "AT-MASKING-001/GRAPHEME_SPLIT",
        RawSpanMasker(spans=[(3, 4, "PERSON_NAME")]),
        "担当は\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466さんです",
        "EMOJI_ZWJ_SEQUENCE_SPLIT",
    )


# ---------------------------------------------------------------------------
# Artifactの素性
# ---------------------------------------------------------------------------


def test_missing_artifact_is_rejected(tmp_path: Path, policy: MaskingPolicy) -> None:
    with pytest.raises(HarnessError) as error:
        ExtendedPictographicTable(
            tmp_path / "absent",
            expected_sha256=policy.grapheme.pictographic_artifact_sha256,
        )
    assert error.value.code is ErrorCode.MASKING_NORMALIZATION_ARTIFACT_MISSING


def test_wrong_size_artifact_is_rejected(tmp_path: Path, policy: MaskingPolicy) -> None:
    broken = tmp_path / "short"
    broken.write_bytes(b"\x00" * (BITMAP_BYTES - 1))
    with pytest.raises(HarnessError, match="size"):
        ExtendedPictographicTable(
            broken, expected_sha256=policy.grapheme.pictographic_artifact_sha256
        )


def test_tampered_artifact_is_rejected(tmp_path: Path, policy: MaskingPolicy) -> None:
    """1bitの反転で判定が逆になる。Sizeだけでは捕まらない。"""
    original = (REPO_ROOT / policy.grapheme.pictographic_artifact_path).read_bytes()
    data = bytearray(original)
    data[0x2460 // 8] |= 1 << (0x2460 % 8)  # 丸数字を誤ってPictographicにする改竄
    tampered = tmp_path / "tampered"
    tampered.write_bytes(bytes(data))

    with pytest.raises(HarnessError, match="hash"):
        ExtendedPictographicTable(
            tampered, expected_sha256=policy.grapheme.pictographic_artifact_sha256
        )


def test_registry_pins_both_source_and_artifact(policy: MaskingPolicy) -> None:
    """入力と生成物の双方をHashで固定する（UCD Bitmapと同じ方針）。"""
    assert policy.grapheme.pictographic_source_sha256.startswith("sha256:")
    assert policy.grapheme.pictographic_artifact_sha256.startswith("sha256:")
    assert "unicode.org" in policy.grapheme.pictographic_source_url

    actual = (REPO_ROOT / policy.grapheme.pictographic_artifact_path).read_bytes()
    assert len(actual) == BITMAP_BYTES
    assert (
        "sha256:" + hashlib.sha256(actual).hexdigest()
        == policy.grapheme.pictographic_artifact_sha256
    )


# ---------------------------------------------------------------------------
# 未注入は通さない
# ---------------------------------------------------------------------------


def test_lookup_is_a_required_argument() -> None:
    """判定表はModule Globalに置かない。**引数で渡す。**

    一度Globalへ置いたが、注入した別の試験が先に走ったかどうかで結果が
    変わる順序依存を生んだ。ローカルでは通り、CIで11件落ちた。
    引数にすれば渡し忘れは型検査と実行時TypeErrorで止まり、
    「たまたま動いていた」状態が作れない。
    """
    import inspect

    from harness.domain import masking

    assert not hasattr(masking, "set_pictographic_lookup")
    parameters = inspect.signature(contains_risky_codepoint).parameters
    assert "pictographic" in parameters
    assert parameters["pictographic"].default is inspect.Parameter.empty
