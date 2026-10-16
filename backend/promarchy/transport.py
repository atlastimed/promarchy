"""A small HTTPS client on the standard library.

The daemon lives as long as your desktop session, so it avoids heavier HTTP
stacks: this keeps its resident memory at roughly a third of what
``requests`` costs. Connections are reused within a sync burst, responses
are gzip-compressed on the wire, and every socket operation has a timeout.
"""

from __future__ import annotations

import http.client
import json as jsonlib
import os
import ssl
import time
import zlib
from typing import Any
from urllib.parse import urlencode, urlsplit

MAX_BODY = 16 * 1024 * 1024
# Proton closes idle keep-alive connections; reconnecting before that point is
# cheaper than discovering it mid-request.
IDLE_REUSE_SECONDS = 20
IDEMPOTENT = {"GET", "HEAD"}


class TransportError(OSError):
    """The request did not complete (DNS, TLS, timeout, dropped connection)."""


class Response:
    def __init__(self, status: int, headers: dict[str, str], content: bytes) -> None:
        self.status_code = status
        self.headers = headers
        self.content = content

    def json(self) -> Any:
        return jsonlib.loads(self.content)


class HttpTransport:
    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout
        self.headers: dict[str, str] = {}
        self._context = ssl.create_default_context()
        self._conn: http.client.HTTPSConnection | None = None
        self._origin: tuple[str, int] | None = None
        self._last_used = 0.0

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
        self._conn = None

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Any = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        timeout: Any = None,
        max_bytes: int = MAX_BODY,
    ) -> Response:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError(f"refusing non-HTTPS URL: {url}")
        path = parts.path or "/"
        if params:
            path += "?" + urlencode(params, doseq=True)
        body = jsonlib.dumps(json).encode() if json is not None else None
        merged = {**self.headers, "Accept-Encoding": "gzip", **(headers or {})}
        if body is not None:
            merged["Content-Type"] = "application/json"

        origin = (parts.hostname, parts.port or 443)
        reused = self._conn is not None and self._origin == origin
        attempts = 2 if method.upper() in IDEMPOTENT else 1
        for attempt in range(attempts):
            conn = self._connection(origin)
            try:
                try:
                    conn.request(method, path, body=body, headers=merged)
                except ValueError:
                    # http.client quotes the offending header (maybe a token).
                    raise TransportError("invalid request header") from None
                resp = conn.getresponse()
                content = resp.read(max_bytes + 1)
                if len(content) > max_bytes:
                    raise TransportError("response too large")
                if resp.getheader("Content-Encoding", "").lower() == "gzip":
                    content = gunzip_bounded(content, max_bytes)
                header_map = {k.lower(): v for k, v in resp.getheaders()}
                if resp.will_close:
                    self.close()
                self._last_used = time.monotonic()
                return Response(resp.status, header_map, content)
            except TransportError:
                self.close()  # our own verdict on the response: not worth a retry
                raise
            except (http.client.HTTPException, OSError) as exc:
                self.close()
                # A reused connection may have been dropped by the server
                # while idle; one retry on a fresh connection is safe for GETs.
                if reused and attempt + 1 < attempts:
                    reused = False
                    continue
                raise TransportError(_describe(exc)) from exc
        raise TransportError("request failed")  # pragma: no cover - loop always returns or raises

    def _connection(self, origin: tuple[str, int]) -> http.client.HTTPSConnection:
        stale = time.monotonic() - self._last_used > IDLE_REUSE_SECONDS
        if self._conn is not None and (self._origin != origin or stale):
            self.close()
        if self._conn is None:
            host, port = origin
            proxy = _proxy_for(host)
            if proxy:
                conn = http.client.HTTPSConnection(
                    proxy[0], proxy[1], context=self._context, timeout=self.timeout
                )
                conn.set_tunnel(host, port)
            else:
                conn = http.client.HTTPSConnection(host, port, context=self._context, timeout=self.timeout)
            self._conn = conn
            self._origin = origin
        return self._conn


def gunzip_bounded(data: bytes, limit: int) -> bytes:
    """Decompress gzip (including concatenated members) without ever holding
    more than `limit` expanded bytes, so a tiny compressed body cannot
    balloon into hundreds of megabytes."""
    out = bytearray()
    remaining = data
    try:
        while remaining:
            inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
            out += inflater.decompress(remaining, limit + 1 - len(out))
            if len(out) > limit or inflater.unconsumed_tail:
                raise TransportError("response too large")
            if not inflater.eof:
                raise TransportError("Proton sent a truncated response")
            remaining = inflater.unused_data
    except zlib.error as exc:
        raise TransportError("Proton sent a corrupt response") from exc
    return bytes(out)


def _proxy_for(host: str) -> tuple[str, int] | None:
    proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or ""
    if not proxy:
        return None
    no_proxy = (os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or "").split(",")
    for entry in (e.strip().lstrip(".") for e in no_proxy):
        if entry == "*" or (entry and (host == entry or host.endswith("." + entry))):
            return None
    parts = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    if not parts.hostname:
        return None
    return parts.hostname, parts.port or 8080


def _describe(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "Proton did not respond in time"
    if isinstance(exc, ssl.SSLError):
        return f"TLS error talking to Proton ({exc.__class__.__name__})"
    if isinstance(exc, OSError):
        return "Cannot reach Proton (offline?)"
    return f"Network error: {exc.__class__.__name__}"
