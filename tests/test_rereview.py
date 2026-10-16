"""Regression tests for the Codex re-review of 2603349 (R01–R06)."""

from __future__ import annotations

import argparse
import itertools
import json
import subprocess

import pytest
from fakes import NOW, Clock, FakeProton, MemorySecrets
from test_hardening import daemon_env  # noqa: F401  (fixture)

from promarchy import cli
from promarchy.api import ProtonClient, RateLimited, Session
from promarchy.auth import GONE, REVOKED, UNCONFIRMED, end_session
from promarchy.avatars import Avatars
from promarchy.mailbox import Mailbox, summarize_conversation
from promarchy.store import SecretToolBackend, SessionStore, StoreUnavailable

# ----------------------------------------------------------------- R01 logout


class Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self.headers = {"content-type": "application/json"}
        self.content = json.dumps(payload).encode()

    def json(self):
        return json.loads(self.content)


class AuthServer:
    """Proton's session rules: refresh tokens are single-use, DELETE needs a
    current access token, and a revoked session accepts nothing."""

    def __init__(self, uid="uid1"):
        self.uid = uid
        self.counter = itertools.count(1)
        self.access, self.refresh = "acc0", "ref0"
        self.revoked = False
        self.headers = {}

    def rotate(self):
        n = next(self.counter)
        self.access, self.refresh = f"acc{n}", f"ref{n}"
        return self.access, self.refresh

    def request(self, method, url, params=None, json=None, headers=None, timeout=None, max_bytes=None):
        headers = headers or {}
        if url.endswith("/auth/v4/refresh"):
            if self.revoked or json["RefreshToken"] != self.refresh:
                return Resp(422, {"Code": 10013, "Error": "Invalid refresh token"})
            access, refresh = self.rotate()
            return Resp(200, {"Code": 1000, "AccessToken": access, "RefreshToken": refresh})
        if url.endswith("/auth/v4") and method == "DELETE":
            if self.revoked or headers.get("Authorization") != f"Bearer {self.access}":
                return Resp(401, {"Code": 401, "Error": "Invalid access token"})
            self.revoked = True
            return Resp(200, {"Code": 1000})
        raise AssertionError(url)

    def close(self):
        pass


def stored_session(server, email="me@proton.me"):
    return Session(uid=server.uid, access_token=server.access, refresh_token=server.refresh, email=email)


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    return SessionStore(secrets=MemorySecrets(), root=root)


def test_stale_tokens_after_a_daemon_refresh_still_revoke(store):
    server = AuthServer()
    cli_copy = stored_session(server)  # the CLI loads these...
    store.save(cli_copy)
    server.rotate()  # ...then the daemon refreshes and saves the new pair
    store.update_tokens(stored_session(server))
    client = ProtonClient(Session(**cli_copy.to_dict()), http=server)
    assert end_session(client, lambda: store.load("me@proton.me")) == REVOKED
    assert server.revoked is True


def test_a_genuinely_dead_session_is_reported_gone(store):
    server = AuthServer()
    store.save(stored_session(server))
    server.revoked = True
    client = ProtonClient(stored_session(server), http=server)
    assert end_session(client, lambda: store.load("me@proton.me")) == GONE


def test_unreachable_proton_is_unconfirmed(store):
    class Offline(AuthServer):
        def request(self, *a, **kw):
            raise ConnectionRefusedError("offline")

    server = Offline()
    store.save(stored_session(server))
    client = ProtonClient(stored_session(server), http=server)
    assert end_session(client, lambda: store.load("me@proton.me")) == UNCONFIRMED


@pytest.fixture
def cli_env(store, monkeypatch):
    server = AuthServer()
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    monkeypatch.setattr(cli, "ProtonClient", lambda s, **kw: ProtonClient(s, http=server, **kw))
    return store, server


def args(**kw):
    return argparse.Namespace(**{"email": None, "all": True, "local": False, **kw})


def test_cli_logout_survives_a_concurrent_daemon_refresh(cli_env, monkeypatch):
    store, server = cli_env
    store.save(stored_session(server))
    real_load = store.load
    calls = {"n": 0}

    def load_then_daemon_refreshes(email):
        session = real_load(email)
        calls["n"] += 1
        if calls["n"] == 1:  # right after the CLI read its copy
            server.rotate()
            store.update_tokens(stored_session(server))
        return session

    monkeypatch.setattr(store, "load", load_then_daemon_refreshes)
    assert cli.cmd_logout(args()) == 0
    assert server.revoked is True
    assert store.accounts() == []


def test_unreadable_session_changes_nothing_without_local(cli_env, monkeypatch, capsys):
    store, server = cli_env
    store.save(stored_session(server))
    monkeypatch.setattr(store, "load", lambda email: None)  # e.g. a locked keyring
    assert cli.cmd_logout(args()) == 1
    assert store.accounts() == ["me@proton.me"]
    assert "could not be read" in capsys.readouterr().err


def test_local_removal_never_deletes_a_session_that_appeared_meanwhile(store):
    store.save(Session(uid="new", access_token="a", refresh_token="r", email="me@proton.me"))
    assert store.remove("me@proton.me", require_absent=True) is False
    assert store.load("me@proton.me").uid == "new"


def test_failed_save_of_rotated_tokens_does_not_abort_logout(cli_env, monkeypatch):
    store, server = cli_env
    store.save(stored_session(server))
    server.access = "expired-on-server"  # forces a refresh before DELETE

    def broken(session):
        raise StoreUnavailable("keyring busy")

    monkeypatch.setattr(store, "update_tokens", broken)
    assert cli.cmd_logout(args()) == 0
    assert server.revoked is True


# --------------------------------------------------------------- R02 logos


SVG_UTF16 = '<?xml version="1.0" encoding="UTF-16"?><svg><image href="file:///tmp/x.png"/></svg>'.encode(
    "utf-16"
)
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16
WEBP = b"RIFF\x10\0\0\0WEBPVP8 "


class LogoClient:
    def __init__(self, ctype, body):
        self.ctype, self.body, self.params = ctype, body, None

    def fetch_bytes(self, path, *, params=None, max_bytes):
        self.params = params
        return 200, self.ctype, self.body


@pytest.mark.parametrize(
    "ctype,body",
    [
        ("image/svg+xml", SVG_UTF16),
        ("image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'><path d='M0 0'/></svg>"),
        ("image/png", b"<svg><image href='x'/></svg>"),  # mislabeled
        ("text/html", PNG),
    ],
)
def test_only_raster_logos_are_ever_stored(tmp_path, ctype, body):
    avatars = Avatars(root=tmp_path / "logos")
    row = {"senderImage": True, "senderAddress": "x@example.com"}
    avatars.attach([row], LogoClient(ctype, body))
    assert row.get("avatar", "") == ""
    assert [p.suffix for p in (tmp_path / "logos").iterdir()] == [".miss"]


@pytest.mark.parametrize("body,suffix", [(PNG, ".png"), (WEBP, ".webp"), (b"\xff\xd8\xff\xe0jpeg", ".jpg")])
def test_raster_logos_are_accepted_and_png_is_requested(tmp_path, body, suffix):
    avatars = Avatars(root=tmp_path / "logos")
    client = LogoClient("image/png", body)
    row = {"senderImage": True, "senderAddress": "x@example.com"}
    avatars.attach([row], client)
    assert row["avatar"].endswith(suffix)
    assert client.params["Format"] == "png"


def test_svgs_cached_by_older_versions_are_ignored_and_pruned(tmp_path):
    avatars = Avatars(root=tmp_path / "logos")
    stem = avatars._stem("x@example.com")
    old = tmp_path / "logos" / f"{stem}.svg"
    old.write_bytes(SVG_UTF16)
    assert avatars._cached("x@example.com") is None
    avatars.prune()
    assert not old.exists()


# ------------------------------------------------------- R03 conversations


def test_inbox_row_uses_the_inbox_context_of_a_spanning_conversation():
    """Proton-shaped: one read message in Inbox; unread mail in Archive and
    Sent; starred; Primary. Values as published in WebClients' Conversation."""
    conversation = {
        "ID": "c1",
        "Subject": "Plan",
        "Senders": [{"Name": "Ada", "Address": "ada@example.com"}],
        "CategoryID": "24",
        "NumMessages": 5,
        "NumUnread": 3,
        "Time": NOW,
        "Labels": [
            {"ID": "0", "ContextNumMessages": 1, "ContextNumUnread": 0, "ContextTime": NOW - 500},
            {"ID": "2", "ContextNumMessages": 2, "ContextNumUnread": 1, "ContextTime": NOW - 10},
            {"ID": "6", "ContextNumMessages": 2, "ContextNumUnread": 2, "ContextTime": NOW},
            {"ID": "10", "ContextNumMessages": 1, "ContextNumUnread": 0, "ContextTime": NOW - 500},
        ],
    }
    row = summarize_conversation(conversation, "me@proton.me")
    assert row["threadCount"] == 1
    assert row["unread"] is False
    assert row["starred"] is True
    assert row["category"] == "primary"
    assert row["time"] == NOW - 500


def test_spanning_conversation_end_to_end_through_the_fake_api():
    api = FakeProton()
    api.add(500, conversation="c1", unread=False, labels=("0", "5", "24"))
    api.add(10, conversation="c1", unread=True, labels=("2", "5"))  # sent reply
    api.add(5, conversation="c1", unread=True, labels=("6", "5"))  # archived
    box = Mailbox(api, "me@proton.me", clock=Clock())
    box.full_sync()
    row = box.recent[0]
    assert (row["threadCount"], row["unread"], row["time"]) == (1, False, NOW - 500)


# --------------------------------------------------------- R04 one account


def test_another_address_of_the_same_account_replaces_the_old_entry(store):
    store.save(
        Session(uid="u1", access_token="a", refresh_token="r", email="old@example.com", user_id="USER")
    )
    displaced = store.replace(
        Session(uid="u2", access_token="a", refresh_token="r", email="new@proton.me", user_id="USER")
    )
    assert [s.email for s in displaced] == ["old@example.com"]
    assert store.accounts() == ["new@proton.me"]
    assert store.load("old@example.com") is None


def test_daemon_never_loads_the_same_account_twice(daemon_env):  # noqa: F811
    d, api, store, clock, states = daemon_env
    api.add(60)
    store.secrets.set(
        "session:a@proton.me",
        json.dumps(
            Session(uid="ua", access_token="a", refresh_token="r", email="a@proton.me", user_id="U").to_dict()
        ),
    )
    store.secrets.set(
        "session:b@proton.me",
        json.dumps(
            Session(uid="ub", access_token="a", refresh_token="r", email="b@proton.me", user_id="U").to_dict()
        ),
    )
    store._write_index(["a@proton.me", "b@proton.me"])
    store._bump()
    d.tick()
    assert list(d.accounts) == ["a@proton.me"]
    assert states()[-1]["counts"]["1h"] == 1


# ------------------------------------------------------ R05 Retry-After gate


def throttle(d, api, clock, seconds=300):
    api.fail_with = RateLimited("slow down", seconds)
    clock.advance(31)
    d.tick()
    api.fail_with = None
    return len(api.calls)


def test_shorter_poll_setting_cannot_bypass_retry_after(daemon_env):  # noqa: F811
    d, api, store, clock, states = daemon_env
    store.save(Session(uid="u", access_token="a", refresh_token="r", email="me@proton.me"))
    d.tick()
    calls = throttle(d, api, clock)
    d.handle({"cmd": "configure", "settings": {"pollSeconds": 10}})
    clock.advance(11)
    d.tick()
    assert len(api.calls) == calls
    clock.advance(300)
    d.tick()
    assert len(api.calls) > calls


def test_mark_read_waits_for_retry_after(daemon_env):  # noqa: F811
    d, api, store, clock, states = daemon_env
    message = api.add(60)
    store.save(Session(uid="u", access_token="a", refresh_token="r", email="me@proton.me"))
    d.tick()
    calls = throttle(d, api, clock)
    d.handle({"cmd": "markRead", "account": "me@proton.me", "ids": [message["ID"]]})
    assert len(api.calls) == calls
    assert "wait" in states()[-1]["message"]


# ---------------------------------------------------- R06 keyring exit codes


def backend_returning(monkeypatch, returncode, stderr=""):
    backend = SecretToolBackend.__new__(SecretToolBackend)
    backend.tool = "/usr/bin/secret-tool"
    result = subprocess.CompletedProcess([], returncode, stdout="", stderr=stderr)
    monkeypatch.setattr(backend, "_run", lambda *a, **kw: result)
    return backend


@pytest.mark.parametrize("returncode,stderr", [(-9, ""), (2, ""), (1, "D-Bus error")])
def test_abnormal_keyring_helper_exits_are_errors(monkeypatch, returncode, stderr):
    backend = backend_returning(monkeypatch, returncode, stderr)
    with pytest.raises(StoreUnavailable):
        backend.delete("session:x")
    with pytest.raises(StoreUnavailable):
        backend.get("session:x")


def test_quiet_status_one_means_no_match(monkeypatch):
    backend = backend_returning(monkeypatch, 1)
    backend.delete("session:x")  # nothing to delete: fine
    assert backend.get("session:x") is None
