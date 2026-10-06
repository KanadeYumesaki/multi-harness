"""Ed25519 Signer。秘密鍵値をLedger・Artifact・Logへ書き込まない。"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

__all__ = ["Ed25519Signer", "Ed25519Verifier"]


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    try:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + padding)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("signature must be base64url") from exc


@dataclass(frozen=True, slots=True)
class Ed25519Verifier:
    public_key: Ed25519PublicKey
    issuer_key_id: str

    @property
    def algorithm(self) -> str:
        return "Ed25519"

    def verify(self, payload: bytes, signature: str) -> bool:
        try:
            self.public_key.verify(_decode(signature), payload)
        except (InvalidSignature, ValueError):
            return False
        return True

    def public_key_bytes(self) -> bytes:
        return self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )


@dataclass(frozen=True, slots=True)
class Ed25519Signer:
    private_key: Ed25519PrivateKey
    issuer_key_id: str

    @classmethod
    def generate(cls, issuer_key_id: str) -> Ed25519Signer:
        if not issuer_key_id:
            raise ValueError("issuer_key_id must not be empty")
        return cls(private_key=Ed25519PrivateKey.generate(), issuer_key_id=issuer_key_id)

    @property
    def algorithm(self) -> str:
        return "Ed25519"

    def sign(self, payload: bytes) -> str:
        return _encode(self.private_key.sign(payload))

    def verify(self, payload: bytes, signature: str) -> bool:
        return self.verifier().verify(payload, signature)

    def verifier(self) -> Ed25519Verifier:
        return Ed25519Verifier(self.private_key.public_key(), self.issuer_key_id)

    def private_key_bytes(self) -> bytes:
        return self.private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
