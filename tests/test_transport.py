from __future__ import annotations

import gzip
import http.client
import json

import pytest

from promarchy import transport
from promarchy.transport import HttpTransport, TransportError

REAL_CONNECTION = transport._Connection


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

    def __init__(self, host, port, context=None, server_name=None):
        self.host, self.port, self.server_name = host, port, server_name
        self.deadline = transport._Deadline()
        self.deadlines = []  # what each request was given
        self.sent = []
        self.closed = False
        self.tunnel = None
        FakeConnection.instances.append(self)

    def set_tunnel(self, host, port, headers=None):
        self.tunnel = (host, port)
        self.tunnel_headers = headers

    def request(self, method, path, body=None, headers=None):
        self.sent.append((method, path, body, headers))
        self.deadlines.append(self.deadline.at)
        item = FakeConnection.script.pop(0)
        if callable(item):
            item = item()
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
    monkeypatch.setattr(transport, "_Connection", FakeConnection)
    # An inherited proxy setting (lowercase wins) must never steer these tests.
    for name in ("https_proxy", "HTTPS_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
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
    assert conn.server_name == "mail-api.proton.me"  # TLS is with Proton, not the proxy


def test_proxy_credentials_go_to_the_proxy_only(monkeypatch):
    import base64

    monkeypatch.setenv("HTTPS_PROXY", "http://me%40corp:p%3Ass@proxy.lan:3128")
    FakeConnection.script = [FakeResponse()]
    HttpTransport().request("GET", "https://mail-api.proton.me/a")
    conn = FakeConnection.instances[0]
    assert (conn.host, conn.port) == ("proxy.lan", 3128)
    expected = "Basic " + base64.b64encode(b"me@corp:p:ss").decode()
    assert conn.tunnel_headers == {"Proxy-Authorization": expected}
    assert all("Proxy-Authorization" not in (h or {}) for _, _, _, h in conn.sent)


def test_no_proxy_is_honoured(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "proxy.lan:3128")
    monkeypatch.setenv("NO_PROXY", ".proton.me")
    FakeConnection.script = [FakeResponse()]
    HttpTransport().request("GET", "https://mail-api.proton.me/a")
    assert FakeConnection.instances[0].tunnel is None


def test_corrupt_gzip_is_a_transport_error():
    FakeConnection.script = [FakeResponse(body=b"not gzip", headers=[("Content-Encoding", "gzip")])] * 2
    with pytest.raises(TransportError, match="corrupt"):
        HttpTransport().request("GET", "https://mail-api.proton.me/a")


@pytest.mark.parametrize(
    "url",
    ["https://me:secret@proxy.lan:3128", "https://proxy.lan:3128", "socks5://me:secret@proxy.lan:1080"],
)
def test_unsupported_proxy_schemes_are_refused_before_connecting(monkeypatch, url):
    monkeypatch.setenv("HTTPS_PROXY", url)
    FakeConnection.script = [FakeResponse()]
    with pytest.raises(TransportError, match="not supported"):
        HttpTransport().request("GET", "https://mail-api.proton.me/a")
    assert FakeConnection.instances == []  # nothing was opened, let alone sent


def test_an_https_proxy_url_never_puts_credentials_on_the_wire(monkeypatch):
    """Real sockets: a loopback listener posing as the proxy must receive nothing."""
    import socket as socketlib

    monkeypatch.setattr(transport, "_Connection", REAL_CONNECTION)
    with socketlib.socket(socketlib.AF_INET, socketlib.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.5)
        port = listener.getsockname()[1]
        monkeypatch.setenv("HTTPS_PROXY", f"https://me:secret@127.0.0.1:{port}")
        with pytest.raises(TransportError):
            HttpTransport(timeout=2).request("GET", "https://mail-api.proton.me/a")
        with pytest.raises(TimeoutError):
            listener.accept()


def test_each_request_gets_its_own_deadline(monkeypatch):
    """A reused connection must not keep the previous caller's deadline."""
    clock = [1000.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: clock[0])
    FakeConnection.script = [FakeResponse(), FakeResponse()]
    t = HttpTransport(timeout=20)
    t.request("GET", "https://mail-api.proton.me/a", timeout=8)
    clock[0] += 5
    t.request("GET", "https://mail-api.proton.me/b")
    assert len(FakeConnection.instances) == 1
    assert FakeConnection.instances[0].deadlines == [1008.0, 1025.0]


def test_a_retry_gets_no_extra_time(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: clock[0])

    def dropped_after_a_while():
        clock[0] += 3
        return http.client.RemoteDisconnected("bye")

    FakeConnection.script = [FakeResponse(), dropped_after_a_while, FakeResponse()]
    t = HttpTransport(timeout=20)
    t.request("GET", "https://mail-api.proton.me/a")
    assert t.request("GET", "https://mail-api.proton.me/b").status_code == 200
    first, second = FakeConnection.instances
    # The three seconds the first attempt lost are not given back.
    assert first.deadlines == [1020.0, 1020.0] and second.deadlines == [1020.0]


def test_only_a_connection_that_was_reused_earns_a_retry(monkeypatch):
    """An idle connection is replaced before the request: its failure is the
    new connection's own, not a keep-alive the server dropped."""
    clock = [1000.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: clock[0])
    FakeConnection.script = [FakeResponse(), http.client.RemoteDisconnected("bye"), FakeResponse()]
    t = HttpTransport()
    t.request("GET", "https://mail-api.proton.me/a")
    clock[0] += transport.IDLE_REUSE_SECONDS + 1
    with pytest.raises(TransportError):
        t.request("GET", "https://mail-api.proton.me/b")
    assert len(FakeConnection.script) == 1  # the third answer was never asked for


def test_a_passed_deadline_stops_before_anything_is_sent():
    deadline = transport._Deadline()
    deadline.at = transport.time.monotonic() - 1
    sent = []

    class Sock:
        def settimeout(self, value):
            raise AssertionError("no time left to set")

        def send(self, data):
            sent.append(bytes(data))
            return len(data)

    with pytest.raises(TimeoutError):
        transport._TimedSocket(Sock(), deadline).sendall(b"secret")
    assert sent == []
