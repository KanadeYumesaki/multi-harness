"""Loopback OAuth/PKCE with verified identity and RAM-only credentials."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from harness.infrastructure.provider.chatgpt_http import ChatGptTransportError, OpenAiHttps
from harness.infrastructure.provider.chatgpt_identity import verify_identity
from harness.ports.chatgpt import SecretRef

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN_ENDPOINT = ISSUER + "/api/accounts/oauth/token"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
USAGE_URL = "https://chatgpt.com/settings/usage"


@dataclass(repr=False)
class _Attempt:
    state: str
    nonce: str
    verifier: str
    redirect_uri: str
    client_id: str
    deadline: float
    used: bool = False


@dataclass(repr=False)
class _Credentials:
    access: str = field(repr=False)
    refresh: str = field(repr=False)
    client_id: str
    subject_hash: str
    reference: SecretRef
    expires_at: float
    earliest_refresh: float


def _client_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,256}", value):
        raise ChatGptTransportError("OAUTH_CLIENT_INVALID")
    if value == "dynamic_agent_client":
        raise ChatGptTransportError("OAUTH_ISSUED_CLIENT_REQUIRED")
    return value


class ChatGptAuth:
    def __init__(
        self,
        *,
        transport: OpenAiHttps | None = None,
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.transport = transport or OpenAiHttps()
        self.now, self.monotonic = now, monotonic
        self.remember: Callable[[Mapping[str, str]], None] | None = None
        self._metadata: dict[str, str] = {}
        self._credentials: _Credentials | None = None
        self._models: tuple[dict[str, str], ...] = ()
        self._attempt: _Attempt | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._state = "DISCONNECTED"
        self._reason: str | None = None
        self._remote_revocation = "NOT_REQUESTED"

    def bind(self, metadata: Mapping[str, str]) -> None:
        host = metadata.get("host_id", "")
        if not re.fullmatch(r"urn:uuid:[0-9a-f-]{36}", host):
            raise ChatGptTransportError("HOST_REGISTRATION_INVALID")
        if "client_id" in metadata:
            _client_id(metadata["client_id"])
        self._metadata = dict(metadata)

    def status(self) -> dict[str, Any]:
        with self._lock:
            if (
                self._attempt
                and not self._attempt.used
                and self.monotonic() >= self._attempt.deadline
            ):
                self._attempt.used = True
                self._state, self._reason = "LOGIN_EXPIRED", "OAUTH_ATTEMPT_EXPIRED"
            return {
                "state": self._state,
                "reason": self._reason,
                "connected": self._credentials is not None and self._state == "CONNECTED",
                "models": list(self._models),
                "credential_storage": "PROCESS_MEMORY_ONLY",
                "restart_requires_login": True,
                "usage_url": USAGE_URL,
                "quota_state": "UNVERIFIED",
                "remote_revocation": self._remote_revocation,
                "transport": "HTTPS_RESPONSES_SSE",
            }

    def _stop_listener(self) -> None:
        server, thread = self._server, self._thread
        self._server, self._thread = None, None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def start(self, *, new_account: bool = False) -> dict[str, Any]:
        self._stop_listener()
        with self._lock:
            if self._credentials is not None:
                raise ChatGptTransportError("DISCONNECT_BEFORE_ACCOUNT_CHANGE")
            auth = self

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self) -> None:
                    try:
                        if len(self.path) > 16384:
                            raise ChatGptTransportError("CALLBACK_TOO_LARGE")
                        if self.headers.get("Host") != f"127.0.0.1:{server.server_port}":
                            raise ChatGptTransportError("CALLBACK_HOST_REJECTED")
                        auth.accept_callback(self.path)
                        status, message = 200, "Login complete. Return to the Harness window."
                    except ChatGptTransportError:
                        status, message = 400, "Login was not completed. Check the Harness window."
                    body = message.encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("Content-Security-Policy", "default-src 'none'")
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, format: str, *args: Any) -> None:
                    # Callback code/state/query must never enter access logs.
                    return

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.daemon_threads = True
            self._server = server
            redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
            client = (
                "dynamic_agent_client"
                if new_account
                else self._metadata.get("client_id", "dynamic_agent_client")
            )
            attempt = _Attempt(
                secrets.token_urlsafe(32),
                secrets.token_urlsafe(32),
                secrets.token_urlsafe(48),
                redirect,
                client,
                self.monotonic() + 600,
            )
            self._attempt = attempt
            values = {
                "client_id": client,
                "ext_agent_host_id": self._metadata["host_id"],
                "redirect_uri": redirect,
                "response_type": "code",
                "scope": SCOPE,
                "resource": RESOURCE,
                "state": attempt.state,
                "nonce": attempt.nonce,
                "code_challenge_method": "S256",
                "code_challenge": base64.urlsafe_b64encode(
                    hashlib.sha256(attempt.verifier.encode("ascii")).digest()
                )
                .decode("ascii")
                .rstrip("="),
            }
            if client == "dynamic_agent_client":
                values["agent_name_hint"] = "Multi Harness"
            self._state, self._reason = "LOGIN_PENDING", None
            self._thread = threading.Thread(target=server.serve_forever, daemon=True)
            self._thread.start()
            return {
                "authorization_url": AUTHORIZE + "?" + urlencode(values),
                "expires_in_seconds": 600,
                "state": self._state,
            }

    def _discovery(self) -> dict[str, Any]:
        discovery = self.transport.json("GET", ISSUER + "/.well-known/openid-configuration")
        if discovery.get("issuer") != ISSUER:
            raise ChatGptTransportError("DISCOVERY_ISSUER_REJECTED")
        for key in ("jwks_uri", "revocation_endpoint"):
            value = discovery.get(key)
            if not isinstance(value, str):
                raise ChatGptTransportError("DISCOVERY_ENDPOINT_REJECTED")
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "auth.openai.com"
                or parsed.port not in (None, 443)
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or not parsed.path.startswith("/")
            ):
                raise ChatGptTransportError("DISCOVERY_ENDPOINT_REJECTED")
        return discovery

    def _token(self, values: Mapping[str, str]) -> dict[str, Any]:
        return self.transport.json(
            "POST",
            TOKEN_ENDPOINT,
            body=urlencode(values).encode("ascii"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    def _verified_credentials(
        self,
        result: dict[str, Any],
        *,
        client_id: str,
        nonce: str | None,
        reference: SecretRef | None = None,
    ) -> _Credentials:
        for name in ("access_token", "refresh_token", "id_token", "scope"):
            if (
                not isinstance(result.get(name), str)
                or not result[name]
                or len(result[name]) > 65536
            ):
                raise ChatGptTransportError("TOKEN_RESPONSE_INVALID")
        token_type = result.get("token_type")
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            raise ChatGptTransportError("TOKEN_TYPE_REJECTED")
        if not {"openid", "resource.invoke", "chatgpt.tokens.use.direct"} <= set(
            result["scope"].split()
        ):
            raise ChatGptTransportError("CHATGPT_PLAN_SCOPE_MISSING")
        ttl = result.get("expires_in")
        if type(ttl) is not int or not 1 <= ttl <= 86400:
            raise ChatGptTransportError("TOKEN_EXPIRY_INVALID")
        earliest = result.get("earliest_refresh_at")
        try:
            if isinstance(earliest, str):
                instant = datetime.fromisoformat(earliest.replace("Z", "+00:00"))
                if instant.tzinfo is None:
                    raise ChatGptTransportError("TOKEN_REFRESH_TIME_INVALID")
                earliest = instant.timestamp()
            if (
                not isinstance(earliest, int | float)
                or isinstance(earliest, bool)
                or not 0 <= earliest <= self.now() + ttl
            ):
                raise ChatGptTransportError("TOKEN_REFRESH_TIME_INVALID")
        except ValueError:
            raise ChatGptTransportError("TOKEN_REFRESH_TIME_INVALID") from None
        discovery = self._discovery()
        jwks = self.transport.json("GET", str(discovery["jwks_uri"]))
        subject = verify_identity(
            result["id_token"], jwks, client_id=client_id, nonce=nonce, now=self.now()
        )
        subject_hash = hashlib.sha256(subject.encode("utf-8")).hexdigest()
        return _Credentials(
            result["access_token"],
            result["refresh_token"],
            client_id,
            subject_hash,
            reference or SecretRef(hashlib.sha256(result["access_token"].encode()).hexdigest()),
            self.now() + ttl,
            float(earliest),
        )

    def accept_callback(self, path: str) -> None:
        with self._lock:
            attempt = self._attempt
            parsed = urlsplit(path)
            try:
                query = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True)
            except ValueError:
                raise ChatGptTransportError("CALLBACK_REJECTED") from None
            if (
                parsed.path != "/auth/callback"
                or parsed.fragment
                or attempt is None
                or attempt.used
                or self.monotonic() >= attempt.deadline
                or any(len(v) != 1 for v in query.values())
                or not hmac.compare_digest(
                    query.get("state", [""])[0].encode("utf-8"), attempt.state.encode("ascii")
                )
            ):
                raise ChatGptTransportError("CALLBACK_REJECTED")
            attempt.used = True
            try:
                if "error" in query:
                    raise ChatGptTransportError("LOGIN_CANCELLED")
                client = _client_id(query.get("client_id", [""])[0])
                if attempt.client_id != "dynamic_agent_client" and client != attempt.client_id:
                    raise ChatGptTransportError("CALLBACK_CLIENT_MISMATCH")
                code = query.get("code", [""])[0]
                if not code or len(code) > 8192:
                    raise ChatGptTransportError("CALLBACK_CODE_INVALID")
                result = self._token(
                    {
                        "grant_type": "authorization_code",
                        "client_id": client,
                        "code": code,
                        "code_verifier": attempt.verifier,
                        "redirect_uri": attempt.redirect_uri,
                        "resource": RESOURCE,
                    }
                )
                credentials = self._verified_credentials(
                    result, client_id=client, nonce=attempt.nonce
                )
                if attempt.client_id != "dynamic_agent_client" and (
                    credentials.subject_hash != self._metadata.get("subject_hash")
                ):
                    raise ChatGptTransportError("REGISTERED_IDENTITY_MISMATCH")
                metadata = {
                    "host_id": self._metadata["host_id"],
                    "client_id": client,
                    "subject_hash": credentials.subject_hash,
                }
                if self.remember is None:
                    raise ChatGptTransportError("REGISTRATION_STORE_UNAVAILABLE")
                self.remember(metadata)
                self._metadata = metadata
                self._credentials = credentials
                self._load_models(credentials.access)
                self._state, self._reason = "CONNECTED", None
            except ChatGptTransportError as error:
                self._credentials, self._models = None, ()
                self._state, self._reason = "LOGIN_FAILED", str(error)
                raise
            except (OSError, ValueError):
                self._credentials, self._models = None, ()
                self._state, self._reason = "LOGIN_FAILED", "REGISTRATION_UNCONFIRMED"
                raise ChatGptTransportError("REGISTRATION_UNCONFIRMED") from None

    def _load_models(self, token: str) -> None:
        catalog = self.transport.json(
            "GET", RESOURCE + "/models", headers={"Authorization": "Bearer " + token}
        )
        models = catalog.get("models")
        if not isinstance(models, list) or len(models) > 512:
            raise ChatGptTransportError("MODEL_CATALOG_INVALID")
        choices: list[dict[str, str]] = []
        for model in models:
            if not isinstance(model, dict):
                raise ChatGptTransportError("MODEL_CATALOG_INVALID")
            if model.get("visibility") != "list":
                continue
            slug, label = model.get("slug"), model.get("display_name")
            if (
                not isinstance(slug, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,95}", slug)
                or not isinstance(label, str)
                or not 1 <= len(label) <= 160
                or any(c["slug"] == slug for c in choices)
            ):
                raise ChatGptTransportError("MODEL_CATALOG_INVALID")
            choices.append({"slug": slug, "display_name": label})
        if not choices:
            raise ChatGptTransportError("NO_ELIGIBLE_CHATGPT_MODELS")
        self._models = tuple(choices)

    def credential_ref(self) -> SecretRef:
        with self._lock:
            if self._credentials is None or self._state != "CONNECTED":
                raise ChatGptTransportError("CHATGPT_LOGIN_REQUIRED")
            return self._credentials.reference

    def access(self, reference: SecretRef) -> str:
        with self._lock:
            credentials = self._credentials
            if credentials is None or credentials.reference != reference:
                raise ChatGptTransportError("CHATGPT_LOGIN_CHANGED")
            if self.now() >= credentials.expires_at - 60:
                if self.now() < credentials.earliest_refresh:
                    raise ChatGptTransportError("TOKEN_REFRESH_NOT_YET_ALLOWED")
                try:
                    result = self._token(
                        {
                            "grant_type": "refresh_token",
                            "client_id": credentials.client_id,
                            "refresh_token": credentials.refresh,
                            "resource": RESOURCE,
                        }
                    )
                    updated = self._verified_credentials(
                        result,
                        client_id=credentials.client_id,
                        nonce=None,
                        reference=reference,
                    )
                    if updated.subject_hash != credentials.subject_hash:
                        raise ChatGptTransportError("REFRESH_IDENTITY_CHANGED")
                    self._credentials = credentials = updated
                except ChatGptTransportError:
                    self._credentials, self._models = None, ()
                    self._state, self._reason = "LOGIN_REQUIRED", "TOKEN_REFRESH_FAILED"
                    raise
            return credentials.access

    def disconnect(self) -> dict[str, Any]:
        self._stop_listener()
        with self._lock:
            credentials = self._credentials
            self._credentials, self._models, self._attempt = None, (), None
            self._state, self._reason = "DISCONNECTED", None
            self._remote_revocation = "NOT_NEEDED"
            if credentials is not None:
                self._remote_revocation = "UNCONFIRMED"
                try:
                    discovery = self._discovery()
                    connection, response = self.transport.request(
                        "POST",
                        str(discovery["revocation_endpoint"]),
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        body=urlencode(
                            {
                                "token": credentials.refresh,
                                "token_type_hint": "refresh_token",
                                "client_id": credentials.client_id,
                            }
                        ).encode("ascii"),
                    )
                    try:
                        if response.status == 200:
                            self._remote_revocation = "CONFIRMED"
                    finally:
                        connection.close()
                except ChatGptTransportError:
                    self._reason = "REVOKE_IN_CHATGPT_SETTINGS_IF_NEEDED"
            return self.status()

    def close(self) -> None:
        self._stop_listener()
        with self._lock:
            self._credentials, self._models, self._attempt = None, (), None
            self._state = "DISCONNECTED"
