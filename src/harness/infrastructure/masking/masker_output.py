"""Masker出力の検証（ADR-007 §2／§3）。信用境界の外から来たJSONを構造化する。

`json.loads` が通ることと妥当であることは別である。ここでは
**期待した形以外を全て拒否する**。未知Keyの黙殺、型の暗黙変換、
`start`に浮動小数を許す、といった緩和は一切しない。緩めた分だけ、
Maskerが座標以外の何かを送り込む余地が生まれる。

## 出力契約は4項目

ADR-007 §2 が定める形は次である。

```json
{
  "source_normalized_hash": "sha256:...",
  "normalization_profile": "NFC_CODEPOINT_V3",
  "normalization_profile_artifact_hash": "sha256:...",
  "spans": [{"start": 120, "end": 132, "category": "EMAIL"}]
}
```

初版は前2項目のうち`source_normalized_hash`と`spans`しか受理せず、
Profile系2項目を「未知のKey」として拒否していた。**仕様どおりのMaskerが
弾かれる欠陥**であり、レビューで BLOCKER 2 として指摘された。

## Echo Back を照合する意味

3つのHash／識別子をそのまま返させ、要求値と一致することを確認する。

* `source_normalized_hash` … 別の本文に対する応答を取り違えて適用する事故を止める
* `normalization_profile` … Maskerが別の正規化前提で座標を数えた応答を弾く
* `normalization_profile_artifact_hash` … 同じProfile名でも異なるUCD Artifactを
  使っていた場合を弾く

Profileが違えば「何文字目か」の意味が変わる。座標だけを信頼する設計である以上、
**座標の基準が同じであることの確認は本文Hashの確認と同格に重要**である。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.masking import Span

__all__ = ["MaskerProposal", "parse_masker_output"]

_ALLOWED_ROOT_KEYS = frozenset(
    {
        "source_normalized_hash",
        "normalization_profile",
        "normalization_profile_artifact_hash",
        "spans",
    }
)
_ALLOWED_SPAN_KEYS = frozenset({"start", "end", "category"})
# 何を返されても止まるようにする。Spanは1件あたり3項目しかないため、
# 極端な件数はそれ自体が異常である。max_spans検査より前に効く安全弁。
_MAX_RAW_SPANS = 100_000


def _malformed(detail: str) -> HarnessError:
    """値本体を含めない（不変条件#7）。Masker出力にはPIIが載り得る。"""
    return HarnessError(ErrorCode.MASKER_OUTPUT_MALFORMED, detail)


def _profile_mismatch(detail: str) -> HarnessError:
    return HarnessError(ErrorCode.MASKING_NORMALIZATION_PROFILE_MISMATCH, detail)


@dataclass(frozen=True, slots=True)
class MaskerProposal:
    source_normalized_hash: str
    normalization_profile: str
    normalization_profile_artifact_hash: str
    spans: tuple[Span, ...]


def parse_masker_output(
    raw: object,
    *,
    expected_hash: str,
    expected_profile: str,
    expected_profile_artifact_hash: str,
) -> MaskerProposal:
    """Masker出力を検証して構造化する。

    構造・型の不正は`MASKER_OUTPUT_MALFORMED`、正規化前提の不一致は
    `MASKING_NORMALIZATION_PROFILE_MISMATCH`。

    引数を`str`ではなく`object`で受ける。Port実装は信用境界の外にあり、
    型注釈は約束であって保証ではない。`str`で受けると、注釈に反する値が
    来たときの分岐が「到達しないコード」に見えて消される。
    """
    if not isinstance(raw, str):
        raise _malformed(f"masker output is {type(raw).__name__}, not a string")
    try:
        document: Any = json.loads(raw)
    except json.JSONDecodeError as error:
        # 例外Messageには入力片が載るため、位置だけを取り出す。
        raise _malformed(
            f"masker output is not valid JSON (line {error.lineno}, col {error.colno})"
        ) from None

    if not isinstance(document, dict):
        raise _malformed(f"masker output root is {type(document).__name__}, not object")

    unknown = set(document) - _ALLOWED_ROOT_KEYS
    if unknown:
        # **Key名を載せない。** Key名もMaskerが決めた未信頼の文字列であり、
        # Secret Canaryをここへ置かれるとError／Log経由で流出する。
        # 件数だけで異常は分かる。
        raise _malformed(f"masker output has {len(unknown)} unexpected root key(s)")
    missing = _ALLOWED_ROOT_KEYS - set(document)
    if missing:
        # こちらは期待側の定数なので載せてよい。
        raise _malformed(f"missing root keys: {sorted(missing)}")

    echoed_hash = _require_string(document, "source_normalized_hash")
    if echoed_hash != expected_hash:
        # `on_hash_mismatch: REJECT`。値は載せない。
        raise _malformed("source_normalized_hash does not match the request")

    # Profile系は専用Codeで返す。「壊れた出力」と「別前提で動いたMasker」は
    # 運用上の対処が違う。前者はMasker実装の不具合、後者は配備の不整合である。
    echoed_profile = _require_string(document, "normalization_profile")
    if echoed_profile != expected_profile:
        # 受け取った値を載せない。Maskerが決めた未信頼の文字列であり、
        # Secret Canaryを埋め込まれるとError／Log経由で流出する。
        # 期待値だけを示せば運用上の判断には足りる。
        raise _profile_mismatch(
            f"masker did not echo the requested normalization profile "
            f"(expected {expected_profile!r})"
        )
    echoed_artifact = _require_string(document, "normalization_profile_artifact_hash")
    if echoed_artifact != expected_profile_artifact_hash:
        # Profile名が同じでもArtifactが違えば割当済み集合が違う。
        raise _profile_mismatch(
            "masker used a different normalization profile artifact than the request"
        )

    raw_spans = document["spans"]
    if not isinstance(raw_spans, list):
        raise _malformed(f"spans is {type(raw_spans).__name__}, not array")
    if len(raw_spans) > _MAX_RAW_SPANS:
        raise _malformed(f"spans array has {len(raw_spans)} entries")

    spans = tuple(_parse_span(entry, index) for index, entry in enumerate(raw_spans))
    return MaskerProposal(
        source_normalized_hash=echoed_hash,
        normalization_profile=echoed_profile,
        normalization_profile_artifact_hash=echoed_artifact,
        spans=spans,
    )


def _require_string(document: dict[str, Any], key: str) -> str:
    value = document[key]
    if not isinstance(value, str) or not value:
        raise _malformed(f"{key} is not a non-empty string")
    return value


def _parse_span(entry: Any, index: int) -> Span:
    if not isinstance(entry, dict):
        raise _malformed(f"spans[{index}] is {type(entry).__name__}, not object")

    unknown = set(entry) - _ALLOWED_SPAN_KEYS
    if unknown:
        # Root Key と同じ理由でKey名を載せない。件数だけ示す。
        raise _malformed(f"spans[{index}] has {len(unknown)} unexpected key(s)")
    missing = _ALLOWED_SPAN_KEYS - set(entry)
    if missing:
        raise _malformed(f"spans[{index}] is missing keys: {sorted(missing)}")

    start, end = entry["start"], entry["end"]
    for name, value in (("start", start), ("end", end)):
        # `bool`は`int`の派生である。`True`を座標1として通さない。
        if isinstance(value, bool) or not isinstance(value, int):
            raise _malformed(f"spans[{index}].{name} is {type(value).__name__}, not integer")
    category = entry["category"]
    if not isinstance(category, str) or not category:
        raise _malformed(f"spans[{index}].category is not a non-empty string")

    try:
        return Span(start=start, end=end, category=category)
    except ValueError as error:
        # `start < 0` や `end <= start`。Span.__post_init__ が座標しか
        # 載せないため、そのまま伝えてよい。
        raise _malformed(f"spans[{index}]: {error}") from None
