from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.domain.hashing import hash_canonical
from harness.infrastructure.sqlite.migrations import SCHEMA_VERSION
from harness.presentation.cli import main

pytestmark = pytest.mark.integration


def _hash(label: str) -> str:
    return str(hash_canonical({"label": label}, artifact_type="test-value", schema_major=1))


def test_cli_migrates_verifies_empty_ledger_and_invokes_mock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "harness.db"
    assert (
        main(["migrate", "--database", str(database), "--recorded-at", "2026-08-15T00:00:00Z"]) == 0
    )
    # 件数を手入力しない。Migration の正本と突き合わせる（不変条件#18）。
    assert json.loads(capsys.readouterr().out)["schema_version"] == SCHEMA_VERSION

    assert main(["verify-ledger", "--database", str(database), "--stream-id", "run-1"]) == 0
    assert json.loads(capsys.readouterr().out) == {"stream_id": "run-1", "valid": True}

    assert (
        main(
            [
                "mock-propose",
                "--database",
                str(database),
                "--model",
                "mock-v1",
                "--instruction-hash",
                _hash("instruction"),
                "--context-bundle-hash",
                _hash("context"),
                "--input-artifact-hash",
                _hash("input"),
                "--output-schema-hash",
                _hash("schema"),
            ]
        )
        == 0
    )
    response = json.loads(capsys.readouterr().out)
    assert response["provider_id"] == "mock"
    assert response["network_used"] is False
    assert response["billing_mode"] == "FREE"
