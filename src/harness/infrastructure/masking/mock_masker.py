"""決定論Mock Masker。MVP0-Aでは実LLMを使わない。

## Mockで足りる理由

Pipelineが検証するのは「Maskerが何を返してきても安全側で止まるか」であり、
Maskerの検出精度ではない。実LLMは同じ入力に同じ出力を返す保証が無く、
受入Caseの合否が実行ごとに揺れる。それでは
`MASKING_SPAN_CONFLICT` や `MASKING_RATIO_EXCEEDED` の経路を
狙って踏めない。

実Provider Maskerを足すのは、Provider Adapter層（MVP0-B以降）である。
そのときも本Mockは残す。異常系の受入Caseを再現できる唯一の手段だからである。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from harness.ports.masker import (
    MaskerDescriptor,
    MaskerIsolationReport,
    MaskerRequest,
    MaskerUnavailableError,
    MaskerUnavailableReason,
)

__all__ = [
    "FullyIsolatedReport",
    "MalformedOutputMasker",
    "RawSpanMasker",
    "StaticPhraseMasker",
    "UnavailableMasker",
]


def _mock_descriptor(name: str) -> MaskerDescriptor:
    """Mockの同定情報。実Providerではないことが分かる値にする。

    Receiptを見たときに「これはMockの判断である」と即座に分かる必要がある。
    実Provider名に似せると、証跡の読み手が実行環境を取り違える。
    """
    return MaskerDescriptor(
        provider="MOCK",
        model=name,
        model_digest="sha256:" + "0" * 64,
        instruction_hash="sha256:" + "0" * 64,
    )


FullyIsolatedReport = MaskerIsolationReport(
    network_egress_denied=True,
    telemetry_disabled=True,
    prompt_logging_disabled=True,
    core_dump_disabled=True,
    temp_files_disallowed=True,
    memory_locked=True,
)


def _envelope(
    request: MaskerRequest,
    spans: Sequence[tuple[int, int, str]],
    *,
    echo_hash: str | None = None,
    echo_profile: str | None = None,
    echo_artifact: str | None = None,
) -> str:
    """ADR-007 §2 の4項目Envelopeを組む。

    Echo値を差し替えられるようにしてあるのは、
    `MASKING_NORMALIZATION_PROFILE_MISMATCH` 経路を試験で踏むためである。
    """
    return json.dumps(
        {
            "source_normalized_hash": echo_hash or request.source_normalized_hash,
            "normalization_profile": echo_profile or request.normalization_profile,
            "normalization_profile_artifact_hash": (
                echo_artifact or request.normalization_profile_artifact_hash
            ),
            "spans": [
                {"start": start, "end": end, "category": category} for start, end, category in spans
            ],
        },
        ensure_ascii=False,
    )


@dataclass
class StaticPhraseMasker:
    """語句表からSpanを組む。実LLMが人名を見つける役を代替する。

    `phrases` は 語句 → カテゴリ。出現位置を全て提案する。
    重なる語句は長い方を優先する（Scan#1候補の解決と同じ方針）。
    """

    phrases: Mapping[str, str]
    isolation: MaskerIsolationReport = FullyIsolatedReport
    invocations: list[MaskerRequest] = field(default_factory=list)

    def descriptor(self) -> MaskerDescriptor:
        return _mock_descriptor("static-phrase")

    def verify_isolation(self) -> MaskerIsolationReport:
        return self.isolation

    def propose_spans(self, request: MaskerRequest) -> str:
        self.invocations.append(request)
        found: list[tuple[int, int, str]] = []
        for phrase, category in self.phrases.items():
            start = request.text.find(phrase)
            while start != -1:
                found.append((start, start + len(phrase), category))
                start = request.text.find(phrase, start + 1)

        found.sort(key=lambda row: (-(row[1] - row[0]), row[0]))
        selected: list[tuple[int, int, str]] = []
        for start, end, category in found:
            overlaps = any(
                start < chosen_end and chosen_start < end
                for chosen_start, chosen_end, _ in selected
            )
            if overlaps:
                continue
            selected.append((start, end, category))
        selected.sort()

        return _envelope(request, selected)


@dataclass
class MalformedOutputMasker:
    """任意の文字列をそのまま返す。`MASKER_OUTPUT_MALFORMED` 経路の再現用。"""

    output: str
    isolation: MaskerIsolationReport = FullyIsolatedReport
    invocations: list[MaskerRequest] = field(default_factory=list)

    def descriptor(self) -> MaskerDescriptor:
        return _mock_descriptor("malformed-output")

    def verify_isolation(self) -> MaskerIsolationReport:
        return self.isolation

    def propose_spans(self, request: MaskerRequest) -> str:
        self.invocations.append(request)
        return self.output


@dataclass
class UnavailableMasker:
    """常に利用不能。`MASKER_UNAVAILABLE` 経路の再現用。

    Timeoutも同じ扱いである（`on_timeout: REJECT`／`on_unavailable: REJECT`）。
    """

    # `reason_text` は実装者向けの自由文。Pipelineへは渡らないことを
    # 試験で確認するため、意図的にSecretらしき値を入れられるようにしてある。
    reason_text: str = "masker process did not respond"
    reason: MaskerUnavailableReason = MaskerUnavailableReason.UNSPECIFIED
    isolation: MaskerIsolationReport = FullyIsolatedReport
    invocations: list[MaskerRequest] = field(default_factory=list)

    def descriptor(self) -> MaskerDescriptor:
        return _mock_descriptor("unavailable")

    def verify_isolation(self) -> MaskerIsolationReport:
        return self.isolation

    def propose_spans(self, request: MaskerRequest) -> str:
        self.invocations.append(request)
        raise MaskerUnavailableError(self.reason_text, reason=self.reason)


@dataclass
class RawSpanMasker:
    """指定した座標をそのまま返す。Span検証の異常系を狙って踏むため。

    範囲外・逆順・重複・未知カテゴリなど、妥当でない提案を意図的に作る。
    `echo_*` を与えるとEcho Back値を差し替えられる。
    """

    spans: Sequence[tuple[int, int, str]]
    echo_hash: str | None = None
    echo_profile: str | None = None
    echo_artifact: str | None = None
    isolation: MaskerIsolationReport = FullyIsolatedReport
    invocations: list[MaskerRequest] = field(default_factory=list)

    def descriptor(self) -> MaskerDescriptor:
        return _mock_descriptor("raw-span")

    def verify_isolation(self) -> MaskerIsolationReport:
        return self.isolation

    def propose_spans(self, request: MaskerRequest) -> str:
        self.invocations.append(request)
        return _envelope(
            request,
            self.spans,
            echo_hash=self.echo_hash,
            echo_profile=self.echo_profile,
            echo_artifact=self.echo_artifact,
        )
