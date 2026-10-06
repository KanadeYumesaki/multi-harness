"""Grapheme危険クラス10種の判定（ADR-007 §3、`grapheme_risky_classes`）。

## なぜ必要か

Registryは危険クラスを10種宣言し、実装も10種を返す。だが試験が触れて
いたのは `COMBINING_MARK` と `EXTENDED_PICTOGRAPHIC` の**2種だけ**だった。
残り8種の分岐は一度も実行されていない。

範囲定数を1桁書き間違えても、クラス名をTypoしても、症状は同じ
「何も起きない」である。判定は

    _risky_class(...) in risky_classes

の形なので、名前が食い違えばその文字は「危険ではない」ものとして通る。
**Guardが緩む方向へ黙って倒れる。** 拒否が消えたことは、拒否が起きない
という形でしか現れないので、正常系の試験では永久に気付けない。

## 何を固定するか

1. 10種それぞれが、実際の符号位置で検出されること
2. 実装が返しうる名前の集合と Registry の宣言が一致すること
3. 食い違えばPolicy読込みで止まること（緩む方向へ倒れないこと）
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.masking import (
    RISKY_CLASS_NAMES,
    Span,
    contains_risky_codepoint,
)
from harness.infrastructure.masking.policy import MaskingPolicy

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def policy() -> MaskingPolicy:
    return MaskingPolicy.load(REPO_ROOT)


# 各クラスの代表的な符号位置。実在する文字を使う。
# 「この範囲のはず」ではなく「この文字が」で固定する。
REPRESENTATIVE: dict[str, str] = {
    "COMBINING_MARK": "́",  # COMBINING ACUTE ACCENT (Mn)
    "SPACING_MARK": "ः",  # DEVANAGARI SIGN VISARGA (Mc)
    "ZERO_WIDTH_JOINER": "‍",
    "VARIATION_SELECTOR": "️",  # VARIATION SELECTOR-16
    "HANGUL_JAMO": "ᄀ",  # HANGUL CHOSEONG KIYEOK
    "EMOJI_MODIFIER": "\U0001f3fb",  # EMOJI MODIFIER FITZPATRICK TYPE-1-2
    "REGIONAL_INDICATOR": "\U0001f1ef",  # REGIONAL INDICATOR SYMBOL LETTER J
    "TAG_CHARACTER": "\U000e0067",  # TAG LATIN SMALL LETTER G
    "EXTENDED_PICTOGRAPHIC": "\U0001f600",  # GRINNING FACE
    "FORMAT_JOINER": "‌",  # ZERO WIDTH NON-JOINER (Cf)
}


# ---------------------------------------------------------------------------
# 1. 10種それぞれが検出されること
# ---------------------------------------------------------------------------


def test_every_declared_class_has_a_representative_character() -> None:
    """代表文字の取りこぼしを防ぐ。

    クラスを増やしたときにここへ足し忘れると、そのクラスは
    parametrize から漏れて**試験が減ったことに気付けない**。
    """
    assert set(REPRESENTATIVE) == RISKY_CLASS_NAMES


@pytest.mark.parametrize("expected_class", sorted(REPRESENTATIVE))
def test_representative_character_is_detected_as_its_class(
    policy: MaskingPolicy, expected_class: str
) -> None:
    """Span内に危険文字があれば、そのクラス名で拒否する。"""
    character = REPRESENTATIVE[expected_class]
    text = f"AB{character}CD"
    constraints = policy.span_constraints

    found = contains_risky_codepoint(
        text,
        Span(0, 4, "PERSON_NAME"),
        constraints.risky_classes,
        constraints.pictographic,
    )
    assert found == expected_class


@pytest.mark.parametrize("expected_class", sorted(REPRESENTATIVE))
def test_risky_character_just_after_the_span_is_also_detected(
    policy: MaskingPolicy, expected_class: str
) -> None:
    """**Spanの直後**も見る。結合文字の直前で切るとClusterを割る。

    Span内だけを見る実装だと、`山田太郎` の直後にVariation Selectorが
    続く入力で、名前だけを置換して選択子を残すことになる。
    """
    character = REPRESENTATIVE[expected_class]
    text = f"AB{character}"
    constraints = policy.span_constraints

    found = contains_risky_codepoint(
        text,
        Span(0, 2, "PERSON_NAME"),  # 危険文字はSpanの外側（直後）
        constraints.risky_classes,
        constraints.pictographic,
    )
    assert found == expected_class


def test_specific_class_wins_over_the_general_category(policy: MaskingPolicy) -> None:
    """回帰対象（2026-08-15検出・修正済み）: 一般カテゴリが具体クラスを覆っていた。

    Variation Selector は**全て** Unicode General Category が `Mn` である。
    `Mn` の判定を先頭に置いていたため `VARIATION_SELECTOR` の分岐へは
    一度も到達せず、Registryが宣言しているクラスを実装が返せなかった。

    拒否そのものは `COMBINING_MARK` として起きていたので挙動は安全側に
    留まっていた。だからこそ、正常系でもFail-Closed系でも気付けない。
    分岐ごとに試験を書いて初めて出てきた。
    """
    constraints = policy.span_constraints
    for character, expected in (
        ("️", "VARIATION_SELECTOR"),  # Mn でもある
        ("\U000e0100", "VARIATION_SELECTOR"),  # 補助面。こちらも Mn
        ("‍", "ZERO_WIDTH_JOINER"),  # Cf でもある
        ("\U000e0067", "TAG_CHARACTER"),  # Cf でもある
    ):
        assert unicodedata.category(character) in {"Mn", "Cf"}, "前提が崩れている"
        found = contains_risky_codepoint(
            f"AB{character}",
            Span(0, 3, "PERSON_NAME"),
            constraints.risky_classes,
            constraints.pictographic,
        )
        assert found == expected, f"{character!r} が {found} と分類されている"


def test_plain_ascii_and_japanese_are_not_risky(policy: MaskingPolicy) -> None:
    """通常の業務文書は通る。保守判定が過剰に効いていないこと。"""
    constraints = policy.span_constraints
    for text in ("Yamada Taro", "山田太郎", "①山田太郎②佐藤花子", "担当: 佐藤"):
        assert (
            contains_risky_codepoint(
                text,
                Span(0, len(text), "PERSON_NAME"),
                constraints.risky_classes,
                constraints.pictographic,
            )
            is None
        ), f"通常の入力が拒否されている: {text!r}"


def test_representative_characters_have_the_unicode_property_they_claim() -> None:
    """代表文字が本当にそのクラスに属することを、Unicodeの側から確かめる。

    ここが無いと、実装と試験が同じ思い違いを共有していても気付けない。
    `_risky_class` が使う判定と別の情報源（`unicodedata`）で裏を取る。
    """
    assert unicodedata.category(REPRESENTATIVE["COMBINING_MARK"]) == "Mn"
    assert unicodedata.category(REPRESENTATIVE["SPACING_MARK"]) == "Mc"
    assert unicodedata.category(REPRESENTATIVE["FORMAT_JOINER"]) == "Cf"
    assert ord(REPRESENTATIVE["ZERO_WIDTH_JOINER"]) == 0x200D
    assert 0xFE00 <= ord(REPRESENTATIVE["VARIATION_SELECTOR"]) <= 0xFE0F
    assert 0x1100 <= ord(REPRESENTATIVE["HANGUL_JAMO"]) <= 0x11FF
    assert 0x1F3FB <= ord(REPRESENTATIVE["EMOJI_MODIFIER"]) <= 0x1F3FF
    assert 0x1F1E6 <= ord(REPRESENTATIVE["REGIONAL_INDICATOR"]) <= 0x1F1FF
    assert 0xE0020 <= ord(REPRESENTATIVE["TAG_CHARACTER"]) <= 0xE007F


# ---------------------------------------------------------------------------
# 2. Registry と実装の一致
# ---------------------------------------------------------------------------


def test_registry_and_implementation_declare_the_same_classes(policy: MaskingPolicy) -> None:
    """不変条件#18。クラス名の一覧を2箇所で手書きしない。"""
    assert policy.span_constraints.risky_classes == RISKY_CLASS_NAMES


# ---------------------------------------------------------------------------
# 3. 食い違えば読込みで止まること
# ---------------------------------------------------------------------------


def _policy_document() -> dict[str, object]:
    import yaml

    path = REPO_ROOT / "design-source" / "registries" / "masking-policy.yaml"
    document: dict[str, object] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return document


def _load_with_risky_classes(tmp_path: Path, classes: list[str]) -> MaskingPolicy:
    """危険クラス一覧だけを差し替えたPolicyを読む。

    正本を書き換えず、Policy Fileだけを複製して差し替える。判定表Artifactは
    実物を指したままにしたいので `repo_root` は実Repositoryのままにし、
    `registries` だけを複製先へ向ける。
    """
    import yaml

    document = _policy_document()
    span = document["span_constraints"]
    assert isinstance(span, dict)
    span["grapheme_risky_classes"] = classes

    (tmp_path / "masking-policy.yaml").write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return MaskingPolicy.load(REPO_ROOT, registries=tmp_path)


def test_registry_class_the_implementation_cannot_detect_is_rejected(tmp_path: Path) -> None:
    """Registryへ足したが実装が返さないクラス。

    これを通すと「宣言はしたが検出しない」状態が正常として固定される。
    宣言だけで守れている気になるのが一番危ない。
    """
    with pytest.raises(HarnessError) as error:
        _load_with_risky_classes(tmp_path, [*sorted(RISKY_CLASS_NAMES), "MUSICAL_SYMBOL"])
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
    assert "MUSICAL_SYMBOL" in str(error.value)


def test_implementation_class_missing_from_the_registry_is_rejected(tmp_path: Path) -> None:
    """Registryから1つ落ちている場合。

    落ちたクラスは黙って検出されなくなる。例外にならないため、
    Registryを削った側は「何も壊れなかった」と受け取る。
    """
    reduced = sorted(RISKY_CLASS_NAMES - {"ZERO_WIDTH_JOINER"})
    with pytest.raises(HarnessError) as error:
        _load_with_risky_classes(tmp_path, reduced)
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
    assert "ZERO_WIDTH_JOINER" in str(error.value)
