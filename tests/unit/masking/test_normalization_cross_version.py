"""Python Version をまたいだ `source_normalized_hash` の一致（Gate 44）。

## CIが両Versionで走っているだけでは足りない

Gate 44 は「Python 3.11 と 3.12 で同じ `source_normalized_hash` になる」ことを
要求する。CIは両Versionで走っているが、**各Jobは片方のVersionしか見ない**。
Job同士で結果を突き合わせる仕組みが無いため、両方が緑でも
「互いに違う値を出して、それぞれ自分の値と一致していた」可能性を排除できない。

期待値をRepositoryへ固定し、どのVersionのJobも**同じ定数**と照合する。
両者が食い違えばどちらかが必ず落ちる。これが実質的なCross-Version検査になる。

Skipで回避しない。不変条件#16「実行していないTestをPASSと書かない」により、
「もう片方のPythonが無いので飛ばす」という逃げ道は使えない。

## 実測（2026-08-14）

    Python 3.11.14 (UCD 14.0.0) -> 14件一致
    Python 3.12.3  (UCD 15.0.0) -> 14件一致

Fixture生成時に実行時UCD版数を書き込んでいたため、当初は
Versionが違うと必ず不一致になっていた。Hashは全件一致していたのに
検査が「常に赤」で用をなさなかった。Versionをまたいで同じであるべき値だけを
載せる形へ直した。
"""

from __future__ import annotations

import json
import sys
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from harness.infrastructure.masking.policy import MaskingPolicy
from harness.infrastructure.masking.ucd_guard import Ucd14Guard

_SUPPORT = Path(__file__).resolve().parents[3] / "support"
sys.path.insert(0, str(_SUPPORT))
from case_probe import observe_unit_case  # noqa: E402
from masking_probe import masking_evidence_payload  # noqa: E402

from harness.infrastructure.masking.mock_masker import (  # noqa: E402
    StaticPhraseMasker,
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
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "normalization" / "source_normalized_hash.json"


@pytest.fixture(scope="module")
def guard() -> Ucd14Guard:
    policy = MaskingPolicy.load(REPO_ROOT)
    return Ucd14Guard(
        REPO_ROOT / policy.normalization.artifact_path,
        expected_artifact_sha256=policy.normalization.artifact_sha256,
        profile_id=policy.normalization.profile_id,
        minimum_unicodedata_version=policy.normalization.runtime_minimum_version,
    )


@pytest.fixture(scope="module")
def pinned() -> dict[str, object]:
    data: dict[str, object] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def _text_of(case: dict[str, object]) -> str:
    """符号位置の列から本文を組み立てる。

    Fixtureへ生の文字を書かない。何かがFileをNFC正規化した瞬間に
    期待値の意味が変わってしまうためである。
    """
    codepoints = case["text_codepoints"]
    assert isinstance(codepoints, list)
    return "".join(chr(int(str(point)[2:], 16)) for point in codepoints)


def test_fixture_covers_normalization_sensitive_inputs(pinned: dict[str, object]) -> None:
    """ASCIIだけでは、Guardが働いているかどうかが分からない。"""
    cases = pinned["cases"]
    assert isinstance(cases, list)
    names = {str(case["name"]) for case in cases}
    for required in (
        "composed_e_acute",
        "decomposed_e_acute",
        "hangul_jamo",
        "hangul_syllable",
        "combining_order_a",
        "combining_order_b",
    ):
        assert required in names, f"正規化で差が出る入力 {required} がFixtureに無い"


@pytest.mark.case("AT-MASKING-001/UNICODE_PROFILE_CROSS_PYTHON")
@pytest.mark.unit_subject("MASKING_RESULT", durability="NOT_APPLICABLE")
def test_every_pinned_hash_matches_this_interpreter(
    guard: Ucd14Guard,
    pinned: dict[str, object],
    case_observation: Any,
) -> None:
    """**この試験がCross-Version検査の本体である。**

    3.11のJobも3.12のJobも同じ定数と照合する。片方でも違う値を出せば落ちる。
    """
    cases = pinned["cases"]
    assert isinstance(cases, list)
    mismatches: list[str] = []
    for case in cases:
        computed = guard.normalize(_text_of(case)).source_normalized_hash
        if computed != case["source_normalized_hash"]:
            mismatches.append(
                f"{case['name']}: pinned={case['source_normalized_hash']} computed={computed}"
            )
    assert not mismatches, (
        f"UCD {unicodedata.unidata_version} の実行系がFixtureと違うHashを出した。\n"
        "Python Version間で source_normalized_hash が食い違っている。\n" + "\n".join(mismatches)
    )

    _observe_via_pipeline(
        case_observation,
        "AT-MASKING-001/UNICODE_PROFILE_CROSS_PYTHON",
        StaticPhraseMasker(phrases={}),
        "\u00e9mile@example.com へ連絡",
        "NFC_COMPOSED_EMAIL",
    )


def test_profile_binding_matches_the_registry(pinned: dict[str, object]) -> None:
    """FixtureがどのProfile／Artifactに対する期待値かを固定する。"""
    policy = MaskingPolicy.load(REPO_ROOT)
    assert pinned["profile_id"] == policy.normalization.profile_id
    assert pinned["profile_artifact_sha256"] == policy.normalization.artifact_sha256


def test_fixture_does_not_pin_the_runtime_unicode_version(pinned: dict[str, object]) -> None:
    """実行時のUCD版数をFixtureへ入れない。

    入れると3.11と3.12で必ず不一致になり、検査が常に赤になって
    用をなさなくなる。実際に一度そうなった。
    """
    assert "generated_with_unidata_version" not in pinned


def test_nfc_equivalent_inputs_share_one_hash(guard: Ucd14Guard) -> None:
    """合成形と分解形が同じHashになる。NFCが効いていることの直接確認。"""
    composed = guard.normalize("émile").source_normalized_hash
    decomposed = guard.normalize("émile").source_normalized_hash
    assert composed == decomposed


def test_hangul_jamo_composes_to_the_same_hash(guard: Ucd14Guard) -> None:
    """ハングルはNFCでSyllableへ合成される。"""
    jamo = guard.normalize("각").source_normalized_hash
    syllable = guard.normalize("각").source_normalized_hash
    assert jamo == syllable
