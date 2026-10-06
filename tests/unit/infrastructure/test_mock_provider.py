from __future__ import annotations

import pytest

from harness.domain.hashing import hash_canonical
from harness.infrastructure.provider.mock_provider import DeterministicMockProvider
from harness.ports.provider import ProviderRequest

pytestmark = pytest.mark.unit


def _request(task: str) -> ProviderRequest:
    return ProviderRequest(
        provider_id="mock",
        model_id="mock-v1",
        instruction_hash=hash_canonical(
            {"instruction": "fixed"}, artifact_type="test-value", schema_major=1
        ),
        context_bundle_hash=hash_canonical(
            {"context": task}, artifact_type="test-value", schema_major=1
        ),
        input_artifact_hash=hash_canonical(
            {"task": task}, artifact_type="test-value", schema_major=1
        ),
        output_schema_hash=hash_canonical(
            {"schema": "artifact"}, artifact_type="test-value", schema_major=1
        ),
    )


def test_mock_provider_is_deterministic_and_declares_no_network_or_billing() -> None:
    provider = DeterministicMockProvider(adapter_version="mock-adapter/1.0")

    first = provider.propose(_request("update notes"))
    second = provider.propose(_request("update notes"))

    assert first.artifact_bytes == second.artifact_bytes
    assert first.artifact_hash == second.artifact_hash
    assert first.provider_id == "mock"
    assert first.network_used is False
    assert first.billing_mode == "FREE"


def test_mock_provider_changes_output_when_frozen_request_changes() -> None:
    provider = DeterministicMockProvider(adapter_version="mock-adapter/1.0")

    assert (
        provider.propose(_request("a")).artifact_hash
        != provider.propose(_request("b")).artifact_hash
    )
