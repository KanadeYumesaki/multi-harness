"""Fixed-host HTTPS, bounded reads, no redirects and no automatic inference retry."""

from __future__ import annotations

import http.client
import json
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from harness.domain.errors import ErrorCode, HarnessError


class ChatGptTransportError(HarnessError):
    """Only a fixed classification leaves the credential boundary."""

    def __init__(self, classification: str) -> None:
        super().__init__(ErrorCode.RUNTIME_SPEC_MISMATCH, classification)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("ambiguous JSON")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError("non-finite JSON")


class OpenAiHttps:
    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}
        self._blocked_until: dict[str, float] = {}

    def _admit(self, host: str) -> None:
        with self._lock:
            if self._monotonic() < self._blocked_until.get(host, 0.0):
                raise ChatGptTransportError("REMOTE_CIRCUIT_OPEN")

    def _record_result(self, host: str, *, failed: bool) -> None:
        with self._lock:
            if failed:
                failures = self._failures.get(host, 0) + 1
                self._failures[host] = failures
                if failures >= 3:
                    self._blocked_until[host] = self._monotonic() + 30
            else:
                self._failures[host] = 0
                self._blocked_until.pop(host, None)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 30,
        on_connection: Callable[[http.client.HTTPSConnection], None] | None = None,
    ) -> tuple[http.client.HTTPSConnection, http.client.HTTPResponse]:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in ("auth.openai.com", "api.openai.com")
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ChatGptTransportError("ENDPOINT_REJECTED")
        host = parsed.hostname
        if host is None:
            raise ChatGptTransportError("ENDPOINT_REJECTED")
        self._admit(host)
        connection = http.client.HTTPSConnection(
            host, timeout=timeout, context=ssl.create_default_context()
        )
        try:
            if on_connection is not None:
                on_connection(connection)
            connection.request(
                method,
                parsed.path + ("?" + parsed.query if parsed.query else ""),
                body=body,
                headers=dict(headers or {}),
            )
            response = connection.getresponse()
            self._record_result(host, failed=response.status >= 500 or response.status == 429)
            return connection, response
        except (OSError, http.client.HTTPException):
            connection.close()
            self._record_result(host, failed=True)
            raise ChatGptTransportError("NETWORK_UNCONFIRMED") from None

    def json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> dict[str, Any]:
        connection, response = self.request(method, url, headers=headers, body=body)
        try:
            data = response.read(1024 * 1024 + 1)
            if response.status != 200 or len(data) > 1024 * 1024:
                raise ChatGptTransportError("AUTH_HTTP_REJECTED")
            value = json.loads(
                data, object_pairs_hook=_unique_object, parse_constant=_reject_constant
            )
            if not isinstance(value, dict):
                raise ChatGptTransportError("AUTH_BODY_REJECTED")
            return value
        except (OSError, http.client.HTTPException, ValueError):
            raise ChatGptTransportError("AUTH_RESPONSE_UNCONFIRMED") from None
        finally:
            connection.close()
