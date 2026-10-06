"""失敗の分類と、未信頼文字列の非混入（不変条件#7／#9）。

## 何が問題だったか

Pipelineは `except Exception` で未知例外をすべて
`MASKING_VERIFICATION_FAILED` へ変換していた。そのため運用側から見ると

    Scan#2 が過少マスクを検出して拒否した（機構は正常に働いた）
    Harness内部のBugで落ちた（機構が壊れている）

が同じCodeで並ぶ。前者は日常、後者は即調査であり、混ぜてはいけない。

また `MaskerUnavailableError` の本文をそのまま `detail` へ入れていた。
Maskerは信用境界の外にあり、そのMessageに本文の断片やSecret Canaryが
載り得る。

## ここで固定すること

1. Policy境界の拒否 / マスキング失敗 / 内部障害 の3分類
2. どの経路でも本文を返さない（`never_pass_unmasked` は維持）
3. Maskerが作った文字列を `detail` へ入れない
4. 未知例外でも型名しか残さない
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.domain.errors import ErrorCode
from harness.domain.masking_result import MaskingFailureKind
from harness.infrastructure.masking.mock_masker import (
    FullyIsolatedReport,
    MalformedOutputMasker,
    RawSpanMasker,
    StaticPhraseMasker,
    UnavailableMasker,
)
from harness.infrastructure.masking.pipeline import MaskingPipeline
from harness.infrastructure.masking.policy import MaskingPolicy
from harness.ports.masker import (
    MaskerDescriptor,
    MaskerIsolationReport,
    MaskerRequest,
    MaskerUnavailableReason,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
CANARY = "FDE-HARNESS-CANARY-QX7T2M9P"


@pytest.fixture(scope="module")
def policy() -> MaskingPolicy:
    return MaskingPolicy.load(REPO_ROOT)


def build(policy: MaskingPolicy, masker: object) -> MaskingPipeline:
    return MaskingPipeline(policy, masker, repo_root=REPO_ROOT)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3分類
# ---------------------------------------------------------------------------


def test_policy_rejection_is_not_a_failure(policy: MaskingPolicy) -> None:
    """Secretを弾いたのは機構が正常に働いた結果である。"""
    report = build(policy, StaticPhraseMasker(phrases={})).run("password: hunter2xyz")
    assert report.failure_kind is MaskingFailureKind.POLICY_REJECTION
    assert report.error_code is None
    assert report.is_policy_rejection


def test_masking_failure_is_distinct_from_internal_fault(policy: MaskingPolicy) -> None:
    """Span検証の不合格は「マスキング失敗」。機構は動いている。"""
    masker = RawSpanMasker(spans=[(2, 9999, "PERSON_NAME")])
    report = build(policy, masker).run("担当は山田太郎さんです")
    assert report.failure_kind is MaskingFailureKind.MASKING_FAILURE
    assert report.error_code is ErrorCode.MASKING_SPAN_INVALID


def test_unknown_exception_is_an_internal_fault(policy: MaskingPolicy) -> None:
    """未知例外は**内部障害**として分類する。

    `MASKING_VERIFICATION_FAILED` と同じ棚に置くと、
    「Scan#2が過少マスクを捕まえた」件数と「Bugで落ちた」件数が混ざる。
    前者は日常、後者は即調査である。
    """

    class ExplodingMasker:
        def descriptor(self) -> MaskerDescriptor:
            return MaskerDescriptor(
                provider="MOCK",
                model="exploding",
                model_digest="sha256:" + "0" * 64,
                instruction_hash="sha256:" + "0" * 64,
            )

        def verify_isolation(self) -> MaskerIsolationReport:
            return FullyIsolatedReport

        def propose_spans(self, request: MaskerRequest) -> str:
            raise RuntimeError(f"boom {CANARY} alice@example.com")

    report = build(policy, ExplodingMasker()).run("普通の文章です")
    assert report.failure_kind is MaskingFailureKind.INTERNAL_FAULT
    assert report.rejected
    assert report.masked_text is None


def test_scan2_rejection_is_a_masking_failure_not_an_internal_fault(
    policy: MaskingPolicy,
) -> None:
    """Scan#2の不合格は機構が正しく働いた結果である。"""
    masker = RawSpanMasker(spans=[(6, 7, "PERSON_NAME")])
    report = build(policy, masker).run("code 12345678901234 end")
    assert report.error_code is ErrorCode.MASKING_VERIFICATION_FAILED
    assert report.failure_kind is MaskingFailureKind.MASKING_FAILURE


def test_successful_run_has_no_failure_kind(policy: MaskingPolicy) -> None:
    report = build(policy, StaticPhraseMasker(phrases={})).run("普通の文章です")
    assert report.result == "CLEAN"
    assert report.failure_kind is None


# ---------------------------------------------------------------------------
# 未信頼文字列を detail へ入れない
# ---------------------------------------------------------------------------


def test_masker_unavailable_message_is_not_copied_into_detail(
    policy: MaskingPolicy,
) -> None:
    """Maskerが作った文字列を転記しない。

    Maskerは信用境界の外にある。Messageに本文の断片やSecret Canaryが
    載り得るため、Harness側の固定文言と構造化理由だけを使う。
    """
    masker = UnavailableMasker(reason_text=f"crashed with {CANARY} alice@example.com")
    report = build(policy, masker).run("担当は山田太郎さんです")

    assert report.error_code is ErrorCode.MASKER_UNAVAILABLE
    assert CANARY not in report.detail
    assert "alice@example.com" not in report.detail
    assert "crashed" not in report.detail


def test_unavailable_reason_is_a_structured_token(policy: MaskingPolicy) -> None:
    """診断情報は自由文ではなく列挙で運ぶ。

    自由文を禁じるだけだと原因が分からなくなる。安全な語彙を決めて
    そこだけを通す。
    """
    masker = UnavailableMasker(reason=MaskerUnavailableReason.TIMEOUT, reason_text=f"leak {CANARY}")
    report = build(policy, masker).run("担当は山田太郎さんです")
    assert "TIMEOUT" in report.detail
    assert CANARY not in report.detail


def test_unknown_exception_detail_carries_only_the_type_name(
    policy: MaskingPolicy,
) -> None:
    """例外Messageには本文片が載り得る。型名だけを残す。"""

    class LeakyMasker:
        def descriptor(self) -> MaskerDescriptor:
            return MaskerDescriptor(
                provider="MOCK",
                model="leaky",
                model_digest="sha256:" + "0" * 64,
                instruction_hash="sha256:" + "0" * 64,
            )

        def verify_isolation(self) -> MaskerIsolationReport:
            return FullyIsolatedReport

        def propose_spans(self, request: MaskerRequest) -> str:
            raise ValueError(f"{CANARY} 山田太郎 alice@example.com")

    report = build(policy, LeakyMasker()).run("普通の文章です")
    assert "ValueError" in report.detail
    assert CANARY not in report.detail
    assert "山田太郎" not in report.detail
    assert "alice@example.com" not in report.detail


def test_malformed_output_detail_carries_no_masker_text(policy: MaskingPolicy) -> None:
    """Masker出力そのものも転記しない。"""
    masker = MalformedOutputMasker(output=f"{{invalid {CANARY}}}")
    report = build(policy, masker).run("普通の文章です")
    assert report.error_code is ErrorCode.MASKER_OUTPUT_MALFORMED
    assert CANARY not in report.detail


# ---------------------------------------------------------------------------
# Fail-Closed は維持する
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "masker_factory",
    [
        lambda: UnavailableMasker(),
        lambda: MalformedOutputMasker(output="not json"),
        lambda: RawSpanMasker(spans=[(0, 3, "CREDIT_CARD")]),
    ],
    ids=["unavailable", "malformed", "unknown_category"],
)
def test_no_failure_path_returns_text(policy: MaskingPolicy, masker_factory: object) -> None:
    """`never_pass_unmasked: true`。分類を足しても失効させない。"""
    report = build(policy, masker_factory()).run("担当は山田太郎さんです")  # type: ignore[operator]
    assert report.rejected
    assert report.masked_text is None
    assert report.masked_content_hash is None
