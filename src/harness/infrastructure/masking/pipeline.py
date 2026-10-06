"""ADR-007 マスキングPipeline。

    UCD14 Guard → 標準NFC → Scan#1 → Masker Span提案 → Span検証
      → 決定論Rewriter → Scan#2

## 拒否には2種類ある

* **Policy境界による拒否**（`reject_categories` 検出、上限超過など）
  Error Codeを持たない。仕様どおりに働いた結果であり、失敗ではない。
  `MASKING_RESULT` の `REJECTED` という状態として表す。
* **Pipeline失敗による拒否**（Span不正、Masker利用不能、Scan#2不合格など）
  Error Codeを持つ。

両者を同じCodeへ潰すと、「Secretを含む入力が正しく弾かれた」件数と
「マスキング機構が壊れている」件数が区別できなくなる。運用上、
前者は正常、後者は調査対象である。

## `never_pass_unmasked`

本Moduleは**マスク済み本文をREJECT時に一切返さない**。
`MaskingReport.masked_text` は `REJECTED` のとき常に`None`である。
想定外の例外もここで捕まえてREJECTへ落とす。例外が外へ抜けると
呼出側の`except`次第で「未処理のまま先へ進む」経路ができるためである。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.masking import (
    REWRITER_VERSION,
    MaskingRejection,
    Span,
    apply_mask_tokens,
    merge_candidate_and_llm_spans,
    require_sorted_llm_spans,
    validate_spans,
)
from harness.domain.masking_result import (
    MaskerDescriptor,
    MaskingFailureKind,
    MaskingReport,
    MaskingTrace,
    ScanFinding,
)
from harness.infrastructure.masking.masker_output import parse_masker_output
from harness.infrastructure.masking.policy import MaskingPolicy
from harness.infrastructure.masking.scanner import DeterministicScanner, Disposition
from harness.infrastructure.masking.ucd_guard import Ucd14Guard
from harness.ports.masker import (
    MaskerPort,
    MaskerRequest,
    MaskerUnavailableError,
)

__all__ = [
    "MaskingPipeline",
    "MaskingReport",
    "MaskingTrace",
    "maskable_finding_categories",
]

_CLEAN = "CLEAN"
_MASKED = "MASKED"
_REJECTED = "REJECTED"


@dataclass(slots=True)
class _TraceState:
    """`MaskingTrace` の組立て途中。**可変**。

    Pipelineは段階的に事実を確定させる。確定した分だけを載せた
    不変Snapshotを、結果を返す時点で作る。
    """

    masking_policy_version: int
    normalization_profile: str
    normalization_profile_artifact_hash: str
    scan1_decision: str = "UNKNOWN"
    masker: MaskerDescriptor | None = None
    span_validation_result: str | None = None
    scan2_result: str | None = None
    spans: tuple[Span, ...] = ()

    def snapshot(self) -> MaskingTrace:
        return MaskingTrace(
            masking_policy_version=self.masking_policy_version,
            normalization_profile=self.normalization_profile,
            normalization_profile_artifact_hash=self.normalization_profile_artifact_hash,
            rewriter_version=REWRITER_VERSION,
            scan1_decision=self.scan1_decision,
            masker=self.masker,
            span_validation_result=self.span_validation_result,
            scan2_result=self.scan2_result,
            spans=self.spans,
        )


class MaskingPipeline:
    def __init__(
        self,
        policy: MaskingPolicy,
        masker: MaskerPort,
        *,
        repo_root: Path,
        scanner: DeterministicScanner | None = None,
        guard: Ucd14Guard | None = None,
    ) -> None:
        self._policy = policy
        self._masker = masker
        self._scanner = scanner or DeterministicScanner(
            reject_categories=policy.reject_categories,
            maskable_categories=policy.maskable_categories,
        )
        self._guard = guard or Ucd14Guard(
            repo_root / policy.normalization.artifact_path,
            expected_artifact_sha256=policy.normalization.artifact_sha256,
            profile_id=policy.normalization.profile_id,
            minimum_unicodedata_version=policy.normalization.runtime_minimum_version,
        )

    # ------------------------------------------------------------------

    def run(self, text: str) -> MaskingReport:
        """マスキングを実行する。例外は外へ出さない。

        Traceは呼出しごとに新しく作る。Instanceへ持たせると、
        同じPipelineを2箇所から使ったときに互いのTraceが混ざる。
        """
        trace = _TraceState(
            masking_policy_version=self._policy.policy_version,
            normalization_profile=self._policy.normalization.profile_id,
            normalization_profile_artifact_hash=self._policy.normalization.artifact_sha256,
        )
        try:
            return self._run(text, trace)
        except HarnessError as error:
            # Harnessが自分で作ったErrorである。Messageは値本体を含まない
            # 規約（不変条件#7）で書かれているため、そのまま使ってよい。
            return self._failure(trace, error.code, str(error))
        except Exception as error:
            # **想定外の例外は内部障害**として分類する。
            # Scan#2の不合格と同じ棚に置くと、「機構が正しく働いて拒否した」
            # 件数と「機構が壊れている」件数が混ざる。
            #
            # 本文を返さないのは他の経路と同じ。例外Messageには本文片や
            # Secret Canaryが載り得るため、**型名だけ**を残す。
            return self._failure(
                trace,
                ErrorCode.MASKING_VERIFICATION_FAILED,
                f"unexpected {type(error).__name__} in masking pipeline",
                failure_kind=MaskingFailureKind.INTERNAL_FAULT,
            )

    # ------------------------------------------------------------------

    def _run(self, text: str, trace: _TraceState) -> MaskingReport:
        # --- 0. 上限。LLMへ渡す前に切る -----------------------------------
        size_bytes = len(text.encode("utf-8"))
        if size_bytes > self._policy.max_maskable_bytes:
            return self._policy_rejection(
                trace,
                (),
                (),
                f"input is {size_bytes} bytes, over max_maskable_bytes="
                f"{self._policy.max_maskable_bytes}",
            )

        # --- 1. UCD14 Guard → NFC ----------------------------------------
        # 正規化の**前**にGuardを通す。未割当符号位置は処理系によって
        # 正規化結果が違い、Hashが処理系依存になる。
        normalization = self._guard.normalize(text)
        normalized = normalization.normalized
        # Guardが実際に使った値で上書きする。Policy記載値と食い違えば
        # Receiptには使った側が載る。
        trace.normalization_profile = normalization.normalization_profile
        trace.normalization_profile_artifact_hash = (
            normalization.normalization_profile_artifact_hash
        )

        # --- 2. Scan#1 ----------------------------------------------------
        scan1 = self._scanner.scan(normalized)
        reject_categories = scan1.reject_categories
        trace.scan1_decision = (
            "REJECT" if reject_categories else ("MASKABLE" if scan1.candidate_spans else "CLEAN")
        )
        if reject_categories:
            # **Maskerを呼ばない。** 受入Case SECRET_REJECTED_NOT_MASKED /
            # NATIONAL_ID_REJECTED_NOT_MASKED は `masker_invocation_count == 0`
            # を要求する。ここでreturnすることがその保証そのものである。
            return self._policy_rejection(
                trace,
                scan1.findings,
                reject_categories,
                f"scan#1 detected reject categories: {list(reject_categories)}",
                source_normalized_hash=normalization.source_normalized_hash,
            )

        # --- 3. Masker隔離検証（呼出しの前） --------------------------------
        # 要求項目はRegistryが決める。実装側で固定すると、Registryを
        # 緩めても厳しいまま／厳しくしても緩いまま、のどちらかになる。
        unmet = self._policy.isolation.unmet(self._masker.verify_isolation())
        if unmet:
            return self._failure(
                trace,
                ErrorCode.MASKER_ISOLATION_INCOMPLETE,
                f"masker isolation not established: {list(unmet)}",
                findings=scan1.findings,
                source_normalized_hash=normalization.source_normalized_hash,
            )

        # --- 4. Span提案 ---------------------------------------------------
        request = MaskerRequest(
            text=normalized,
            source_normalized_hash=normalization.source_normalized_hash,
            normalization_profile=normalization.normalization_profile,
            normalization_profile_artifact_hash=(normalization.normalization_profile_artifact_hash),
            allowed_categories=self._policy.maskable_categories,
            candidate_spans=scan1.candidate_spans,
        )
        trace.masker = self._masker.descriptor()
        try:
            raw_output = self._masker.propose_spans(request)
        except MaskerUnavailableError as error:
            # **例外Messageを転記しない。** Maskerは信用境界の外にあり、
            # Messageに本文の断片やSecret Canaryを載せられる。
            # 構造化された理由（安全な語彙）だけを使う。
            return self._failure(
                trace,
                ErrorCode.MASKER_UNAVAILABLE,
                f"masker unavailable ({error.reason.value})",
                findings=scan1.findings,
                source_normalized_hash=normalization.source_normalized_hash,
                masker_invocation_count=1,
            )

        # 3値のEcho Backを照合する。座標の基準が同じであることの確認は、
        # 本文Hashの確認と同格に重要である（ADR-007 §2）。
        proposal = parse_masker_output(
            raw_output,
            expected_hash=normalization.source_normalized_hash,
            expected_profile=normalization.normalization_profile,
            expected_profile_artifact_hash=(normalization.normalization_profile_artifact_hash),
        )

        # --- 5. Union と 検証 ----------------------------------------------
        #
        # 順序検査は Union の**前**に置く。Union は結果を sorted() するので、
        # 後から見ても順序は必ず整っており、順序が壊れた出力を返す Masker を
        # そこでは検出できない（設計書 v1.17 §1.16.3.1 / Owner Decision MASK-3-A）。
        unsorted_rejection = require_sorted_llm_spans(proposal.spans, self._policy.span_constraints)
        if unsorted_rejection is not None:
            trace.span_validation_result = "REJECTED"
            return self._rejection_from(
                trace,
                unsorted_rejection,
                scan1.findings,
                normalization.source_normalized_hash,
            )

        merged = merge_candidate_and_llm_spans(scan1.candidate_spans, proposal.spans)
        if isinstance(merged, MaskingRejection):
            trace.span_validation_result = "REJECTED"
            return self._rejection_from(
                trace, merged, scan1.findings, normalization.source_normalized_hash
            )

        rejection = validate_spans(
            normalized,
            merged,
            scan1.candidate_spans,
            known_categories=self._policy.known_categories,
            constraints=self._policy.span_constraints,
        )
        if rejection is not None:
            trace.span_validation_result = "REJECTED"
            return self._rejection_from(
                trace, rejection, scan1.findings, normalization.source_normalized_hash
            )

        trace.span_validation_result = "ACCEPTED"
        trace.spans = merged
        # --- 6. 決定論Rewriter ---------------------------------------------
        # Maskerが返した文字列は使わない。Tokenは Registry 固定値だけ。
        masked = apply_mask_tokens(normalized, merged, self._policy.mask_tokens)

        # --- 7. Scan#2。ここが本当のゲート ----------------------------------
        scan2 = self._scanner.scan(masked)
        if not scan2.is_clean:
            trace.scan2_result = "REJECTED"
            # 過少マスク。Maskerが候補を無視した、あるいは置換で新たな
            # 一致が生まれた場合に起きる。どちらも通さない。
            remaining = [f"{finding.category}x{finding.count}" for finding in scan2.findings]
            return self._failure(
                trace,
                ErrorCode.MASKING_VERIFICATION_FAILED,
                f"scan#2 still detects sensitive content: {remaining}",
                findings=scan2.findings,
                source_normalized_hash=normalization.source_normalized_hash,
                masker_invocation_count=1,
            )

        trace.scan2_result = "ACCEPTED"
        applied = tuple(sorted({span.category for span in merged}))
        return MaskingReport(
            result=_MASKED if merged else _CLEAN,
            masked_text=masked,
            source_normalized_hash=normalization.source_normalized_hash,
            masked_content_hash="sha256:" + hashlib.sha256(masked.encode("utf-8")).hexdigest(),
            findings=scan1.findings,
            applied_categories=applied,
            span_count=len(merged),
            rejected_categories=(),
            error_code=None,
            detail="",
            masker_invocation_count=1,
            trace=trace.snapshot(),
        )

    # ------------------------------------------------------------------
    # 結果生成。REJECT時は`masked_text`を必ず`None`にする
    # ------------------------------------------------------------------

    @staticmethod
    def _blank(
        trace: _TraceState,
        *,
        findings: tuple[ScanFinding, ...],
        source_normalized_hash: str | None,
        rejected_categories: tuple[str, ...],
        error_code: ErrorCode | None,
        detail: str,
        masker_invocation_count: int,
        failure_kind: MaskingFailureKind,
    ) -> MaskingReport:
        return MaskingReport(
            result=_REJECTED,
            masked_text=None,
            source_normalized_hash=source_normalized_hash,
            masked_content_hash=None,
            findings=findings,
            applied_categories=(),
            span_count=0,
            rejected_categories=rejected_categories,
            error_code=error_code,
            detail=detail,
            masker_invocation_count=masker_invocation_count,
            trace=trace.snapshot(),
            failure_kind=failure_kind,
        )

    def _policy_rejection(
        self,
        trace: _TraceState,
        findings: tuple[ScanFinding, ...],
        rejected_categories: tuple[str, ...],
        detail: str,
        source_normalized_hash: str | None = None,
    ) -> MaskingReport:
        return self._blank(
            trace,
            findings=findings,
            source_normalized_hash=source_normalized_hash,
            rejected_categories=rejected_categories,
            error_code=None,
            detail=detail,
            masker_invocation_count=0,
            failure_kind=MaskingFailureKind.POLICY_REJECTION,
        )

    def _failure(
        self,
        trace: _TraceState,
        code: ErrorCode,
        detail: str,
        findings: tuple[ScanFinding, ...] = (),
        source_normalized_hash: str | None = None,
        masker_invocation_count: int = 0,
        failure_kind: MaskingFailureKind = MaskingFailureKind.MASKING_FAILURE,
    ) -> MaskingReport:
        return self._blank(
            trace,
            findings=findings,
            source_normalized_hash=source_normalized_hash,
            rejected_categories=(),
            error_code=code,
            detail=detail,
            masker_invocation_count=masker_invocation_count,
            failure_kind=failure_kind,
        )

    def _rejection_from(
        self,
        trace: _TraceState,
        rejection: MaskingRejection,
        findings: tuple[ScanFinding, ...],
        source_normalized_hash: str,
    ) -> MaskingReport:
        return self._failure(
            trace,
            rejection.error_code,
            rejection.detail,
            findings=findings,
            source_normalized_hash=source_normalized_hash,
            masker_invocation_count=1,
        )


def maskable_finding_categories(
    findings: tuple[ScanFinding, ...],
) -> tuple[str, ...]:
    """MASKABLE側の検出カテゴリ。Event Payload組立て用。"""
    return tuple(
        sorted(
            {
                finding.category
                for finding in findings
                if finding.disposition is Disposition.MASKABLE
            }
        )
    )
