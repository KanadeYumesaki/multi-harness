"""ADR-007 MaskerPort。**LLMは座標しか返せない。**

## 戻り値が`str`である理由

`propose_spans` は構造化済みの`Span`ではなく生のJSON文字列を返す。
Portの向こう側は信用境界の外であり、「型が付いている」ことと
「内容が妥当である」ことは別だからである。ここで`tuple[Span, ...]`を
返す型にすると、実装が`Span`を組み立てた時点で検証を通過したかのように
見えてしまう。生Bytesを受け取り、`masker_output.parse_masker_output()`が
明示的に検証する形にしておけば、`MASKER_OUTPUT_MALFORMED` の経路が
実際に存在し、試験できる。

## 本文を返させない

Maskerがマスク済み本文を返す設計は採らない。欠落・重複・並べ替え・
意味改変を決定論的に検出できないためである（ADR-007 §2）。
座標だけなら、Maskerが影響できるのは「どこを隠すか」であって
「何が書かれるか」ではない。置換はHarness側の決定論Rewriterが行う。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from harness.domain.masking import Span
from harness.domain.masking_result import MaskerDescriptor

__all__ = [
    "MaskerDescriptor",
    "MaskerIsolationReport",
    "MaskerPort",
    "MaskerRequest",
    "MaskerUnavailableError",
    "MaskerUnavailableReason",
]


class MaskerUnavailableReason(Enum):
    """Maskerが使えない理由。**安全な語彙だけ**を持つ。

    自由文のMessageを診断に使うと、Maskerが作った文字列がLogやEventへ
    流れる。Maskerは信用境界の外にあり、本文の断片やSecret Canaryを
    そこへ載せられる。かといって理由を捨てると原因が追えない。
    語彙を固定して、そこだけを通す。
    """

    TIMEOUT = "TIMEOUT"
    LAUNCH_FAILED = "LAUNCH_FAILED"
    NONZERO_EXIT = "NONZERO_EXIT"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    UNSPECIFIED = "UNSPECIFIED"


class MaskerUnavailableError(Exception):
    """Maskerを呼べない／時間内に返らない。`MASKER_UNAVAILABLE`へ写す。

    Pipelineはこれを捕捉してREJECTする。マスクなしでの通過は無い
    （`never_pass_unmasked: true`）。

    `reason` だけがPipelineへ渡る。例外Messageは実装者の診断用であり、
    **Report・Event・Ledgerのどこへも転記されない**。
    """

    def __init__(
        self,
        message: str = "",
        *,
        reason: MaskerUnavailableReason = MaskerUnavailableReason.UNSPECIFIED,
    ) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class MaskerRequest:
    """Maskerへ渡す入力（ADR-007 §2）。

    `text` はNFC正規化済みで、Scan#1がREJECTカテゴリを含まないことを
    確認済みである。**この不変条件が破れるとRaw SecretがLLMへ渡る。**

    `normalization_profile` と `normalization_profile_artifact_hash` を
    渡すのは、Maskerが座標をどの基準で数えるべきかを明示するためである。
    Maskerはこの3値をそのまま応答へEcho Backし、Harnessが一致を検証する。
    Profileが違えば「何文字目か」の意味が変わるため、座標だけを信頼する
    本設計では基準の一致確認が本文Hashの確認と同格に重要になる。
    """

    text: str
    source_normalized_hash: str
    normalization_profile: str
    normalization_profile_artifact_hash: str
    allowed_categories: tuple[str, ...]
    candidate_spans: tuple[Span, ...]


@dataclass(frozen=True, slots=True)
class MaskerIsolationReport:
    """§ADR-007 §7。Raw PIIを渡す前に確認する隔離状態。

    `enforcement: PRE_LAUNCH_VERIFY`。起動後に確認する形にはできない。
    1度起動すればRaw PIIが渡り得るためである。
    """

    network_egress_denied: bool
    telemetry_disabled: bool
    prompt_logging_disabled: bool
    core_dump_disabled: bool
    temp_files_disallowed: bool
    memory_locked: bool

    # どの項目を要求するかはRegistry側が決める。判定は
    # `MaskerIsolationPolicy.unmet()` が行う。本型は観測値だけを持つ。


class MaskerPort(Protocol):
    def descriptor(self) -> MaskerDescriptor:
        """自身の同定情報を返す。Receiptへ記録される。"""
        ...

    def verify_isolation(self) -> MaskerIsolationReport:
        """隔離状態を返す。Pipelineは呼出しの**前**にこれを確認する。"""
        ...

    def propose_spans(self, request: MaskerRequest) -> str:
        """マスク範囲の提案をJSON文字列で返す（ADR-007 §2）。

        期待形式は次の4項目ちょうどである。過不足はいずれも拒否される。

        ```json
        {
          "source_normalized_hash": "sha256:...",
          "normalization_profile": "NFC_CODEPOINT_V3",
          "normalization_profile_artifact_hash": "sha256:...",
          "spans": [{"start": 120, "end": 132, "category": "EMAIL"}]
        }
        ```

        先頭3項目は`request`の値をそのままEcho Backする。検証は呼出側の
        `parse_masker_output()` が行う。時間切れ・利用不能は
        `MaskerUnavailableError` を送出する。
        """
        ...
