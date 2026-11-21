from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest
from fakes import FakeProton

from promarchy import api
from promarchy.api import (
    APP_VERSION,
    TIMEOUT,
    AuthExpired,
    HumanVerificationRequired,
    NetworkError,
    ProtonClient,
    ProtonError,
    RateLimited,
    Session,
    WrongCredentials,
)
from promarchy.transport import TransportError


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

    def request(self, method, url, params=None, json=None, headers=None, timeout=None, max_bytes=None):
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
    http = FakeHttp([ConnectionRefusedError("down")])
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
        "/core/v4/images/logo", params={"Address": "a@b.c"}, max_bytes=1024
    )
    assert (status, ctype, body) == (200, "image/png", b"\x89PNG")
    assert http.sent[0]["headers"]["Authorization"] == "Bearer old-access"


# ------------------------------------------------ a time limit for a whole step


class SlowHttp:
    """Answers every request, each one taking `cost` seconds of a fake clock."""

    def __init__(self, monkeypatch, cost, answer=None):
        self.now = 5000.0
        self.cost = cost
        self.answer = answer or (lambda method, path, json: Response(200, {"Code": 1000}))
        self.limits = []  # the timeout each request was given
        self.paths = []
        self.headers = {}
        monkeypatch.setattr(api.time, "monotonic", lambda: self.now)

    def request(self, method, url, params=None, json=None, headers=None, timeout=None, max_bytes=None):
        self.limits.append(timeout)
        self.paths.append(urlsplit(url).path)
        assert timeout > 0
        if self.cost > timeout:
            self.now += timeout
            raise TransportError("Proton did not respond in time")
        self.now += self.cost
        return self.answer(method, urlsplit(url).path, json)

    def close(self):
        pass


def test_requests_in_a_step_share_its_time(monkeypatch):
    http = SlowHttp(monkeypatch, cost=15)
    client = ProtonClient(session(), http=http)
    started = http.now
    with client.within(40):
        client.call("GET", "/a")
        client.call("GET", "/b")
        with pytest.raises(NetworkError, match="did not respond in time"):
            client.call("GET", "/c")  # 10 s left, and it takes 15
        with pytest.raises(NetworkError, match="did not respond in time"):
            client.call("GET", "/d")  # nothing left: not even sent
    assert http.limits == [TIMEOUT, TIMEOUT, 10]
    assert http.now - started == 40
    client.call("GET", "/e")  # the limit ends with its block
    assert http.limits[-1] == TIMEOUT


def test_a_step_never_lengthens_a_request_or_an_outer_step(monkeypatch):
    http = SlowHttp(monkeypatch, cost=1)
    client = ProtonClient(session(), http=http)
    with client.within(600):
        client.fetch_bytes("/logo", max_bytes=10, timeout=8)
        with client.within(5):
            client.call("GET", "/a")
            with client.within(600):
                client.call("GET", "/b")
        client.call("GET", "/c")
    assert http.limits == [8, 5, 4, TIMEOUT]


def test_a_renewal_gets_a_full_request_whatever_the_step_has_left(monkeypatch):
    """Cut short, it could still use up the single-use refresh token."""

    def answer(method, path, json):
        if path == "/auth/v4/refresh":
            return Response(200, {"Code": 1000, "AccessToken": "new-access", "RefreshToken": "new-refresh"})
        return Response(401, {"Code": 401}) if client.session.access_token == "old-access" else ok

    ok = Response(200, {"Code": 1000})
    http = SlowHttp(monkeypatch, cost=8, answer=answer)
    saved = []
    client = ProtonClient(session(), http=http, on_refresh=lambda s: saved.append(s.refresh_token))
    with client.within(18), pytest.raises(NetworkError):
        client.call("GET", "/core/v4/events/x")
    # The 401 took 8 of the 18 seconds; the renewal still got a request's 20
    # rather than the 10 that were left, and only the repeat of the request
    # was held to what remained after it.
    assert http.paths == ["/core/v4/events/x", "/auth/v4/refresh", "/core/v4/events/x"]
    assert http.limits == [18, TIMEOUT, 2]
    assert saved == ["new-refresh"]
    assert client._over_at is None


def test_a_sync_of_slow_answers_stops_at_the_step_limit(monkeypatch):
    """Every answer arrives inside its own deadline, yet the sync as a whole
    is cut off, and the next one starts over."""
    from promarchy.mailbox import Mailbox

    proton = FakeProton()
    proton.add(60)

    def answer(method, path, json):
        params = http.params.pop(0)
        return Response(200, proton.call(method, path, params=params, body=json))

    class Http(SlowHttp):
        params: list = []

        def request(self, method, url, params=None, **kw):
            self.params.append(params)
            return super().request(method, url, params=params, **kw)

    http = Http(monkeypatch, cost=TIMEOUT - 1, answer=answer)
    client = ProtonClient(session(), http=http)
    mailbox = Mailbox(client, "me@proton.me", clock=lambda: proton.now)
    started = http.now
    with client.within(60), pytest.raises(NetworkError):
        mailbox.poll()
    assert http.now - started == 60
    assert not mailbox.synced and mailbox.dirty  # so nothing of it is shown

    http.cost = 1
    with client.within(60):
        assert mailbox.poll() is True
    assert mailbox.synced and len(mailbox.unread_times) == 1
