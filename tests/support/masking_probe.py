"""Masking Case の観測。**原文・PII・Secretを一切載せない。**

## なぜ専用の観測器が要るか

`MaskingReport` は `masked_text` を持つ。`CLEAN` 判定のときの `masked_text` は
**原文そのもの**である。素直に記録すると原文が Evidence へ入る。

そこでこの観測器は、載せてよい値を**allowlistで決める**。report から自動で
拾わない。拾う方式にすると、Report に Field が増えた瞬間に中身が漏れ始める
（不変条件#7）。

## masking_receipt_hash

実装に `masking_receipt_hash` という Field は無い。Receipt の同一性は
Policy Version・正規化Profile・Scan結果・Span集合で決まる（ADR-007 §5）。
ここではその**実測した構成要素だけ**からDomain分離Hashを導出する。

原文も `masked_text` も入れない。入れれば Hash から中身を推定する経路が残る。
"""

from __future__ import annotations

from typing import Any

from harness.domain.hashing import hash_canonical

__all__ = ["masking_evidence_payload", "masking_receipt_hash"]

#: Evidence へ載せてよいTrace項目。ここに無いものは載せない。
_TRACE_ALLOWLIST = (
    "masking_policy_version",
    "normalization_profile",
    "normalization_profile_artifact_hash",
    "rewriter_version",
    "scan1_decision",
    "span_validation_result",
    "scan2_result",
)


def _text(value: Any) -> str | None:
    """Hash等の値オブジェクトを文字列へ落とす。JSONへ載せるため。"""
    return None if value is None else str(value)


def _span_shape(spans: Any) -> list[dict[str, Any]]:
    """Span を位置とカテゴリだけへ落とす。**本文を持たせない。**"""
    shaped: list[dict[str, Any]] = []
    for span in spans or ():
        shaped.append(
            {
                "start": int(span.start),
                "end": int(span.end),
                "category": str(span.category),
            }
        )
    return shaped


def _finding_shape(findings: Any) -> list[dict[str, Any]]:
    """検出結果を件数とRule IDへ落とす。**検出した実値は載せない。**"""
    shaped: list[dict[str, Any]] = []
    for finding in findings or ():
        shaped.append(
            {
                "rule_id": str(finding.rule_id),
                "category": str(finding.category),
                "disposition": str(finding.disposition),
                "count": int(finding.count),
            }
        )
    return sorted(shaped, key=lambda item: (item["rule_id"], item["category"]))


def _trace_fields(report: Any) -> dict[str, Any]:
    trace = getattr(report, "trace", None)
    fields: dict[str, Any] = {}
    for name in _TRACE_ALLOWLIST:
        value = getattr(trace, name, None)
        fields[name] = value if value is None or isinstance(value, int | bool) else str(value)
    return fields


def masking_receipt_hash(report: Any) -> str:
    """Receipt の同一性Hash。実測した構成要素だけから導出する。

    束縛するのは Policy Version・正規化Profile・Scan結果・Span集合であり、
    **原文・masked_text・PII実値は含めない。**
    """
    trace = getattr(report, "trace", None)
    body = {
        **_trace_fields(report),
        "result": str(report.result),
        "span_count": int(report.span_count),
        "applied_categories": sorted(str(c) for c in (report.applied_categories or ())),
        "rejected_categories": sorted(str(c) for c in (report.rejected_categories or ())),
        "source_normalized_hash": _text(report.source_normalized_hash),
        "masked_content_hash": _text(report.masked_content_hash),
        "spans": _span_shape(getattr(trace, "spans", ())),
    }
    return str(hash_canonical(body, artifact_type="masking-receipt", schema_major=1))


def masking_evidence_payload(report: Any, *, input_label: str) -> dict[str, Any]:
    """Input Fixture へ載せる観測値。原文を含めない。

    `input_label` は入力の**種類名**であり入力本文ではない。
    例：`EMAIL_IN_SENTENCE`。ここへ原文を渡してはならない。
    """
    return {
        "input_label": input_label,
        **_trace_fields(report),
        "result": str(report.result),
        "span_count": int(report.span_count),
        "applied_categories": sorted(str(c) for c in (report.applied_categories or ())),
        "rejected_categories": sorted(str(c) for c in (report.rejected_categories or ())),
        "masker_invocation_count": int(report.masker_invocation_count),
        "failure_kind": None if report.failure_kind is None else str(report.failure_kind),
        "source_normalized_hash": _text(report.source_normalized_hash),
        "masked_content_hash": _text(report.masked_content_hash),
        "masking_receipt_hash": masking_receipt_hash(report),
        "findings": _finding_shape(report.findings),
        "spans": _span_shape(getattr(getattr(report, "trace", None), "spans", ())),
    }
