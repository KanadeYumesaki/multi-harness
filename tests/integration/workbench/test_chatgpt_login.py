"""OAuth contract tests: signed fixtures and a real loopback callback, no live inference."""

from __future__ import annotations

import http.client
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from chatgpt_fixtures import NOW, callback, login, setup_auth

from harness.infrastructure.provider.chatgpt_http import ChatGptTransportError, OpenAiHttps
from harness.infrastructure.provider.chatgpt_identity import verify_identity

pytestmark = pytest.mark.integration


def test_authorization_code_pkce_and_issued_client() -> None:
    auth, transport, stored = setup_auth()
    try:
        result = auth.start()
        params = parse_qs(urlsplit(result["authorization_url"]).query)
        assert params["client_id"] == ["dynamic_agent_client"]
        assert params["resource"] == ["https://api.openai.com/v1"]
        assert params["code_challenge_method"] == ["S256"]
        assert "chatgpt.tokens.use.direct" in params["scope"][0]
        redirect = urlsplit(params["redirect_uri"][0])
        assert redirect.scheme == "http" and redirect.hostname == "127.0.0.1"
        connection = http.client.HTTPConnection("127.0.0.1", redirect.port, timeout=10)
        try:
            connection.request("GET", redirect.path + "?" + callback(auth).split("?", 1)[1])
            response = connection.getresponse()
            assert response.status == 200
            response.read()
        finally:
            connection.close()
        assert auth.status()["connected"] is True
        exchanged = next(body for _, url, body in transport.calls if url.endswith("/oauth/token"))
        exchange = parse_qs(exchanged.decode())
        assert exchange["client_id"] == ["fixture-client"]
        assert exchange["redirect_uri"] == params["redirect_uri"]
        assert set(stored[0]) == {"host_id", "client_id", "subject_hash"}
        public = json.dumps(auth.status()) + json.dumps(stored)
        for secret in ("fixture-access-only", "fixture-refresh-only", "fixture-code-only"):
            assert secret not in public
    finally:
        auth.close()


@pytest.mark.parametrize(
    "change",
    [
        {"iss": "https://attacker.invalid"},
        {"aud": "wrong-client"},
        {"nonce": "wrong-nonce"},
        {"exp": NOW},
        {"iat": NOW + 100},
        {"sub": ""},
        {"nbf": NOW + 100},
    ],
)
def test_signed_but_invalid_identity_fails_closed(change: dict[str, object]) -> None:
    auth, transport, stored = setup_auth()
    transport.claim_overrides = change
    try:
        auth.start()
        with pytest.raises(ChatGptTransportError):
            auth.accept_callback(callback(auth))
        assert not auth.status()["connected"]
        assert not stored
        assert not any(url.endswith("/models") for _, url, _ in transport.calls)
    finally:
        auth.close()


def test_unsigned_or_wrong_signature_is_rejected() -> None:
    auth, transport, _ = setup_auth()
    claims = {
        "iss": "https://auth.openai.com",
        "aud": "fixture-client",
        "sub": "fixture",
        "iat": NOW,
        "exp": NOW + 100,
        "nonce": "fixture",
    }
    token = transport.signed(claims)
    wrong = token.rsplit(".", 1)[0] + ".AAAA"
    with pytest.raises(ChatGptTransportError):
        verify_identity(wrong, transport.jwks, client_id="fixture-client", nonce="fixture", now=NOW)
    none = transport.signed(claims, {"kid": "fixture", "alg": "none"})
    with pytest.raises(ChatGptTransportError):
        verify_identity(none, transport.jwks, client_id="fixture-client", nonce="fixture", now=NOW)
    auth.close()


@pytest.mark.parametrize("mode", ["state", "duplicate", "cancel", "client", "query", "expired"])
def test_bad_callback_never_exchanges_tokens(mode: str) -> None:
    auth, transport, _ = setup_auth()
    try:
        auth.start()
        path = callback(auth)
        if mode == "state":
            path = path.replace("state=", "state=wrong")
        elif mode == "duplicate":
            path += "&code=second-code"
        elif mode == "cancel":
            path = callback(auth, error="access_denied")
        elif mode == "client":
            path = path.replace("fixture-client", "dynamic_agent_client")
        elif mode == "query":
            path += "&bad-field"
        else:
            auth.monotonic = lambda: 10**20
        with pytest.raises(ChatGptTransportError):
            auth.accept_callback(path)
        assert not any(url.endswith("/oauth/token") for _, url, _ in transport.calls)
    finally:
        auth.close()


def test_callback_replay_is_refused_and_refresh_is_serialized() -> None:
    auth, transport, _ = setup_auth()
    try:
        login(auth)
        used = callback(auth)
        with pytest.raises(ChatGptTransportError):
            auth.accept_callback(used)
        reference = auth.credential_ref()
        auth.now = lambda: NOW + 3550
        assert auth.access(reference) == "fixture-access-only"
        refreshes = [
            parse_qs(body.decode())
            for _, url, body in transport.calls
            if url.endswith("/oauth/token") and b"refresh_token" in (body or b"")
        ]
        assert len(refreshes) == 1
        assert "scope" not in refreshes[0]
        assert refreshes[0]["client_id"] == ["fixture-client"]
        assert auth.access(reference) == "fixture-access-only"
        assert auth.status()["connected"]
        auth.disconnect()
        with pytest.raises(ChatGptTransportError):
            auth.access(reference)
        assert auth.status()["remote_revocation"] == "CONFIRMED"
    finally:
        auth.close()


def test_missing_plan_scope_cannot_enable_generation() -> None:
    auth, transport, _ = setup_auth()
    transport.scope = "openid profile email"
    try:
        auth.start()
        with pytest.raises(ChatGptTransportError):
            auth.accept_callback(callback(auth))
        assert auth.status()["connected"] is False
    finally:
        auth.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://api.openai.com/v1/responses",
        "https://attacker.invalid/",
        "https://api.openai.com:444/",
        "https://token@api.openai.com/",
        "https://auth.openai.com.evil.invalid/",
        "https://127.0.0.1/",
    ],
)
def test_endpoint_allowlist_is_checked_before_network(url: str) -> None:
    with pytest.raises(ChatGptTransportError):
        OpenAiHttps().request("POST", url)


def test_registered_client_is_reused_and_another_identity_cannot_overwrite_it() -> None:
    auth, transport, stored = setup_auth()
    try:
        login(auth)
        registered = dict(stored[-1])
        auth.disconnect()
        result = auth.start()
        parameters = parse_qs(urlsplit(result["authorization_url"]).query)
        assert parameters["client_id"] == ["fixture-client"]
        assert "agent_name_hint" not in parameters
        transport.claim_overrides["sub"] = "another-synthetic-account"
        with pytest.raises(ChatGptTransportError):
            auth.accept_callback(callback(auth))
        assert stored == [registered]
        assert auth.status()["connected"] is False
        result = auth.start(new_account=True)
        assert parse_qs(urlsplit(result["authorization_url"]).query)["client_id"] == [
            "dynamic_agent_client"
        ]
        auth.accept_callback(callback(auth).replace("fixture-client", "another-fixture-client"))
        assert stored[-1]["host_id"] == registered["host_id"]
        assert stored[-1]["subject_hash"] != registered["subject_hash"]
        assert stored[-1]["client_id"] == "another-fixture-client"
    finally:
        auth.close()


def test_two_refresh_requests_use_one_rotating_exchange() -> None:
    import threading

    auth, transport, _ = setup_auth()
    try:
        login(auth)
        reference = auth.credential_ref()
        auth.now = lambda: NOW + 3550
        barrier = threading.Barrier(2)
        results: list[str] = []

        def access() -> None:
            barrier.wait(timeout=10)
            results.append(auth.access(reference))

        workers = [threading.Thread(target=access) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
        assert results == ["fixture-access-only", "fixture-access-only"]
        exchanges = [
            body
            for _, url, body in transport.calls
            if url.endswith("/oauth/token") and b"grant_type=refresh_token" in (body or b"")
        ]
        assert len(exchanges) == 1
    finally:
        auth.close()


def test_untrusted_jwks_discovery_cannot_redirect_tokens_or_fetch_private_hosts() -> None:
    auth, transport, _ = setup_auth()
    original = transport.json

    def discovery(method: str, url: str, **kwargs: object) -> dict[str, object]:
        if url.endswith("/.well-known/openid-configuration"):
            return {
                "issuer": "https://auth.openai.com",
                "jwks_uri": "http://169.254.169.254/",
                "revocation_endpoint": "https://attacker.invalid/revoke",
            }
        return original(method, url, **kwargs)

    transport.json = discovery
    try:
        auth.start()
        with pytest.raises(ChatGptTransportError):
            auth.accept_callback(callback(auth))
        assert not any(
            "169.254" in url or "attacker.invalid" in url for _, url, _ in transport.calls
        )
        assert auth.status()["connected"] is False
    finally:
        auth.close()


def test_https_circuit_stops_repeated_network_attempts_and_keeps_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ssl

    from harness.infrastructure.provider import chatgpt_http

    clock = [1.0]
    attempts: list[dict[str, object]] = []

    class Connection:
        def __init__(self, host: str, **options: object) -> None:
            attempts.append({"host": host, **options})

        def request(self, *args: object, **kwargs: object) -> None:
            raise OSError("synthetic-secret-must-not-escape")

        def close(self) -> None:
            return

    monkeypatch.setattr(chatgpt_http.http.client, "HTTPSConnection", Connection)
    transport = OpenAiHttps(monotonic=lambda: clock[0])
    for _ in range(3):
        with pytest.raises(ChatGptTransportError, match="NETWORK_UNCONFIRMED") as error:
            transport.request("GET", "https://auth.openai.com/fixture")
        assert "synthetic-secret" not in str(error.value)
    with pytest.raises(ChatGptTransportError, match="REMOTE_CIRCUIT_OPEN"):
        transport.request("GET", "https://auth.openai.com/fixture")
    assert len(attempts) == 3
    assert all(row["timeout"] == 30 for row in attempts)
    assert all(row["context"].verify_mode == ssl.CERT_REQUIRED for row in attempts)
    assert all(row["context"].check_hostname for row in attempts)
    clock[0] = 32.0
    with pytest.raises(ChatGptTransportError, match="NETWORK_UNCONFIRMED"):
        transport.request("GET", "https://auth.openai.com/fixture")
    assert len(attempts) == 4


@pytest.mark.parametrize("body", [b'{"scope":"a","scope":"b"}', b'{"expires_in":NaN}', b"[]"])
def test_https_auth_json_is_unambiguous_and_does_not_echo_payload(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    from chatgpt_fixtures import FixtureConnection, FixtureResponse

    transport = OpenAiHttps()
    response = FixtureResponse(body)
    monkeypatch.setattr(
        transport, "request", lambda *args, **kwargs: (FixtureConnection(), response)
    )
    with pytest.raises(ChatGptTransportError) as error:
        transport.json("POST", "https://auth.openai.com/fixture")
    assert body.decode() not in str(error.value)
    assert response.tell() <= 1024 * 1024 + 1
