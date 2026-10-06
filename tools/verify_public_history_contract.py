"""Public gate: exercise real verifiers on synthetic Git, never certify private history."""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import verify_provider_readiness_history as provider
import verify_readiness_report_history as consumer
from synthetic_history_fixture import build_consumer_history, build_provider_history


def verify_contract(kind: str, parent: Path) -> dict[str, Any]:
    """Accept valid synthetic inputs and require rejection of a missing Git input."""
    if kind == "consumer":
        fixture = build_consumer_history(parent / "consumer")
        verifier = consumer
    elif kind == "provider":
        fixture = build_provider_history(parent / "provider")
        verifier = provider
    else:
        raise ValueError("PUBLIC_HISTORY_KIND_INVALID")
    result = verifier.verify(fixture.root, fixture.manifest)
    if (
        result.get("status") != "HISTORICAL_REPRODUCED"
        or result.get("is_runtime_evidence") is not False
    ):
        raise ValueError("PUBLIC_HISTORY_RESULT_INVALID")
    broken = copy.deepcopy(fixture.manifest)
    broken["saved_report"]["source_commit"] = "0" * 40
    try:
        verifier.verify(fixture.root, broken)
    except verifier.HistoryError as exc:
        if str(exc) != "HISTORY_GIT_UNAVAILABLE":
            raise ValueError("PUBLIC_HISTORY_REJECTION_INVALID") from exc
    else:
        raise ValueError("PUBLIC_HISTORY_MISSING_INPUT_ACCEPTED")
    return {
        "status": "SYNTHETIC_CONTRACT_VERIFIED",
        "kind": kind,
        "input_origin": "GENERATED_SYNTHETIC_GIT",
        "accepted_valid_input": True,
        "rejected_missing_git_input": True,
        "private_history_reproduced": False,
        "owner_answers_verified": False,
        "is_runtime_evidence": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("consumer", "provider"))
    args = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix="public-history-contract-") as directory:
            result = verify_contract(args.kind, Path(directory))
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
        consumer.HistoryError,
        provider.HistoryError,
    ) as exc:
        # Input data and subprocess streams can contain paths or record contents.
        print(
            json.dumps(
                {
                    "status": "UNVERIFIED",
                    "error_type": type(exc).__name__,
                    "is_runtime_evidence": False,
                }
            )
        )
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
