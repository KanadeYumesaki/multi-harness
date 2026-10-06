"""Verify OpenAI ID tokens with the existing cryptography dependency."""

from __future__ import annotations

import base64
import json
import math
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from harness.infrastructure.provider.chatgpt_http import ChatGptTransportError


def decode_base64(value: str) -> bytes:
    if not value or len(value) > 65536:
        raise ChatGptTransportError("ID_TOKEN_INVALID")
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except ValueError:
        raise ChatGptTransportError("ID_TOKEN_INVALID") from None


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ChatGptTransportError("ID_TOKEN_DUPLICATE_FIELD")
        result[key] = value
    return result


def verify_identity(
    token: str,
    jwks: dict[str, Any],
    *,
    client_id: str,
    nonce: str | None,
    now: float,
) -> str:
    try:
        pieces = token.split(".")
        if len(pieces) != 3:
            raise ChatGptTransportError("ID_TOKEN_INVALID")
        header = json.loads(decode_base64(pieces[0]), object_pairs_hook=_object)
        claims = json.loads(decode_base64(pieces[1]), object_pairs_hook=_object)
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise ChatGptTransportError("ID_TOKEN_INVALID")
        if (
            header.get("alg") != "RS256"
            or header.get("crit")
            or not isinstance(header.get("kid"), str)
        ):
            raise ChatGptTransportError("ID_TOKEN_ALGORITHM_REJECTED")
        keys = jwks.get("keys")
        if not isinstance(keys, list):
            raise ChatGptTransportError("JWKS_INVALID")
        matching = [k for k in keys if isinstance(k, dict) and k.get("kid") == header["kid"]]
        if len(matching) != 1:
            raise ChatGptTransportError("JWKS_KEY_UNRESOLVED")
        key = matching[0]
        if (
            key.get("kty") != "RSA"
            or key.get("use", "sig") != "sig"
            or key.get("alg", "RS256") != "RS256"
        ):
            raise ChatGptTransportError("JWKS_KEY_REJECTED")
        public = rsa.RSAPublicNumbers(
            int.from_bytes(decode_base64(key["e"]), "big"),
            int.from_bytes(decode_base64(key["n"]), "big"),
        ).public_key()
        if public.key_size < 2048:
            raise ChatGptTransportError("JWKS_KEY_REJECTED")
        public.verify(
            decode_base64(pieces[2]),
            (pieces[0] + "." + pieces[1]).encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        audience = claims.get("aud")
        if audience != client_id and not (
            isinstance(audience, list) and client_id in audience and claims.get("azp") == client_id
        ):
            raise ChatGptTransportError("ID_TOKEN_AUDIENCE_REJECTED")
        exp, issued = claims.get("exp"), claims.get("iat")
        if (
            claims.get("iss") != "https://auth.openai.com"
            or not isinstance(exp, int | float)
            or isinstance(exp, bool)
            or not math.isfinite(exp)
            or exp <= now
            or not isinstance(issued, int | float)
            or isinstance(issued, bool)
            or not math.isfinite(issued)
            or issued > now + 30
            or (
                "nbf" in claims
                and (
                    not isinstance(claims["nbf"], int | float)
                    or isinstance(claims["nbf"], bool)
                    or not math.isfinite(claims["nbf"])
                    or claims["nbf"] > now
                )
            )
            or (nonce is not None and claims.get("nonce") != nonce)
            or not isinstance(claims.get("sub"), str)
            or not claims["sub"]
        ):
            raise ChatGptTransportError("ID_TOKEN_CLAIMS_REJECTED")
        return str(claims["sub"])
    except (ValueError, TypeError, KeyError, InvalidSignature):
        raise ChatGptTransportError("ID_TOKEN_INVALID") from None
