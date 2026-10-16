from __future__ import annotations

import gzip
import http.client
import json

import pytest

from promarchy import transport
from promarchy.transport import HttpTransport, TransportError


class FakeResponse:
    def __init__(self, status=200, body=b"{}", headers=None, will_close=False):
        self.status = status
        self._body = body
        self._headers = headers or [("Content-Type", "application/json")]
        self.will_close = will_close

    def read(self, limit=None):
        return self._body

    def getheader(self, name, default=None):
        for key, value in self._headers:
            if key.lower() == name.lower():
                return value
        return default

    def getheaders(self):
        return self._headers


class FakeConnection:
    instances: list[FakeConnection] = []
    script: list = []

    def __init__(self, host, port, context=None, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.sent = []
        self.closed = False
        self.tunnel = None
        FakeConnection.instances.append(self)

    def set_tunnel(self, host, port):
        self.tunnel = (host, port)

    def request(self, method, path, body=None, headers=None):
        self.sent.append((method, path, body, headers))
        item = FakeConnection.script.pop(0)
        if isinstance(item, Exception):
            raise item
        self._next = item

    def getresponse(self):
        return self._next

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def fake_connections(monkeypatch):
    FakeConnection.instances = []
    FakeConnection.script = []
    monkeypatch.setattr(http.client, "HTTPSConnection", FakeConnection)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    yield


def test_encodes_query_json_and_decodes_gzip():
    payload = gzip.compress(json.dumps({"Code": 1000}).encode())
    FakeConnection.script = [FakeResponse(body=payload, headers=[("Content-Encoding", "gzip")])]
    t = HttpTransport()
    t.headers["x-pm-appversion"] = "Other"
    resp = t.request(
        "POST",
        "https://mail-api.proton.me/core/x",
        params=[("LabelID[]", "0"), ("LabelID[]", "24")],
        json={"a": 1},
    )
    assert resp.json() == {"Code": 1000}
    method, path, body, headers = FakeConnection.instances[0].sent[0]
    assert path == "/core/x?LabelID%5B%5D=0&LabelID%5B%5D=24"
    assert json.loads(body) == {"a": 1}
    assert headers["Content-Type"] == "application/json"
    assert headers["x-pm-appversion"] == "Other"
    assert headers["Accept-Encoding"] == "gzip"
    assert FakeConnection.instances[0].timeout == 20.0


def test_connection_is_reused_within_a_burst():
    FakeConnection.script = [FakeResponse(), FakeResponse()]
    t = HttpTransport()
    t.request("GET", "https://mail-api.proton.me/a")
    t.request("GET", "https://mail-api.proton.me/b")
    assert len(FakeConnection.instances) == 1


def test_idle_connection_is_replaced(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: clock[0])
    FakeConnection.script = [FakeResponse(), FakeResponse()]
    t = HttpTransport()
    t.request("GET", "https://mail-api.proton.me/a")
    clock[0] += transport.IDLE_REUSE_SECONDS + 1
    t.request("GET", "https://mail-api.proton.me/b")
    assert len(FakeConnection.instances) == 2
    assert FakeConnection.instances[0].closed


def test_get_retries_once_on_dropped_keepalive():
    FakeConnection.script = [FakeResponse(), http.client.RemoteDisconnected("bye"), FakeResponse()]
    t = HttpTransport()
    t.request("GET", "https://mail-api.proton.me/a")
    assert t.request("GET", "https://mail-api.proton.me/b").status_code == 200
    assert len(FakeConnection.instances) == 2


def test_post_is_never_replayed():
    FakeConnection.script = [FakeResponse(), http.client.RemoteDisconnected("bye")]
    t = HttpTransport()
    t.request("GET", "https://mail-api.proton.me/a")
    with pytest.raises(TransportError):
        t.request("POST", "https://mail-api.proton.me/auth/v4/refresh", json={})
    assert len(FakeConnection.script) == 0


def test_timeout_becomes_transport_error():
    FakeConnection.script = [TimeoutError()]
    with pytest.raises(TransportError, match="did not respond"):
        HttpTransport().request("GET", "https://mail-api.proton.me/a")


def test_refuses_plain_http():
    with pytest.raises(ValueError):
        HttpTransport().request("GET", "http://mail-api.proton.me/a")


def test_https_proxy_is_tunnelled(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.lan:3128")
    FakeConnection.script = [FakeResponse()]
    HttpTransport().request("GET", "https://mail-api.proton.me/a")
    conn = FakeConnection.instances[0]
    assert (conn.host, conn.port, conn.tunnel) == ("proxy.lan", 3128, ("mail-api.proton.me", 443))


def test_no_proxy_is_honoured(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "proxy.lan:3128")
    monkeypatch.setenv("NO_PROXY", ".proton.me")
    FakeConnection.script = [FakeResponse()]
    HttpTransport().request("GET", "https://mail-api.proton.me/a")
    assert FakeConnection.instances[0].tunnel is None
