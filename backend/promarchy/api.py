"""Minimal Proton API client.

Only message *metadata* is used (sender, subject, time, flags). Proton stores
that in plaintext server-side, so no keys are unlocked and no mail bodies are
ever downloaded or decrypted.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict, dataclass, field
from typing import Any

from . import __version__
from .transport import HttpTransport, Response, TransportError

API_BASE = "https://mail-api.proton.me"

# Proton validates x-pm-appversion against its registered clients and rejects
# unknown product names. "Other" is the generic identifier Proton's own Python
# library (python-proton-core) sends by default, so we use it rather than
# impersonating one of Proton's apps. The User-Agent says who we really are.
APP_VERSION = "Other"
USER_AGENT = f"Promarchy/{__version__} (Linux; Omarchy)"

TIMEOUT = 20  # seconds per socket operation; the daemon must never hang.

CODE_OK = (1000, 1001)
CODE_WRONG_CREDENTIALS = 8002
CODE_HUMAN_VERIFICATION = 9001
CODE_HUMAN_VERIFICATION_INVALID = 12087


class ProtonError(Exception):
    """An API call failed. ``code`` is Proton's body code when there was one."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        status: int | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}


class NetworkError(ProtonError):
    """Proton could not be reached (offline, DNS, TLS, timeout)."""


class RateLimited(ProtonError):
    def __init__(self, message: str, retry_after: int, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class AuthExpired(ProtonError):
    """The stored session is gone; the user has to sign in again."""


class WrongCredentials(ProtonError):
    pass


class HumanVerificationRequired(ProtonError):
    @property
    def token(self) -> str:
        return str(self.details.get("HumanVerificationToken") or "")

    @property
    def methods(self) -> list[str]:
        methods = self.details.get("HumanVerificationMethods") or []
        return [str(m) for m in methods if isinstance(m, str)]


@dataclass
class Session:
    uid: str
    access_token: str
    refresh_token: str
    email: str = ""
    user_id: str = ""
    scopes: list[str] = field(default_factory=list)
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Session:
        return cls(
            uid=checked_token(data["uid"]),
            access_token=checked_token(data["access_token"]),
            refresh_token=checked_token(data["refresh_token"]),
            email=str(data.get("email") or ""),
            user_id=str(data.get("user_id") or ""),
            scopes=[str(s) for s in data.get("scopes") or []],
            created_at=float(data.get("created_at") or 0.0),
        )


_TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{1,1024}")


def checked_token(value: Any) -> str:
    """Accept a UID or token only in its expected shape.

    A corrupted value would otherwise reach an HTTP header, and the library
    exception for a bad header quotes the value back, token and all.
    """
    text = str(value)
    if not _TOKEN.fullmatch(text):
        raise ValueError("malformed session value")  # never echo the value itself
    return text


class ProtonClient:
    """Thin HTTPS client that knows Proton's auth conventions.

    ``on_refresh`` is called with the session right after its tokens rotate.
    Refresh tokens are single-use, so the caller must persist the new pair
    before doing anything else or the next restart will be signed out.
    """

    def __init__(
        self,
        session: Session | None = None,
        *,
        on_refresh: Callable[[Session], None] | None = None,
        refresh_guard: Callable[[], AbstractContextManager[Session | None]] | None = None,
        base: str = API_BASE,
        http: HttpTransport | None = None,
    ) -> None:
        self.session = session
        self.base = base.rstrip("/")
        self._on_refresh = on_refresh
        # Held across "renew, then save": other processes using the same
        # stored session wait instead of consuming the single-use token too.
        # It yields the currently stored session (or None).
        self._refresh_guard = refresh_guard
        self._http = http or HttpTransport(timeout=TIMEOUT)
        self._http.headers.update(
            {
                "x-pm-appversion": APP_VERSION,
                "User-Agent": USER_AGENT,
                "Accept": "application/vnd.protonmail.v1+json",
            }
        )

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ core

    def call(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        body: dict | None = None,
        auth: bool = True,
        headers: dict | None = None,
        _retry_auth: bool = True,
    ) -> dict:
        resp = self._send(method, path, params=params, body=body, auth=auth, headers=headers)
        if resp.status_code == 401 and auth and _retry_auth and self.session:
            self.refresh()
            return self.call(
                method, path, params=params, body=body, auth=auth, headers=headers, _retry_auth=False
            )
        return self._parse(resp)

    def fetch_bytes(self, path: str, *, params: Any = None, max_bytes: int) -> tuple[int, str, bytes]:
        """GET a small binary resource (sender logos). Returns (status, type, body)."""
        resp = self._send("GET", path, params=params, body=None, auth=True, headers=None, max_bytes=max_bytes)
        if resp.status_code == 401 and self.session:
            self.refresh()
            resp = self._send(
                "GET", path, params=params, body=None, auth=True, headers=None, max_bytes=max_bytes
            )
        if resp.status_code in (429, 503):
            raise RateLimited("Proton asked us to slow down", _retry_after(resp))
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        return resp.status_code, ctype, resp.content

    def refresh(self) -> None:
        session = self.session
        if session is None:
            raise AuthExpired("Not signed in")
        with self._refresh_guard() if self._refresh_guard else nullcontext() as stored:
            if (
                stored is not None
                and stored.uid == session.uid
                and stored.refresh_token != session.refresh_token
            ):
                # Another process renewed this session while we waited for
                # the lock: use its tokens instead of spending ours.
                session.access_token = stored.access_token
                session.refresh_token = stored.refresh_token
                return
            self._renew(session)

    def _renew(self, session: Session) -> None:
        body = {
            "UID": session.uid,
            "RefreshToken": session.refresh_token,
            "ResponseType": "token",
            "GrantType": "refresh_token",
            "RedirectURI": "https://protonmail.ch",
        }
        for attempt in range(2):
            try:
                payload = self.call(
                    "POST",
                    "/auth/v4/refresh",
                    body=body,
                    auth=False,
                    headers={"x-pm-uid": session.uid},
                    _retry_auth=False,
                )
                break
            except (NetworkError, RateLimited):
                raise
            except ProtonError as exc:
                # 409: the refresh raced another one server-side; retry once.
                if exc.status == 409 and attempt == 0:
                    continue
                if exc.status in (400, 401, 403, 422):
                    raise AuthExpired("Your Proton session expired. Sign in again.") from exc
                raise
        try:
            access, refresh = checked_token(payload["AccessToken"]), checked_token(payload["RefreshToken"])
        except (KeyError, ValueError) as exc:
            raise AuthExpired("Proton returned an unusable session. Sign in again.") from exc
        session.access_token = access
        session.refresh_token = refresh
        session.scopes = [str(s) for s in payload.get("Scopes") or session.scopes]
        if self._on_refresh:
            self._on_refresh(session)

    # --------------------------------------------------------------- helpers

    def _send(self, method, path, *, params, body, auth, headers, max_bytes=None) -> Response:
        request_headers: dict[str, str] = {}
        if auth and self.session:
            request_headers["x-pm-uid"] = self.session.uid
            request_headers["Authorization"] = f"Bearer {self.session.access_token}"
        if headers:
            request_headers.update(headers)
        try:
            extra = {"max_bytes": max_bytes} if max_bytes else {}
            return self._http.request(
                method,
                self.base + path,
                params=params,
                json=body,
                headers=request_headers,
                timeout=TIMEOUT,
                **extra,
            )
        except (TransportError, OSError) as exc:
            raise NetworkError(str(exc) or "Cannot reach Proton") from exc

    @staticmethod
    def _parse(resp: Response) -> dict:
        status = resp.status_code
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            if status in (429, 503):
                raise RateLimited("Proton asked us to slow down", _retry_after(resp), status=status)
            raise ProtonError(f"Unexpected response from Proton (HTTP {status})", status=status)

        code = payload.get("Code")
        message = str(payload.get("Error") or f"Proton returned HTTP {status}")
        details = payload.get("Details") if isinstance(payload.get("Details"), dict) else {}
        kw = {"code": code, "status": status, "details": details}

        if status in (429, 503):
            raise RateLimited(message, _retry_after(resp), **kw)
        if code in (CODE_HUMAN_VERIFICATION, CODE_HUMAN_VERIFICATION_INVALID):
            raise HumanVerificationRequired(message, **kw)
        if code == CODE_WRONG_CREDENTIALS:
            raise WrongCredentials(message, **kw)
        if status == 401:
            raise AuthExpired(message, **kw)
        if code not in CODE_OK or status >= 400:
            raise ProtonError(message, **kw)
        return payload


def _retry_after(resp: Response) -> int:
    value = resp.headers.get("retry-after", "")
    try:
        return max(5, min(int(value), 3600))
    except ValueError:
        return 60


def now() -> int:
    return int(time.time())
