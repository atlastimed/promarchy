"""One owner per session: the running daemon signs out on the CLI's behalf.

These run a real Daemon.run() loop with a real Unix control socket inside the
isolated XDG runtime directory set up by conftest.py.
"""

from __future__ import annotations

import argparse
import fcntl
import io
import os
import threading
import time

import pytest
from fakes import FakeProton, MemorySecrets
from test_daemon import RecordingNotifier

from promarchy import cli
from promarchy.api import Session
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


@pytest.fixture
def running_daemon(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.save(Session(uid="u1", access_token="a", refresh_token="r", email="me@proton.me"))
    api = FakeProton()

    def factory(session, on_refresh=None, **kw):
        api.session = session
        return api

    daemon = Daemon(
        store,
        out=io.StringIO(),
        notifier=RecordingNotifier(),
        watermarks=Watermarks(tmp_path / "n.json"),
        avatars=Avatars(root=tmp_path / "logos"),
        client_factory=factory,
        state_path=str(tmp_path / "state.json"),
    )
    read_fd, write_fd = os.pipe()
    stdin = os.fdopen(read_fd)
    thread = threading.Thread(target=daemon.run, args=(stdin,), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not (runtime_dir() / "control.sock").exists():
        assert time.monotonic() < deadline, "control socket never appeared"
        time.sleep(0.02)
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    yield store, api
    os.close(write_fd)  # EOF on stdin: the daemon quits
    thread.join(timeout=5)
    assert not (runtime_dir() / "control.sock").exists()


def test_cli_logout_is_done_by_the_running_daemon(running_daemon, capsys):
    store, api = running_daemon
    assert cli.cmd_logout(args()) == 0
    assert getattr(api, "revoked", False) is True  # the daemon's own client revoked it
    assert store.accounts() == []
    assert "Signed out me@proton.me" in capsys.readouterr().out


def test_cli_waits_for_the_daemons_answer_when_proton_is_unreachable(running_daemon, capsys):
    from promarchy.api import NetworkError

    store, api = running_daemon
    api.fail_with = NetworkError("offline")
    assert cli.cmd_logout(args()) == 1
    assert store.accounts() == ["me@proton.me"]
    assert "still signed in" in capsys.readouterr().err


def test_control_socket_refuses_other_requests(running_daemon):
    reply = cli.ask_daemon({"cmd": "markRead", "ids": ["x"]})
    assert reply == {"error": "unsupported request"}


def test_without_a_daemon_the_cli_owns_the_lock(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.save(Session(uid="u1", access_token="a", refresh_token="r", email="me@proton.me"))
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    seen = {}

    def fake_local(store_, email, local):
        # While the CLI acts, nobody else can take the daemon lock.
        fd = open_private(runtime_dir() / "daemon.lock")
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        seen["called"] = True
        return {"email": email, "outcome": "revoked", "removed": True}

    monkeypatch.setattr(cli, "_logout_locally", fake_local)
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
    import json

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

    daemon = Daemon(
        store,
        out=io.StringIO(),
        notifier=RecordingNotifier(),
        watermarks=Watermarks(tmp_path / "n.json"),
        avatars=Avatars(root=tmp_path / "logos"),
        client_factory=factory,
        state_path=str(tmp_path / "state.json"),
    )
    daemon._sync_accounts()
    assert list(daemon.accounts) == ["new@proton.me"]
