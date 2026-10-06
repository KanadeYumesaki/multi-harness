"""Synthetic signed OAuth tokens and HTTPS/SSE envelopes. Never a live provider."""

from __future__ import annotations

import base64
import io
import json
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from harness.infrastructure.provider.chatgpt_auth import ChatGptAuth
from harness.infrastructure.provider.chatgpt_http import OpenAiHttps

NOW = 1791000000.0


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class FixtureConnection:
    sock = None

    def close(self) -> None:
        return


class FixtureResponse(io.BytesIO):
    def __init__(self, payload: bytes, status: int = 200) -> None:
        super().__init__(payload)
        self.status = status

    def getheader(self, name: str, default: str = "") -> str:
        return "text/event-stream" if name == "Content-Type" else default


def event(value: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(value).encode() + b"\n\n"


def completed(text: str) -> bytes:
    return event(
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ],
            },
        }
    )


class FixtureTransport(OpenAiHttps):
    def __init__(self) -> None:
        super().__init__()
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = self.key.public_key().public_numbers()
        self.jwks = {
            "keys": [
                {
                    "kid": "fixture",
                    "kty": "RSA",
                    "alg": "RS256",
                    "use": "sig",
                    "n": b64(numbers.n.to_bytes(256, "big")),
                    "e": b64(numbers.e.to_bytes(3, "big")),
                }
            ]
        }
        self.auth: ChatGptAuth | None = None
        self.claim_overrides: dict[str, Any] = {}
        self.scope = "openid resource.invoke chatgpt.tokens.use.direct offline_access"
        self.models = [
            {"slug": "fixture-model", "display_name": "合成モデル", "visibility": "list"}
        ]
        self.calls: list[tuple[str, str, bytes | None]] = []
        self.stream = completed(json.dumps({"replacement_text": "# 合成コメント\nvalue = 1\n"}))
        self.http_status = 200
        self.on_inference: Any = None

    def signed(self, claims: dict[str, Any], header: dict[str, Any] | None = None) -> str:
        head = b64(json.dumps(header or {"kid": "fixture", "alg": "RS256"}).encode())
        body = b64(json.dumps(claims).encode())
        signature = self.key.sign((head + "." + body).encode(), padding.PKCS1v15(), hashes.SHA256())
        return head + "." + body + "." + b64(signature)

    def json(
        self, method: str, url: str, *, headers: Any = None, body: bytes | None = None
    ) -> dict[str, Any]:
        self.calls.append((method, url, body))
        if url.endswith("/.well-known/openid-configuration"):
            return {
                "issuer": "https://auth.openai.com",
                "jwks_uri": "https://auth.openai.com/fixture-jwks",
                "revocation_endpoint": "https://auth.openai.com/fixture-revoke",
            }
        if url.endswith("/fixture-jwks"):
            return self.jwks
        if url.endswith("/models"):
            return {"models": self.models}
        if url.endswith("/oauth/token"):
            params = parse_qs(body.decode() if body else "")
            assert self.auth is not None and self.auth._attempt is not None
            claims = {
                "iss": "https://auth.openai.com",
                "aud": params["client_id"][0],
                "sub": "synthetic-account",
                "iat": NOW,
                "exp": NOW + 7200,
                "nonce": self.auth._attempt.nonce,
            }
            claims.update(self.claim_overrides)
            return {
                "access_token": "fixture-access-only",
                "refresh_token": "fixture-refresh-only",
                "id_token": self.signed(claims),
                "scope": self.scope,
                "token_type": "Bearer",
                "expires_in": 3600,
                "earliest_refresh_at": NOW,
            }
        raise AssertionError("unexpected synthetic URL")

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Any = None,
        body: bytes | None = None,
        timeout: float = 30,
        on_connection: Any = None,
    ) -> Any:
        self.calls.append((method, url, body))
        connection = FixtureConnection()
        if on_connection is not None:
            on_connection(connection)
        if url.endswith("/fixture-revoke"):
            return connection, FixtureResponse(b"")
        assert url == "https://api.openai.com/v1/responses"
        if self.on_inference:
            self.on_inference()
        return connection, FixtureResponse(self.stream, self.http_status)


def setup_auth() -> tuple[ChatGptAuth, FixtureTransport, list[Any]]:
    transport = FixtureTransport()
    auth = ChatGptAuth(transport=transport, now=lambda: NOW)
    transport.auth = auth
    stored: list[Any] = []
    auth.remember = lambda metadata: stored.append(dict(metadata))
    auth.bind({"host_id": "urn:uuid:11111111-1111-4111-8111-111111111111"})
    return auth, transport, stored


def callback(auth: ChatGptAuth, *, error: str | None = None) -> str:
    assert auth._attempt is not None
    query = {
        "state": auth._attempt.state,
        "client_id": "fixture-client",
        "code": "fixture-code-only",
    }
    if error:
        query["error"] = error
    return "/auth/callback?" + urlencode(query)


def login(auth: ChatGptAuth) -> None:
    result = auth.start()
    assert urlsplit(result["authorization_url"]).hostname == "auth.openai.com"
    auth.accept_callback(callback(auth))
