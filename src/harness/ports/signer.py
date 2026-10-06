"""署名Grantの発行・検証Port。"""

from __future__ import annotations

from typing import Protocol

__all__ = ["SignerPort"]


class SignerPort(Protocol):
    @property
    def algorithm(self) -> str: ...

    @property
    def issuer_key_id(self) -> str: ...

    def sign(self, payload: bytes) -> str: ...

    def verify(self, payload: bytes, signature: str) -> bool: ...
