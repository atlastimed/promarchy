"""One owner per session: the running daemon signs out and takes over new
sign-ins on the CLI's behalf.

These run a real Daemon.run() loop with a real Unix control socket inside the
isolated XDG runtime directory set up by conftest.py.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import io
import json
import os
import socket
import threading
import time

import pytest
from fakes import FakeProton, MemorySecrets
from test_daemon import RecordingNotifier
from test_rereview import AuthServer

from promarchy import cli, control, sessions
from promarchy.api import ProtonClient, Session
from promarchy.auth import REJECTED, REVOKED, PendingLogin
from promarchy.avatars import Avatars
from promarchy.daemon import Daemon
from promarchy.notify import Watermarks
from promarchy.paths import open_private, runtime_dir
from promarchy.store import SessionStore, StoreUnavailable


def make_store(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    return SessionStore(secrets=MemorySecrets(), root=root)


def args(**kw):
    return argparse.Namespace(**{"email": None, "all": True, "local": False, **kw})


def make_daemon(store, tmp_path, factory):
    return Daemon(
        store,
        out=io.StringIO(),
        notifier=RecordingNotifier(),
        watermarks=Watermarks(tmp_path / "n.json"),
        avatars=Avatars(root=tmp_path / "logos"),
        client_factory=factory,
        state_path=str(tmp_path / "state.json"),
    )


def shared_api_factory(api):
    def factory(session, on_refresh=None, **kw):
        api.session = session
        return api

    return factory


@contextlib.contextmanager
def daemon_running(daemon):
    """Run a real Daemon.run() loop with its real control socket."""
    read_fd, write_fd = os.pipe()
    stdin = os.fdopen(read_fd)
    thread = threading.Thread(target=daemon.run, args=(stdin,), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not control.socket_path().exists():
        assert time.monotonic() < deadline, "control socket never appeared"
        time.sleep(0.02)
    try:
        yield daemon
    finally:
        os.close(write_fd)  # EOF on stdin: the daemon quits
        thread.join(timeout=5)
    assert not control.socket_path().exists()


@pytest.fixture
def running_daemon(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.save(Session(uid="u1", access_token="a", refresh_token="r", email="me@proton.me"))
    api = FakeProton()
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    with daemon_running(make_daemon(store, tmp_path, shared_api_factory(api))) as daemon:
        yield store, api, daemon


def test_cli_logout_is_done_by_the_running_daemon(running_daemon, capsys):
    store, api, _ = running_daemon
    assert cli.cmd_logout(args()) == 0
    assert getattr(api, "revoked", False) is True  # the daemon's own client revoked it
    assert store.accounts() == []
    assert "Signed out me@proton.me" in capsys.readouterr().out


def test_cli_waits_for_the_daemons_answer_when_proton_is_unreachable(running_daemon, capsys):
    from promarchy.api import NetworkError

    store, api, _ = running_daemon
    api.fail_with = NetworkError("offline")
    assert cli.cmd_logout(args()) == 1
    assert store.accounts() == ["me@proton.me"]
    assert "still signed in" in capsys.readouterr().err


def test_control_socket_refuses_other_requests(running_daemon):
    reply = control.ask({"cmd": "markRead", "ids": ["x"]})
    assert reply == {"error": "unsupported request"}


def test_without_a_daemon_the_cli_owns_the_lock(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.save(Session(uid="u1", access_token="a", refresh_token="r", email="me@proton.me"))
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    seen = {}

    def fake_local(store_, email, local, client_factory):
        # While the CLI acts, nobody else can take the daemon lock.
        fd = open_private(runtime_dir() / "daemon.lock")
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        seen["called"] = True
        return {"email": email, "outcome": "revoked", "removed": True}

    monkeypatch.setattr(sessions, "sign_out_stored", fake_local)
    assert cli.cmd_logout(args()) == 0
    assert seen["called"]


def test_unreachable_but_running_daemon_is_not_bypassed(tmp_path, monkeypatch, capsys):
    store = make_store(tmp_path)
    store.save(Session(uid="u1", access_token="a", refresh_token="r", email="me@proton.me"))
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    held = open_private(runtime_dir() / "daemon.lock")
    fcntl.flock(held, fcntl.LOCK_EX)  # a daemon holds the lock but has no socket
    try:
        assert cli.cmd_logout(args()) == 1
    finally:
        os.close(held)
    assert store.accounts() == ["me@proton.me"]
    assert "did not answer" in capsys.readouterr().err


# ------------------------------------------------- sign-out of every account


def test_logout_covers_an_account_the_daemon_could_not_load(tmp_path, monkeypatch, capsys):
    store = make_store(tmp_path)
    store._write_index(["me@proton.me"])  # listed, but the keyring has nothing
    store._bump()
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    with daemon_running(make_daemon(store, tmp_path, shared_api_factory(FakeProton()))):
        assert cli.cmd_logout(args()) == 1
        assert "could not be read" in capsys.readouterr().err
        assert store.accounts() == ["me@proton.me"]
        assert cli.cmd_logout(args(local=True)) == 0
    assert store.accounts() == []


def test_logout_all_also_ends_an_older_duplicate_the_daemon_skipped(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    for uid, email, created in (("u1", "old@example.com", 1), ("u2", "new@proton.me", 2)):
        session = Session(
            uid=uid, access_token="a", refresh_token="r", email=email, user_id="U", created_at=created
        )
        store.secrets.set(f"session:{email}", json.dumps(session.to_dict()))
    store._write_index(["old@example.com", "new@proton.me"])
    store._bump()
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    ended = []

    def factory(session, on_refresh=None, **kw):
        api = FakeProton()
        api.session = session
        real_call = api.call

        def call(method, path, **kw):
            if method == "DELETE":
                ended.append(session.uid)
            return real_call(method, path, **kw)

        api.call = call
        return api

    daemon = make_daemon(store, tmp_path, factory)
    with daemon_running(daemon):
        assert list(daemon.accounts) == ["new@proton.me"]
        assert cli.cmd_logout(args()) == 0
    assert sorted(ended) == ["u1", "u2"]
    assert store.accounts() == []


def test_failed_keyring_removal_is_reported_not_called_a_newer_sign_in(running_daemon, capsys):
    store, api, _ = running_daemon

    def refuse(key):
        raise StoreUnavailable("keyring locked")

    store.secrets.delete = refuse
    assert cli.cmd_logout(args()) == 1
    err = capsys.readouterr()
    assert "would not let Promarchy forget it" in err.err
    assert "newer sign-in" not in err.out + err.err
    assert store.accounts() == ["me@proton.me"]


@pytest.mark.parametrize("selector", [[], {}, 17, False, "", "   "])
def test_malformed_account_selectors_never_mean_every_account(running_daemon, selector):
    store, api, _ = running_daemon
    assert control.ask({"cmd": "logout", "account": selector}) == {"error": "invalid request"}
    assert control.ask({"cmd": "logout", "account": None, "local": "yes"}) == {"error": "invalid request"}
    assert not getattr(api, "revoked", False)
    assert store.accounts() == ["me@proton.me"]


def test_oversized_and_malformed_requests_are_refused(running_daemon):
    store, api, _ = running_daemon
    for payload in (b"x" * (control.MAX_REQUEST + 10), b"not json\n", b"[1]\n"):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(5)
            conn.connect(str(control.socket_path()))
            conn.sendall(payload)
            assert json.loads(conn.recv(4096)) == {"error": "invalid request"}
    assert store.accounts() == ["me@proton.me"]


# ------------------------------------------------------ tickets always answer


def test_a_request_that_waited_too_long_never_runs(tmp_path):
    store = make_store(tmp_path)
    store.save(Session(uid="u1", access_token="a", refresh_token="r", email="me@proton.me"))
    daemon = make_daemon(store, tmp_path, shared_api_factory(FakeProton()))
    daemon._sync_accounts()
    ticket = control.Ticket()
    assert ticket.wait(0.01) == {"error": "busy"}  # the caller gave up
    daemon.handle({"cmd": "logout", "account": None, "local": False, "ticket": ticket})
    assert store.accounts() == ["me@proton.me"]


def test_a_failing_request_is_still_answered(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    daemon = make_daemon(store, tmp_path, shared_api_factory(FakeProton()))

    def broken(*a, **kw):
        raise RuntimeError("bug")

    monkeypatch.setattr(daemon, "_logout", broken)
    ticket = control.Ticket()
    with pytest.raises(RuntimeError):
        daemon.handle({"cmd": "logout", "account": None, "local": False, "ticket": ticket})
    assert ticket.wait(0) == {"error": "internal error"}


# ------------------------------------------------- sign-in through the owner


def auth_daemon(store, tmp_path, server):
    def factory(session, on_refresh=None, **kw):
        return ProtonClient(session, on_refresh=on_refresh, http=server)

    return make_daemon(store, tmp_path, factory)


def test_sign_in_replacement_ends_the_old_session_with_the_owners_tokens(tmp_path, monkeypatch):
    """Codex's P1: the daemon renewed the old session but has not saved the
    new tokens yet when a sign-in replaces it. The saved copy is consumed;
    only the daemon's in-memory tokens can still end that session."""
    server = AuthServer(uid="old")
    store = make_store(tmp_path)
    store.save(
        Session(
            uid="old",
            access_token=server.access,
            refresh_token=server.refresh,
            email="old@example.com",
            user_id="U",
        )
    )
    daemon = auth_daemon(store, tmp_path, server)
    daemon._sync_accounts()

    def refuse(session):
        raise StoreUnavailable("keyring busy")

    monkeypatch.setattr(store, "update_tokens", refuse)
    daemon.accounts["old@example.com"].client.refresh()  # rotated on Proton, not saved
    stale = store.load("old@example.com")
    assert stale.refresh_token != server.refresh
    # What 31958e5's CLI did: end it from the saved copy. Proton rejects it,
    # and the live session would have been left behind.
    assert sessions.end_displaced(ProtonClient(stale, http=server)) == REJECTED
    assert server.revoked is False
    monkeypatch.undo()

    new = Session(uid="new", access_token="n1", refresh_token="n2", email="me@proton.me", user_id="U")
    ticket = control.Ticket()
    daemon.handle({"cmd": "adopt", "session": new, "ticket": ticket})
    assert ticket.result["saved"] is True
    assert ticket.result["displaced"] == [{"email": "old@example.com", "outcome": REVOKED}]
    assert server.revoked is True
    assert store.accounts() == ["me@proton.me"]
    assert daemon.accounts["me@proton.me"].client.session.uid == "new"
    assert "old@example.com" not in daemon.accounts


def pending_sign_in(monkeypatch, uid="u2", user_id=""):
    session = Session(uid=uid, access_token="n1", refresh_token="n2", email="", user_id=user_id)
    monkeypatch.setattr(cli, "begin_login", lambda c, u, p, hv: PendingLogin(session, 0))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "pw")
    revoked = []
    monkeypatch.setattr(cli, "logout", lambda client: revoked.append(client.session.uid) or True)
    return revoked


def test_cli_sign_in_is_handed_to_the_running_daemon(running_daemon, monkeypatch):
    store, api, daemon = running_daemon
    revoked_by_cli = pending_sign_in(monkeypatch)
    assert cli._login("me@proton.me") == "me@proton.me"
    assert revoked_by_cli == []  # the CLI never touched the old session
    assert api.revoked is True  # the daemon ended it with its own client
    assert store.load("me@proton.me").uid == "u2"
    assert daemon.accounts["me@proton.me"].client.session.uid == "u2"


def test_sign_in_is_refused_when_the_owner_does_not_answer(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.save(Session(uid="u1", access_token="a", refresh_token="r", email="me@proton.me"))
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    revoked = pending_sign_in(monkeypatch)
    held = open_private(runtime_dir() / "daemon.lock")
    fcntl.flock(held, fcntl.LOCK_EX)  # a daemon holds the lock but has no socket
    try:
        with pytest.raises(cli.LoginFailed, match="did not answer"):
            cli._login("me@proton.me")
    finally:
        os.close(held)
    assert revoked == ["u2"]  # the new session is not left behind on Proton
    assert store.load("me@proton.me").uid == "u1"


def test_without_a_bar_sign_in_replaces_under_the_lock(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.save(Session(uid="u1", access_token="a", refresh_token="r", email="me@proton.me"))
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    pending_sign_in(monkeypatch)
    ended = []

    def end_displaced(client):
        fd = open_private(runtime_dir() / "daemon.lock")
        try:
            with pytest.raises(BlockingIOError):  # the bar cannot start meanwhile
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        ended.append(client.session.uid)
        return REJECTED

    monkeypatch.setattr(sessions, "end_displaced", end_displaced)
    assert cli._login("me@proton.me") == "me@proton.me"
    assert ended == ["u1"]
    assert store.load("me@proton.me").uid == "u2"


@pytest.mark.parametrize(
    ("reply", "saved"),
    [({"error": "busy"}, False), ({"error": "no answer"}, False), ({"error": "no answer"}, True)],
)
def test_an_unanswered_hand_over_trusts_only_the_keyring(tmp_path, monkeypatch, capsys, reply, saved):
    store = make_store(tmp_path)
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    revoked = pending_sign_in(monkeypatch)

    def ask(request):
        if saved:  # it ran before the connection broke
            store.replace(Session.from_dict(request["session"]))
        return dict(reply)

    monkeypatch.setattr(control, "ask", ask)
    if saved:
        assert cli._login("me@proton.me") == "me@proton.me"
        assert revoked == []
        assert "could not confirm whether an older session" in capsys.readouterr().err
    else:
        with pytest.raises(cli.LoginFailed):
            cli._login("me@proton.me")
        assert revoked == ["u2"]
        assert store.accounts() == []


# ------------------------------------------------------------------- N08


def test_new_sign_in_is_authoritative_even_if_old_entry_cannot_be_deleted(tmp_path):
    store = make_store(tmp_path)
    store.save(Session(uid="u1", access_token="a", refresh_token="r", email="old@example.com", user_id="U"))

    def refuse(key):
        raise StoreUnavailable("keyring locked")

    store.secrets.delete = refuse
    displaced = store.replace(
        Session(uid="u2", access_token="a", refresh_token="r", email="new@proton.me", user_id="U")
    )
    assert store.accounts() == ["new@proton.me"]
    assert [s.uid for s in displaced] == ["u1"]  # still revoked by the caller
    assert store.cleanup_failures == ["old@example.com"]


def test_daemon_prefers_the_newer_sign_in_of_one_account(tmp_path):
    store = make_store(tmp_path)
    old = Session(
        uid="u1", access_token="a", refresh_token="r", email="old@example.com", user_id="U", created_at=1
    )
    new = Session(
        uid="u2", access_token="a", refresh_token="r", email="new@proton.me", user_id="U", created_at=2
    )
    store.secrets.set("session:old@example.com", json.dumps(old.to_dict()))
    store.secrets.set("session:new@proton.me", json.dumps(new.to_dict()))
    store._write_index(["old@example.com", "new@proton.me"])
    store._bump()

    def factory(session, **kw):
        api = FakeProton()
        api.session = session
        return api

    daemon = make_daemon(store, tmp_path, factory)
    daemon._sync_accounts()
    assert list(daemon.accounts) == ["new@proton.me"]
