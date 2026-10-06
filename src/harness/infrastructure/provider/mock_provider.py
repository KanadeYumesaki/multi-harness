"""外部I/Oを行わないMVP0-A用の決定論的Mock Provider。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from harness.domain.canonical import canonicalize
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_bytes
from harness.ports.provider import ProviderRequest, ProviderResponse

__all__ = ["DeterministicMockProvider"]


@dataclass(frozen=True, slots=True)
class DeterministicMockProvider:
    adapter_version: str

    def __post_init__(self) -> None:
        if not self.adapter_version:
            raise ValueError("adapter_version must not be empty")

    def propose(self, request: ProviderRequest) -> ProviderResponse:
        if request.provider_id != "mock":
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH, "MVP0-A permits only the mock provider"
            )
        artifact = canonicalize(self._projection(request))
        return ProviderResponse(
            provider_id="mock",
            model_id=request.model_id,
            adapter_version=self.adapter_version,
            artifact_bytes=artifact,
            artifact_hash=hash_bytes(artifact),
            network_used=False,
            billing_mode="FREE",
        )

    def _projection(self, request: ProviderRequest) -> dict[str, Any]:
        return {
            "provider_id": request.provider_id,
            "model_id": request.model_id,
            "adapter_version": self.adapter_version,
            # **無いものを空文字やダミー Hash で埋めない。** `None` のまま出す。
            "instruction_hash": (
                None if request.instruction_hash is None else str(request.instruction_hash)
            ),
            "context_bundle_hash": str(request.context_bundle_hash),
            "input_artifact_hash": str(request.input_artifact_hash),
            "output_schema_hash": (
                None if request.output_schema_hash is None else str(request.output_schema_hash)
            ),
            "network": "NONE",
            "billing_mode": "FREE",
        }
