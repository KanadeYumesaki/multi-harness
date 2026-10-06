"""マスキングの結果型（純粋データ）。

## なぜdomainに置くか

`MaskingReport` と `ScanFinding` は Application層が扱う戻り値である。
CLAUDE.md §2 は「application/ は ports/ の抽象だけへ依存」と定めるため、
これらが `infrastructure/` にあると Application層が具象を直接importすることになる。

実際、初版は `application/masking_service.py` が
`infrastructure.masking.pipeline` と `infrastructure.sqlite.*` を直接
importしていた。層の規則違反であり、自己レビューで発見した。

型自体はI/Oを持たない純粋データなので、domainが正しい置き場である。
Scan規則の実装（正規表現・エントロピー判定）は infrastructure に残る。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from harness.domain.errors import ErrorCode
from harness.domain.masking import Span

__all__ = [
    "Disposition",
    "MaskerDescriptor",
    "MaskingFailureKind",
    "MaskingReport",
    "MaskingTrace",
    "ScanFinding",
]


class MaskingFailureKind(Enum):
    """失敗の種類。運用上の対処が違うものを混ぜない。

    以前は未知例外もScan#2不合格も同じ `MASKING_VERIFICATION_FAILED` へ
    潰していた。そうすると「機構が正しく働いて拒否した」件数と
    「機構が壊れている」件数が同じ棚に並ぶ。前者は日常、後者は即調査であり、
    数えられなければ運用判断ができない。
    """

    # Policy境界による拒否。仕様どおりに働いた結果であり失敗ではない。
    POLICY_REJECTION = "POLICY_REJECTION"
    # マスキングとして不合格。機構は動いている。
    MASKING_FAILURE = "MASKING_FAILURE"
    # Harness内部の障害。想定していない例外が出た。
    INTERNAL_FAULT = "INTERNAL_FAULT"


class Disposition(Enum):
    """検出時の扱い。"""

    REJECT = "REJECT"
    MASKABLE = "MASKABLE"


@dataclass(frozen=True, slots=True)
class ScanFinding:
    """検出の要約。**値を持たない**（不変条件#7）。

    検出したSecretそのものをLog・Event・Errorへ載せると、検出機構が
    漏洩経路になる。Fieldを4つに固定することで、値を運ぶ余地を無くす。
    """

    rule_id: str
    category: str
    disposition: Disposition
    count: int


@dataclass(frozen=True, slots=True)
class MaskerDescriptor:
    """どのMaskerが判断したかの同定情報（Core Schema `MaskingReceipt`）。

    Maskerが変わればマスク結果も変わり得る。Receiptにこれが無いと、
    後から「どの実装の判断だったか」を辿れない。Model Digestと
    Instruction Hashまで含めるのは、同じModel名でも重みや指示が
    差し替わり得るためである。
    """

    provider: str
    model: str
    model_digest: str
    instruction_hash: str


@dataclass(frozen=True, slots=True)
class MaskingTrace:
    """Receiptへ載せる実行Context（Core Schema `MaskingReceipt`）。

    `MaskingReport` は「何が起きたか」を返すが、Receiptは
    **「どの前提で判断したか」**を要求する。Policy版数・正規化Profile・
    Masker同定・Rewriter版数が無いと、後から結果を再現できない。

    改変不能にしてあるのは、Serviceが組み立て直せてはならないためである。
    Serviceが値を作れると、実際に使った前提と記録が食い違い得る。
    ここはPipelineが**実際に使った値**だけを載せる。
    """

    masking_policy_version: int
    normalization_profile: str
    normalization_profile_artifact_hash: str
    rewriter_version: str
    # Scan#1 の判断。REJECTなら以降は走っていない。
    scan1_decision: str
    # Maskerを呼んでいない場合は None。
    masker: MaskerDescriptor | None = None
    # Span検証・Scan#2 は到達した場合のみ ACCEPTED／REJECTED。
    span_validation_result: str | None = None
    scan2_result: str | None = None
    spans: tuple[Span, ...] = ()


@dataclass(frozen=True, slots=True)
class MaskingReport:
    """`MASKING_RESULT` 名前空間の結果。

    値本体を持つのは`masked_text`だけであり、それは`REJECTED`では`None`である。
    `findings`／`rejected_categories`は件数とカテゴリ名しか持たないため、
    そのままEvent Payloadへ載せられる（不変条件#7）。
    """

    result: str
    masked_text: str | None
    source_normalized_hash: str | None
    masked_content_hash: str | None
    findings: tuple[ScanFinding, ...]
    applied_categories: tuple[str, ...]
    span_count: int
    rejected_categories: tuple[str, ...]
    error_code: ErrorCode | None
    detail: str
    masker_invocation_count: int
    # Receiptが要求する実行Context。どの経路で終わっても必ず載る。
    trace: MaskingTrace
    # 成功時は None。失敗の種類は Error Code と別に持つ。
    failure_kind: MaskingFailureKind | None = None

    @property
    def rejected(self) -> bool:
        return self.result == "REJECTED"

    @property
    def is_policy_rejection(self) -> bool:
        """Policy境界による拒否（正常動作）。Pipeline失敗ではない。"""
        return self.rejected and self.error_code is None
