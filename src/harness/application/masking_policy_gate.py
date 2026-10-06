"""Masking Policy の判定を Input Read の判定へ引き取る（設計書 v1.17 §1.16.2.3）。

## なぜ変換規則を1箇所へ置くか

同じ拒否でも層で Subject と State が変わる。

| 層 | Subject | State |
|---|---|---|
| Masking Pipeline内部 | `MASKING_RESULT` | `REJECTED` |
| Orchestration | `INPUT_READ_DECISION` | `DENIED` |

どちらかが誤りではなく、同じ事象の2つの側面である。だからこそ**変換できる口を
1つに絞る**。あちこちで `REJECTED` を `DENIED` へ読み替えられるなら、
Enum をまたぐ変換に根拠が要るという §19.1 の制約が意味を失う。

Owner Decision MASK-2-C は「変換規則を設計書へ明記して正本化する」を選んだ。
本 Module がその実装であり、ここを通らない変換を作らない。

## Error Code を持たせない

Policy 境界による拒否は仕様どおりの動作であって失敗ではない
（§1.16.3.2 / MASK-5-C）。Code を付けると「仕様どおり止まった」と
「検証が失敗した」が同じ形で記録され、運用で区別できなくなる。

## 原文を持ち出さない

返すのは分類名と呼出し回数だけである。何が見つかったかはカテゴリ名で表す。
"""

from __future__ import annotations

from harness.domain.errors import ErrorCode, HarnessError
from harness.ports.masking import MaskingPipelinePort, MaskingPolicyDenial

__all__ = ["MaskingPolicyGate"]


class MaskingPolicyGate:
    """分類済み入力を Masking Pipeline へ通し、拒否を Input Read の判定へ写す。"""

    def __init__(self, pipeline: MaskingPipelinePort, *, encoding: str = "utf-8") -> None:
        self._pipeline = pipeline
        self._encoding = encoding

    def evaluate(self, payload: bytes, classification: str | None) -> MaskingPolicyDenial | None:
        """拒否なら `MaskingPolicyDenial`、通すなら `None` を返す。

        復号できない Bytes は判定できない。**通さない。** 判定不能を
        「問題なし」へ倒すと、読めなかった入力がそのまま先へ進む。
        """
        try:
            text = payload.decode(self._encoding)
        except UnicodeDecodeError as error:
            raise HarnessError(
                ErrorCode.EFFECT_UNKNOWN,
                f"input bytes are not decodable as {self._encoding}; "
                "masking policy cannot judge them",
            ) from error

        report = self._pipeline.run(text)
        if not report.rejected:
            return None
        return MaskingPolicyDenial(
            rejected_categories=tuple(str(c) for c in report.rejected_categories),
            masker_invocation_count=int(report.masker_invocation_count),
        )
