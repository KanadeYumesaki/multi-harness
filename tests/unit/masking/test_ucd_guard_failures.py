"""UCD Guard の異常系（受入Case `UNICODE_PROFILE_ARTIFACT_MISSING` ほか）。

Guardは `source_normalized_hash` の前提であり、ADR-006 §4.3 経由で
委任の照合結果まで決める。Artifactが壊れていれば静かにHashがずれるため、
**壊れているときに止まること**を確かめる。

これらの経路はカバレッジ上90%のまま未到達だった。正常系だけを試験すると
Fail-Closedの分岐は永久に実行されず、壊れていても気付けない。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from case_probe import observe_unit_case

from harness.domain.errors import ErrorCode, HarnessError
from harness.infrastructure.masking.mock_masker import StaticPhraseMasker
from harness.infrastructure.masking.policy import MaskingPolicy
from harness.infrastructure.masking.ucd_guard import Ucd14Guard

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
BITMAP_BYTES = 0x110000 // 8


@pytest.fixture(scope="module")
def policy() -> MaskingPolicy:
    return MaskingPolicy.load(REPO_ROOT)


@pytest.fixture(scope="module")
def artifact_path(policy: MaskingPolicy) -> Path:
    return REPO_ROOT / policy.normalization.artifact_path


def build(path: Path, policy: MaskingPolicy, *, expected_hash: str | None = None) -> Ucd14Guard:
    return Ucd14Guard(
        path,
        expected_artifact_sha256=expected_hash or policy.normalization.artifact_sha256,
        profile_id=policy.normalization.profile_id,
        minimum_unicodedata_version=policy.normalization.runtime_minimum_version,
    )


# ---------------------------------------------------------------------------
# Artifact の異常
# ---------------------------------------------------------------------------


class _InvocationCounter:
    """呼ばれた回数を**数える**。0で初期化するのではなく0を数える。

    Probe を組まずに `normalization_invocation_count == 0` と書けば、
    それは未測定を0件と偽ることになる。ここを通した回数だけが実測値である。
    """

    def __init__(self, target: Any) -> None:
        self._target = target
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return self._target(*args, **kwargs)


@pytest.mark.case("AT-MASKING-001/UNICODE_PROFILE_ARTIFACT_MISSING")
@pytest.mark.unit_subject("MASKING_RESULT", durability="NOT_APPLICABLE")
def test_missing_artifact_is_rejected(
    tmp_path: Path,
    policy: MaskingPolicy,
    monkeypatch: pytest.MonkeyPatch,
    case_observation: Any,
) -> None:
    """`engine_artifact_missing: REJECT`。無ければ正規化を始めない。

    「始めない」ことを実測する。Guard の構築が落ちた後に、正規化と Masker が
    **一度も呼ばれていない**ことを Counter で確かめる。
    """
    normalize_counter = _InvocationCounter(Ucd14Guard.normalize)
    monkeypatch.setattr(Ucd14Guard, "normalize", normalize_counter)
    masker_counter = _InvocationCounter(StaticPhraseMasker.propose_spans)
    monkeypatch.setattr(StaticPhraseMasker, "propose_spans", masker_counter)

    absent = tmp_path / "absent"
    assert not absent.exists()

    with pytest.raises(HarnessError) as error:
        build(absent, policy)
    assert error.value.code is ErrorCode.MASKING_NORMALIZATION_ARTIFACT_MISSING

    # Guard が落ちた後、下流は一度も動いていない。
    assert normalize_counter.calls == 0
    assert masker_counter.calls == 0

    # Guard は構築時に落ちるので MaskingReport は生まれない。主張できるのは
    # 「Artifact が読めないので Reject した」ことだけで、Ledger も触らない。
    observe_unit_case(
        case_observation,
        "AT-MASKING-001/UNICODE_PROFILE_ARTIFACT_MISSING",
        state="REJECTED",
        subject_id=f"masking-normalization:{policy.normalization.profile_id}",
        error_code=error.value.code.value,
        payload={
            "profile_id": policy.normalization.profile_id,
            "artifact_present": absent.exists(),
            "artifact_format_valid": False,
            "normalization_invocation_count": normalize_counter.calls,
            "masker_invocation_count": masker_counter.calls,
        },
    )


def test_truncated_artifact_is_rejected(tmp_path: Path, policy: MaskingPolicy) -> None:
    """Sizeが1Byteでも違えばBitmapとして読まない。

    途中まで正しいBitmapは、途中までは正しい判定を返す。中途半端に
    動くほうが、動かないより危険である。
    """
    broken = tmp_path / "short"
    broken.write_bytes(b"\x00" * (BITMAP_BYTES - 1))
    with pytest.raises(HarnessError) as error:
        build(broken, policy)
    assert error.value.code is ErrorCode.MASKING_NORMALIZATION_ARTIFACT_MISSING
    assert "size" in str(error.value)


def test_oversized_artifact_is_rejected(tmp_path: Path, policy: MaskingPolicy) -> None:
    broken = tmp_path / "long"
    broken.write_bytes(b"\x00" * (BITMAP_BYTES + 1))
    with pytest.raises(HarnessError) as error:
        build(broken, policy)
    assert error.value.code is ErrorCode.MASKING_NORMALIZATION_ARTIFACT_MISSING


def test_hash_mismatch_is_rejected(
    tmp_path: Path, policy: MaskingPolicy, artifact_path: Path
) -> None:
    """Sizeが合っていても中身が違えば拒否する。

    1bitの反転で「割当済み／未割当」の判定が逆になる。Sizeだけの検査では
    捕まらない。
    """
    data = bytearray(artifact_path.read_bytes())
    data[0] ^= 0b1  # 1bitだけ反転
    tampered = tmp_path / "tampered"
    tampered.write_bytes(bytes(data))

    with pytest.raises(HarnessError) as error:
        build(tampered, policy)
    assert error.value.code is ErrorCode.MASKING_NORMALIZATION_ARTIFACT_MISSING
    assert "hash" in str(error.value)


def test_correct_artifact_with_wrong_expected_hash_is_rejected(
    policy: MaskingPolicy, artifact_path: Path
) -> None:
    """期待値の側が食い違っていても止める。

    Registryを書き換えただけでArtifactを差し替えていない、という
    配備事故を検出する。
    """
    wrong = "sha256:" + hashlib.sha256(b"not the artifact").hexdigest()
    with pytest.raises(HarnessError) as error:
        build(artifact_path, policy, expected_hash=wrong)
    assert error.value.code is ErrorCode.MASKING_NORMALIZATION_ARTIFACT_MISSING


# ---------------------------------------------------------------------------
# 実行系のUCD版数
# ---------------------------------------------------------------------------


def test_runtime_older_than_the_required_ucd_is_rejected(
    policy: MaskingPolicy, artifact_path: Path
) -> None:
    """`runtime_unicodedata_min_version`。古い実行系では正規化結果が違う。

    UCD 14.0で追加された文字を知らない実行系は、Guardを通した入力でも
    別の結果を出す。Bitmapが正しくても意味が無い。
    """
    with pytest.raises(HarnessError) as error:
        Ucd14Guard(
            artifact_path,
            expected_artifact_sha256=policy.normalization.artifact_sha256,
            profile_id=policy.normalization.profile_id,
            minimum_unicodedata_version="99.0.0",
        )
    assert error.value.code is ErrorCode.MASKING_NORMALIZATION_PROFILE_MISMATCH
    assert "older than" in str(error.value)


# ---------------------------------------------------------------------------
# 未割当符号位置
# ---------------------------------------------------------------------------


def test_codepoint_outside_the_unicode_space_is_not_assigned(
    policy: MaskingPolicy, artifact_path: Path
) -> None:
    """Bitmapの範囲外を問い合わせても例外にせず「未割当」とする。

    範囲外はそもそも文字ではない。呼出側が境界を気にせず使えるようにする。
    """
    guard = build(artifact_path, policy)
    assert guard.is_assigned(0x110000) is False
    assert guard.is_assigned(-1) is False


def test_ucd15_only_codepoint_is_rejected(policy: MaskingPolicy, artifact_path: Path) -> None:
    """UCD 15.0で追加された符号位置は、実行系が3.12でも拒否する。

    Bitmapが14.0である以上、14.0で未割当なら拒否する。実行系の版数に
    引きずられない。ここが揺れると3.11と3.12でHashが割れる。
    """
    guard = build(artifact_path, policy)
    with pytest.raises(HarnessError) as error:
        guard.normalize("text \U0001e030 here")
    assert error.value.code is ErrorCode.MASKING_UNSUPPORTED_CODEPOINT
    assert "U+1E030" in str(error.value)


def test_rejection_message_carries_no_input_text(
    policy: MaskingPolicy, artifact_path: Path
) -> None:
    """Error Messageへ本文を載せない（不変条件#7）。符号位置と位置だけ。"""
    guard = build(artifact_path, policy)
    secret = "alice@example.com"
    with pytest.raises(HarnessError) as error:
        guard.normalize(f"{secret} \U0001e030")
    assert secret not in str(error.value)
