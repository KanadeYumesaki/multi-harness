"""A CLI proposal is replacement data, never a command or a write permission."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash, hash_bytes


def reject_response() -> HarnessError:
    # Do not interpolate an untrusted response into a persisted diagnostic.
    return HarnessError(ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "invalid CLI proposal response")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise reject_response()
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise reject_response()


def strict_response_json(payload: bytes, *, maximum_bytes: int) -> Any:
    if maximum_bytes < 1 or not payload or len(payload) > maximum_bytes:
        raise reject_response()
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError):
        raise reject_response() from None


@dataclass(frozen=True, slots=True)
class CodeProposal:
    replacement: bytes

    @property
    def content_hash(self) -> ContentHash:
        return hash_bytes(self.replacement)

    @classmethod
    def parse(cls, payload: bytes, *, maximum_bytes: int) -> CodeProposal:
        value = strict_response_json(payload, maximum_bytes=maximum_bytes)
        if not isinstance(value, dict) or value.keys() != {"replacement_text"}:
            raise reject_response()
        replacement = value["replacement_text"]
        if not isinstance(replacement, str) or "\x00" in replacement:
            raise reject_response()
        try:
            encoded = replacement.encode("utf-8")
        except UnicodeError:
            raise reject_response() from None
        if len(encoded) > maximum_bytes:
            raise reject_response()
        # Empty replacement is legitimate, but still requires separate diff approval.
        # Secret scanning remains an Application responsibility before CAS persistence.
        return cls(encoded)
