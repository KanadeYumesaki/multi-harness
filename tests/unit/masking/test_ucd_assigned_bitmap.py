"""UCD 14.0割当済み符号位置Bitmap Artifactの試験（ADR-007）。

このArtifactは`source_normalized_hash`の前提であり、ADR-006 §4.3 項目16経由で
委任の照合結果まで決める。Artifactが壊れると静かにHashがずれるため、
形式・内容・由来・再生成の決定性を機械検証する。
"""

from __future__ import annotations

import hashlib
import json
import sys
import unicodedata
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
ARTIFACT = REPO_ROOT / "src" / "harness" / "masking" / "ucd" / "14.0.0-assigned-codepoints"
PROVENANCE = Path(str(ARTIFACT) + ".provenance.json")

UNICODE_SPACE = 0x110000
EXPECTED_SIZE = UNICODE_SPACE // 8  # 139,264


@pytest.fixture(scope="module")
def bitmap() -> bytes:
    return ARTIFACT.read_bytes()


@pytest.fixture(scope="module")
def provenance() -> dict[str, object]:
    data: dict[str, object] = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    return data


def _assigned(bitmap: bytes, codepoint: int) -> bool:
    """`UCD14_ASSIGNED_BITMAP_V1`: 符号位置昇順・LSB-first。"""
    return bool((bitmap[codepoint // 8] >> (codepoint % 8)) & 1)


# --------------------------------------------------------------------------
# 形式
# --------------------------------------------------------------------------


def test_artifact_size_is_exactly_one_bit_per_codepoint(bitmap: bytes) -> None:
    """0x110000 bit = 139,264 bytes。端数なし。"""
    assert UNICODE_SPACE % 8 == 0
    assert len(bitmap) == EXPECTED_SIZE


def test_artifact_hash_matches_provenance(bitmap: bytes, provenance: dict[str, object]) -> None:
    actual = "sha256:" + hashlib.sha256(bitmap).hexdigest()
    assert provenance["artifact_sha256"] == actual


def test_provenance_records_official_source(provenance: dict[str, object]) -> None:
    """由来が監査可能であること。公式UCDのURLと入力Hashを持つ。"""
    assert provenance["unicode_data_version"] == "14.0.0"
    assert provenance["artifact_format"] == "UCD14_ASSIGNED_BITMAP_V1"
    assert provenance["artifact_bit_order"] == "CODEPOINT_ASCENDING_LSB0"
    assert provenance["artifact_size_bytes"] == EXPECTED_SIZE
    source_url = provenance["source_url"]
    assert isinstance(source_url, str)
    assert source_url.startswith("https://www.unicode.org/Public/14.0.0/")
    source_hash = provenance["source_sha256"]
    assert isinstance(source_hash, str) and source_hash.startswith("sha256:")


# --------------------------------------------------------------------------
# 内容
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("codepoint", "expected", "note"),
    [
        (0x0041, True, "ASCII 'A'"),
        (0x00E9, True, "U+00E9 合成済み é"),
        (0x0301, True, "U+0301 結合アキュート"),
        (0x3042, True, "U+3042 あ"),
        (0xAC00, True, "U+AC00 Hangul 音節"),
        (0x1F600, True, "U+1F600 絵文字（Unicode 6.1）"),
        (0xE000, True, "私用領域は割当済み。NFCは恒等で安定"),
        (0x0870, True, "Arabic Extended-B。Unicode 14.0で追加"),
        (0xD800, False, "Surrogateは除外。Unicode scalar valueではない"),
        (0xDFFF, False, "Surrogate下端"),
        (0xFDD0, False, "Noncharacter"),
        (0x0378, False, "未割当"),
    ],
)
def test_known_codepoints(bitmap: bytes, codepoint: int, expected: bool, note: str) -> None:
    assert _assigned(bitmap, codepoint) is expected, note


@pytest.mark.parametrize(
    ("codepoint", "name"),
    [
        (0x11F00, "Kawi"),
        (0x1E030, "Cyrillic Extended-D"),
        (0x1E4D0, "Nag Mundari"),
        (0x1F6DC, "wireless 絵文字"),
    ],
)
def test_unicode_15_additions_are_not_assigned_in_ucd14(
    bitmap: bytes, codepoint: int, name: str
) -> None:
    """本Guardが塞ぐ差分そのもの。

    Unicode 15.0で追加された符号位置は14.0 Bitmapで未割当でなければならない。
    同時に、実行中のruntime（UCD 15.0）ではこれらが割当済みとして見えることも
    確認する。両者が食い違う範囲こそ、Python 3.11／3.12でNFC結果が
    分岐し得る領域である。
    """
    assert _assigned(bitmap, codepoint) is False, f"{name} must be unassigned in UCD 14.0"
    if unicodedata.unidata_version >= "15.0.0":
        assert unicodedata.category(chr(codepoint)) != "Cn", (
            f"{name} should be assigned in runtime UCD {unicodedata.unidata_version}"
        )


def test_surrogate_range_is_entirely_unassigned(bitmap: bytes) -> None:
    assert not any(_assigned(bitmap, cp) for cp in range(0xD800, 0xE000))


def test_assigned_count_matches_provenance(bitmap: bytes, provenance: dict[str, object]) -> None:
    total = sum(bin(byte).count("1") for byte in bitmap)
    assert total == provenance["assigned_codepoint_count"]


# --------------------------------------------------------------------------
# 再生成の決定性
# --------------------------------------------------------------------------


def _generator() -> object:
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    try:
        import build_ucd_assigned_bitmap  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    return build_ucd_assigned_bitmap


# UnicodeData.txt の要点を再現した合成入力。
# 通常行・First/Lastによる範囲・Surrogate範囲・私用領域を含む。
_SYNTHETIC_UCD = (
    "0041;LATIN CAPITAL LETTER A;Lu;0;L;;;;;N;;;;0061;\n"
    "00E9;LATIN SMALL LETTER E WITH ACUTE;Ll;0;L;0065 0301;;;;N;;;00C9;;00C9\n"
    "3400;<CJK Ideograph Extension A, First>;Lo;0;L;;;;;N;;;;;\n"
    "3402;<CJK Ideograph Extension A, Last>;Lo;0;L;;;;;N;;;;;\n"
    "D800;<Non Private Use High Surrogate, First>;Cs;0;L;;;;;N;;;;;\n"
    "D801;<Non Private Use High Surrogate, Last>;Cs;0;L;;;;;N;;;;;\n"
    "E000;<Private Use, First>;Co;0;L;;;;;N;;;;;\n"
    "E001;<Private Use, Last>;Co;0;L;;;;;N;;;;;\n"
)


def test_parser_expands_ranges_and_drops_surrogates() -> None:
    """First/Last範囲を展開し、Surrogateだけを除外する。"""
    generator = _generator()
    assigned = generator.parse_assigned(_SYNTHETIC_UCD)  # type: ignore[attr-defined]
    assert assigned == {0x0041, 0x00E9, 0x3400, 0x3401, 0x3402, 0xE000, 0xE001}


def test_parser_rejects_unterminated_range() -> None:
    """First に対応する Last が無い入力を黙って受け入れない。"""
    generator = _generator()
    broken = "3400;<CJK Ideograph Extension A, First>;Lo;0;L;;;;;N;;;;;\n"
    with pytest.raises(ValueError, match="unterminated"):
        generator.parse_assigned(broken)  # type: ignore[attr-defined]


def test_bitmap_construction_is_deterministic_and_lsb_first() -> None:
    """同一集合から常に同一Bytes。ビット位置が仕様どおりであることも確認する。"""
    generator = _generator()
    assigned = generator.parse_assigned(_SYNTHETIC_UCD)  # type: ignore[attr-defined]
    first = generator.build_bitmap(assigned)  # type: ignore[attr-defined]
    second = generator.build_bitmap(assigned)  # type: ignore[attr-defined]
    assert first == second
    assert len(first) == EXPECTED_SIZE
    for codepoint in assigned:
        assert _assigned(first, codepoint)
    # U+0041 は byte 8 の bit 1（0x41 = 65 -> 65//8=8, 65%8=1）
    assert first[8] & (1 << 1)


# --------------------------------------------------------------------------
# 信頼根の固定（Registryへ期待Hashを固定し、不一致でFail-Closed）
# --------------------------------------------------------------------------


def _masking_policy() -> dict[str, object]:
    import yaml

    path = REPO_ROOT / "design-source" / "registries" / "masking-policy.yaml"
    document: dict[str, object] = yaml.safe_load(path.read_text(encoding="utf-8"))
    normalization: dict[str, object] = document["normalization"]  # type: ignore[assignment]
    return normalization


def test_registry_pins_both_artifact_and_source_hashes() -> None:
    """`engine_artifact_hash_required: true`だけでは何とも突合できない。

    Artifactと、その生成元である公式UCD入力の**双方**の期待Hashが
    Registryへ固定されていること。
    """
    normalization = _masking_policy()
    assert normalization["engine_artifact_hash_required"] is True
    for key in ("engine_artifact_sha256", "ucd_source_sha256"):
        value = normalization.get(key)
        assert isinstance(value, str), f"{key} が未固定"
        assert value.startswith("sha256:") and len(value) == 71, key
    assert normalization["ucd_source_hash_mismatch"] == "REJECT"
    assert normalization["ucd_source_malformed_line"] == "REJECT"


def test_pinned_hashes_match_the_actual_artifact_and_provenance(
    bitmap: bytes, provenance: dict[str, object]
) -> None:
    """Registryの固定値が実物と一致する。片方だけ更新した状態を検出する。"""
    normalization = _masking_policy()
    assert normalization["engine_artifact_sha256"] == "sha256:" + hashlib.sha256(bitmap).hexdigest()
    assert normalization["engine_artifact_sha256"] == provenance["artifact_sha256"]
    assert normalization["ucd_source_sha256"] == provenance["source_sha256"]
    assert normalization["ucd_source_url"] == provenance["source_url"]


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ("0041;LATIN CAPITAL LETTER A;Lu;0;L\n", "field数不足"),
        ("0041;A;Lu;0;L;;;;;N;;;;;;extra\n", "field数超過"),
        ("ZZZZ;X;Lu;0;L;;;;;N;;;;;\n", "符号位置が16進でない"),
    ],
)
def test_malformed_ucd_line_is_rejected_not_skipped(line: str, reason: str) -> None:
    """不正行を読み飛ばすと割当済み集合が静かに欠け、Guardが通してしまう。"""
    generator = _generator()
    with pytest.raises(ValueError, match="malformed|hexadecimal"):
        generator.parse_assigned(line)  # type: ignore[attr-defined]


def test_unterminated_range_is_still_rejected() -> None:
    generator = _generator()
    with pytest.raises(ValueError, match="unterminated"):
        generator.parse_assigned(  # type: ignore[attr-defined]
            "3400;<CJK Ideograph Extension A, First>;Lo;0;L;;;;;N;;;;;\n"
        )


def test_checked_in_artifact_is_consistent_with_its_own_hash(bitmap: bytes) -> None:
    """チェックイン済みArtifactが改変されていないこと。

    再生成による一致確認は公式UCDの取得を要するため、CIでは
    `tools/build_ucd_assigned_bitmap.py --check` を別Stepとして実行する。
    ここではArtifactとprovenanceの自己整合だけを検証する。
    """
    assert hashlib.sha256(bitmap).hexdigest() == json.loads(PROVENANCE.read_text(encoding="utf-8"))[
        "artifact_sha256"
    ].removeprefix("sha256:")
