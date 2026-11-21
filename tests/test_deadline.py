"""A request has one deadline, whatever the server does with the time.

Real sockets and real TLS on loopback: the point is what a timeout set on a
socket does not cover, so nothing here is faked. Each server below keeps
answering often enough that no single read or write would ever time out.
"""

from __future__ import annotations

import contextlib
import shutil
import socket
import ssl
import subprocess
import threading
import time

import pytest

from promarchy.transport import HttpTransport, TransportError

BUDGET = 0.5  # seconds a request may take in these tests
PROMPT = 3.0  # "gave up at the deadline", with room for a busy machine
TRICKLE = 0.05  # seconds between a slow server's bytes: far below BUDGET
GIVE_UP = 8.0  # a slow server stops by itself, so a regression fails instead of hanging


@pytest.fixture(scope="module")
def certificate(tmp_path_factory):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is needed to make a throwaway certificate")
    folder = tmp_path_factory.mktemp("tls")
    cert, key = folder / "cert.pem", folder / "key.pem"
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"]
        + ["-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", "/CN=127.0.0.1"]
        + ["-addext", "subjectAltName=IP:127.0.0.1"],
        check=True,
        capture_output=True,
    )
    return cert, key


class Server:
    """Accepts on loopback and hands each connection to `behave`."""

    def __init__(self, certificate, behave, tls=True):
        cert, key = certificate
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        self.trust = ssl.create_default_context(cafile=str(cert))
        self.behave, self.tls = behave, tls
        self.connections = 0
        self.requests = []
        self.stop = threading.Event()
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.url = f"https://127.0.0.1:{self.listener.getsockname()[1]}/x"
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        with contextlib.suppress(OSError), conn:
            if self.tls:
                conn = self.context.wrap_socket(conn, server_side=True)
            self.behave(self, conn)

    def trickle(self, conn, data):
        """One byte at a time, each well inside any timeout."""
        end = time.monotonic() + GIVE_UP
        for i in range(len(data)):
            if self.stop.wait(TRICKLE) or time.monotonic() > end:
                return
            conn.sendall(data[i : i + 1])

    def client(self, timeout=BUDGET):
        transport = HttpTransport(timeout=timeout)
        transport._context = self.trust
        return transport

    def close(self):
        self.stop.set()
        self.listener.close()


@pytest.fixture(autouse=True)
def no_inherited_proxy(monkeypatch):
    for name in ("https_proxy", "HTTPS_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def serve(certificate):
    servers = []

    def start(behave, tls=True):
        servers.append(Server(certificate, behave, tls))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


def read_request(conn):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            return b""
        data += chunk
    return data


def answers(server, conn):
    while request := read_request(conn):
        server.requests.append(request)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")


def trickles_body(server, conn):
    read_request(conn)
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
    server.trickle(conn, b"x" * 100000)


def trickles_chunks(server, conn):
    read_request(conn)
    conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
    server.trickle(conn, b"1\r\nx\r\n" * 20000)


def trickles_headers(server, conn):
    read_request(conn)
    conn.sendall(b"HTTP/1.1 200 OK\r\n")
    server.trickle(conn, b"X-Padding: " + b"x" * 60000)


def never_reads(server, conn):
    server.stop.wait(GIVE_UP)


def timed_out(client, url, method="GET", **kw):
    started = time.monotonic()
    with pytest.raises(TransportError, match="did not respond in time"):
        client.request(method, url, **kw)
    return time.monotonic() - started


@pytest.mark.parametrize("behave", [trickles_body, trickles_chunks, trickles_headers])
def test_a_trickling_reply_ends_at_the_deadline(serve, behave):
    server = serve(behave)
    took = timed_out(server.client(), server.url)
    assert BUDGET * 0.9 <= took < PROMPT


def test_a_handshake_that_never_finishes_ends_at_the_deadline(serve):
    server = serve(never_reads, tls=False)  # accepts the connection, then says nothing
    took = timed_out(server.client(), server.url)
    assert BUDGET * 0.9 <= took < PROMPT


def test_a_server_that_stops_reading_ends_at_the_deadline(serve):
    server = serve(never_reads)
    # Far more than the socket buffers hold, so the send itself has to wait.
    took = timed_out(server.client(), server.url, method="POST", json={"pad": "x" * 30_000_000})
    assert BUDGET * 0.9 <= took < PROMPT


def test_the_deadline_is_per_request_not_per_connection(serve):
    server = serve(answers)
    client = server.client()
    assert client.request("GET", server.url).content == b"{}"
    time.sleep(BUDGET * 1.2)  # the first request's deadline has long passed
    assert client.request("GET", server.url).content == b"{}"
    assert server.connections == 1  # and it was the same connection


def test_a_request_can_ask_for_a_shorter_deadline(serve):
    server = serve(trickles_body)
    took = timed_out(server.client(timeout=30), server.url, timeout=BUDGET)
    assert took < PROMPT


def test_a_retry_shares_the_first_attempts_deadline(serve):
    """A dropped keep-alive connection earns a GET one more try, not more time."""
    state = {"served": 0}

    def answers_once_then_trickles(server, conn):
        state["served"] += 1
        if state["served"] == 1:
            read_request(conn)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
            return  # and hangs up, which the client finds out on its next request
        trickles_body(server, conn)

    server = serve(answers_once_then_trickles)
    client = server.client()
    assert client.request("GET", server.url).status_code == 200
    took = timed_out(client, server.url)
    assert state["served"] == 2 and took < PROMPT


def relays(server, conn):
    """A minimal http:// proxy: answer CONNECT, then pass bytes both ways."""
    request = read_request(conn)
    server.requests.append(request)
    host, port = request.split()[1].decode().rsplit(":", 1)
    with socket.create_connection((host, int(port))) as upstream:
        conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")

        def pump(source, sink):
            with contextlib.suppress(OSError):
                while data := source.recv(65536):
                    sink.sendall(data)
            with contextlib.suppress(OSError):
                sink.shutdown(socket.SHUT_RDWR)

        threading.Thread(target=pump, args=(upstream, conn), daemon=True).start()
        pump(conn, upstream)


def through_proxy(serve, monkeypatch, behave):
    server = serve(behave)
    proxy = serve(relays, tls=False)
    port = proxy.listener.getsockname()[1]
    monkeypatch.setenv("https_proxy", f"http://me:pass@127.0.0.1:{port}")
    return server, proxy


def test_a_proxy_tunnel_carries_tls_with_the_server_itself(serve, monkeypatch):
    server, proxy = through_proxy(serve, monkeypatch, answers)
    client = server.client()
    assert client.request("GET", server.url).content == b"{}"
    assert client.request("GET", server.url).content == b"{}"
    port = server.listener.getsockname()[1]
    (connect,) = proxy.requests  # one tunnel, used twice
    assert connect.startswith(f"CONNECT 127.0.0.1:{port} ".encode())
    assert b"Proxy-Authorization: Basic bWU6cGFzcw==" in connect
    assert len(server.requests) == 2 and server.connections == 1
    assert all(b"Proxy-Authorization" not in request for request in server.requests)
    assert all(f"Host: 127.0.0.1:{port}".encode() in request for request in server.requests)


def test_a_trickling_reply_through_a_proxy_ends_at_the_deadline(serve, monkeypatch):
    server, _ = through_proxy(serve, monkeypatch, trickles_body)
    took = timed_out(server.client(), server.url)
    assert BUDGET * 0.9 <= took < PROMPT


# The connection is now wrapped in TLS here rather than inside http.client, so
# the two checks that make it Proton's connection are pinned down as well.


def test_a_certificate_for_another_name_is_refused(serve):
    server = serve(answers)
    port = server.listener.getsockname()[1]
    with pytest.raises(TransportError, match="TLS error"):
        server.client().request("GET", f"https://localhost:{port}/x")  # the certificate names 127.0.0.1
    assert server.requests == []


def test_a_certificate_nobody_vouches_for_is_refused(serve):
    server = serve(answers)
    with pytest.raises(TransportError, match="TLS error"):
        HttpTransport(timeout=BUDGET).request("GET", server.url)  # the system's trust store
    assert server.requests == []
