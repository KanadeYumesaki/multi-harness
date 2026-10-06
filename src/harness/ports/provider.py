"""Provider Adapter Port。

MVP0-Aでは``provider_id=mock``だけを許可し、実Network・認証・請求を伴わない。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from harness.domain.hashing import ContentHash

__all__ = ["ProviderPort", "ProviderRequest", "ProviderResponse"]


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """Provider へ渡す要求。

    ## 必須性を正本 Schema へ揃える（Owner Decision `MTM-5-A`）

    `InvocationManifest@1.0.0` は `instruction_hash` と `output_schema_hash` を
    `required` に入れていない。**正本が任意としているものを Port だけ必須に
    しない。** 既存 Schema は 1 Byte も変えない。

    渡さないことと、空文字やダミー Hash で埋めることは違う。**埋めない。**
    無いものは `None` で表す。
    """

    provider_id: str
    model_id: str
    context_bundle_hash: ContentHash
    input_artifact_hash: ContentHash
    instruction_hash: ContentHash | None = None
    output_schema_hash: ContentHash | None = None

    def __post_init__(self) -> None:
        if not self.provider_id or not self.model_id:
            raise ValueError("provider_id and model_id must not be empty")


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    provider_id: str
    model_id: str
    adapter_version: str
    artifact_bytes: bytes
    artifact_hash: ContentHash
    network_used: bool
    billing_mode: str


class ProviderPort(Protocol):
    def propose(self, request: ProviderRequest) -> ProviderResponse: ...
