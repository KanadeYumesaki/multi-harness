"""The public gate must neither inherit private evidence nor accept a broken verifier."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import verify_public_history_contract as gate

pytestmark = pytest.mark.spec_lint


@pytest.mark.parametrize("kind", ["consumer", "provider"])
def test_public_result_identifies_only_synthetic_contracts(kind: str, tmp_path: Path) -> None:
    result = gate.verify_contract(kind, tmp_path)
    assert result["status"] == "SYNTHETIC_CONTRACT_VERIFIED"
    assert result["accepted_valid_input"] is True
    assert result["rejected_missing_git_input"] is True
    assert result["private_history_reproduced"] is False
    assert result["owner_answers_verified"] is False
    assert result["is_runtime_evidence"] is False


@pytest.mark.parametrize("kind", ["consumer", "provider"])
def test_always_accepting_verifier_cannot_pass_gate(kind: str, tmp_path: Path, monkeypatch) -> None:
    module = gate.consumer if kind == "consumer" else gate.provider
    monkeypatch.setattr(
        module,
        "verify",
        lambda *_: {"status": "HISTORICAL_REPRODUCED", "is_runtime_evidence": False},
    )
    with pytest.raises(ValueError, match="PUBLIC_HISTORY_MISSING_INPUT_ACCEPTED"):
        gate.verify_contract(kind, tmp_path)


def test_cli_failure_is_nonzero_and_does_not_print_input(capsys, monkeypatch) -> None:
    def broken(*_):
        raise OSError("private input text")

    monkeypatch.setattr(gate, "verify_contract", broken)
    assert gate.main(["consumer"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result == {"status": "UNVERIFIED", "error_type": "OSError", "is_runtime_evidence": False}
