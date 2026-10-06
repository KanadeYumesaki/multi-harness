"""Official ChatGPT OAuth and HTTP boundaries. Credentials never enter Application data."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from harness.domain.hashing import ContentHash
from harness.ports.cli_workbench import CliProviderStatus


@dataclass(frozen=True, slots=True)
class SecretRef:
    """A reference to an in-memory credential, never its value."""

    reference: str


@dataclass(frozen=True, slots=True)
class HttpGenerationSpec:
    provider_id: str
    model_id: str
    runtime_hash: ContentHash
    runtime_projection: Mapping[str, Any]
    credential_ref: SecretRef
    reasoning_effort: str | None = None


@dataclass(frozen=True, slots=True)
class HttpGenerationResult:
    outcome: str
    payload: bytes
    http_status: int | None
    terminal_event: str | None
    error_code: str | None = None

    def observation(self) -> dict[str, Any]:
        return {
            "transport": "HTTPS_RESPONSES_SSE",
            "outcome": self.outcome,
            "http_status": self.http_status,
            "terminal_event": self.terminal_event,
            "response_bytes": len(self.payload),
            "error_code": self.error_code,
            "retry_count": 0,
        }


class ChatGptAuthPort(Protocol):
    def bind(self, metadata: Mapping[str, str]) -> None: ...
    def status(self) -> dict[str, Any]: ...
    def start(self, *, new_account: bool = False) -> dict[str, Any]: ...
    def disconnect(self) -> dict[str, Any]: ...
    def close(self) -> None: ...


class ChatGptMetadataPort(Protocol):
    def load(self) -> dict[str, str]: ...
    def save(self, metadata: Mapping[str, str]) -> None: ...


class ChatGptGenerationPort(Protocol):
    def statuses(self) -> tuple[CliProviderStatus, ...]: ...
    def resolve(
        self, *, provider_id: str, model_id: str, reasoning_effort: str | None = None
    ) -> HttpGenerationSpec: ...
    def verify(self, spec: HttpGenerationSpec) -> None: ...
    def attest(self, spec: HttpGenerationSpec) -> Mapping[str, Any]: ...
    def run(
        self, spec: HttpGenerationSpec, *, payload: bytes, timeout_seconds: int, maximum_bytes: int
    ) -> HttpGenerationResult: ...
    def stop(self) -> None: ...
