from __future__ import annotations

import json

import pytest
import requests

from promarchy.api import (
    APP_VERSION,
    AuthExpired,
    HumanVerificationRequired,
    NetworkError,
    ProtonClient,
    ProtonError,
    RateLimited,
    Session,
    WrongCredentials,
)


class Response:
    def __init__(self, status: int, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {"content-type": "application/json"}
        self.content = json.dumps(payload).encode() if payload is not None else b""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeHttp:
    """Scripted stand-in for requests.Session."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []
        self.headers = {}

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        assert timeout is not None, "every request needs a timeout"
        self.sent.append({"method": method, "url": url, "json": json, "headers": dict(headers or {})})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


def session() -> Session:
    return Session(uid="uid1", access_token="old-access", refresh_token="old-refresh", email="me@proton.me")


def test_identifies_honestly():
    http = FakeHttp([Response(200, {"Code": 1000})])
    client = ProtonClient(http=http)
    client.call("GET", "/tests/ping", auth=False)
    assert http.headers["x-pm-appversion"] == APP_VERSION == "Other"
    assert http.headers["User-Agent"].startswith("Promarchy/")


def test_sends_bearer_and_uid():
    http = FakeHttp([Response(200, {"Code": 1000})])
    ProtonClient(session(), http=http).call("GET", "/core/v4/events/latest")
    assert http.sent[0]["headers"]["Authorization"] == "Bearer old-access"
    assert http.sent[0]["headers"]["x-pm-uid"] == "uid1"


def test_401_refreshes_rotates_and_retries():
    rotated = []
    http = FakeHttp(
        [
            Response(401, {"Code": 401, "Error": "expired"}),
            Response(
                200,
                {
                    "Code": 1000,
                    "AccessToken": "new-access",
                    "RefreshToken": "new-refresh",
                    "Scopes": ["mail"],
                },
            ),
            Response(200, {"Code": 1000, "EventID": "e"}),
        ]
    )
    client = ProtonClient(
        session(), http=http, on_refresh=lambda s: rotated.append((s.access_token, s.refresh_token))
    )
    assert client.call("GET", "/core/v4/events/latest")["EventID"] == "e"
    refresh = http.sent[1]
    assert refresh["url"].endswith("/auth/v4/refresh")
    assert refresh["json"]["RefreshToken"] == "old-refresh"
    assert "Authorization" not in refresh["headers"]
    assert rotated == [("new-access", "new-refresh")]
    assert http.sent[2]["headers"]["Authorization"] == "Bearer new-access"


def test_failed_refresh_means_sign_in_again():
    http = FakeHttp(
        [Response(401, {"Code": 401}), Response(422, {"Code": 10013, "Error": "Invalid refresh token"})]
    )
    with pytest.raises(AuthExpired):
        ProtonClient(session(), http=http).call("GET", "/core/v4/events/latest")


def test_refresh_conflict_is_retried_once():
    http = FakeHttp(
        [
            Response(409, {"Code": 409, "Error": "conflict"}),
            Response(200, {"Code": 1000, "AccessToken": "a2", "RefreshToken": "r2"}),
        ]
    )
    client = ProtonClient(session(), http=http)
    client.refresh()
    assert client.session.refresh_token == "r2"


def test_rate_limit_carries_retry_after():
    http = FakeHttp([Response(429, {"Code": 2028, "Error": "slow down"}, {"retry-after": "42"})])
    with pytest.raises(RateLimited) as err:
        ProtonClient(session(), http=http).call("GET", "/x")
    assert err.value.retry_after == 42


def test_human_verification_details():
    details = {"HumanVerificationToken": "tok", "HumanVerificationMethods": ["captcha", "email"]}
    http = FakeHttp(
        [Response(422, {"Code": 9001, "Error": "Human verification required", "Details": details})]
    )
    with pytest.raises(HumanVerificationRequired) as err:
        ProtonClient(http=http).call("POST", "/auth/v4", auth=False)
    assert err.value.token == "tok"
    assert err.value.methods == ["captcha", "email"]


def test_wrong_password_is_distinct():
    http = FakeHttp([Response(422, {"Code": 8002, "Error": "Incorrect login credentials"})])
    with pytest.raises(WrongCredentials):
        ProtonClient(http=http).call("POST", "/auth/v4", auth=False)


def test_network_failure_maps_to_network_error():
    http = FakeHttp([requests.ConnectionError("down")])
    with pytest.raises(NetworkError):
        ProtonClient(http=http).call("GET", "/x", auth=False)


def test_non_json_error():
    http = FakeHttp([Response(502, None)])
    with pytest.raises(ProtonError) as err:
        ProtonClient(http=http).call("GET", "/x", auth=False)
    assert err.value.status == 502


def test_session_roundtrip():
    s = session()
    assert Session.from_dict(json.loads(json.dumps(s.to_dict()))) == s


def test_fetch_bytes_through_real_client():
    png = Response(200, None, {"content-type": "image/png; charset=binary"})
    png.content = b"\x89PNG"
    http = FakeHttp([png])
    status, ctype, body = ProtonClient(session(), http=http).fetch_bytes(
        "/core/v4/images/logo", params={"Address": "a@b.c"}
    )
    assert (status, ctype, body) == (200, "image/png", b"\x89PNG")
    assert http.sent[0]["headers"]["Authorization"] == "Bearer old-access"
