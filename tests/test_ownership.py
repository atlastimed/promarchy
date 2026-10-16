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
from promarchy.store import SaveIndeterminate, SecretToolBackend, SessionStore, StoreUnavailable


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
    ("reply", "kept"),
    [
        ({"error": "busy"}, False),  # it never ran and never will: revoke
        ({"saved": False, "unchanged": True, "error": "keyring locked"}, False),  # nothing written
        ({"saved": False, "indeterminate": True, "error": "timed out"}, True),  # may have landed
        ({"saved": False, "error": "from an older bar"}, True),  # not stated: assume it may have
        ({"error": "no answer"}, True),  # unknown: never revoke on a guess
    ],
)
def test_the_new_session_is_revoked_only_when_it_was_certainly_not_saved(tmp_path, monkeypatch, reply, kept):
    store = make_store(tmp_path)
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    revoked = pending_sign_in(monkeypatch)
    monkeypatch.setattr(control, "ask", lambda request: dict(reply))
    with pytest.raises(cli.LoginFailed) as failure:
        cli._login("me@proton.me")
    assert revoked == ([] if kept else ["u2"])
    if kept:
        assert "did not confirm" in str(failure.value)


def test_a_lost_answer_after_the_bar_saved_is_a_success(tmp_path, monkeypatch, capsys):
    store = make_store(tmp_path)
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    revoked = pending_sign_in(monkeypatch)

    def ask(request):
        store.replace(Session.from_dict(request["session"]))  # it ran, then the connection broke
        return {"error": "no answer"}

    monkeypatch.setattr(control, "ask", ask)
    assert cli._login("me@proton.me") == "me@proton.me"
    assert revoked == []
    assert "could not confirm whether an older session" in capsys.readouterr().err


def test_ctrl_c_right_after_the_hand_over_keeps_the_new_session(tmp_path, monkeypatch):
    """F2: SIGINT delivered the moment the handler is restored."""
    store = make_store(tmp_path)
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    revoked = pending_sign_in(monkeypatch)

    @contextlib.contextmanager
    def interrupted_on_exit():
        yield
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_uninterruptible", interrupted_on_exit)
    with pytest.raises(KeyboardInterrupt):
        cli._login("me@proton.me")
    assert revoked == []
    assert store.load("me@proton.me").uid == "u2"


def test_a_rejected_replaced_session_is_reported(tmp_path, monkeypatch, capsys):
    """F4: rejected credentials cannot prove the old session ended."""
    store = make_store(tmp_path)
    store.save(Session(uid="u1", access_token="a", refresh_token="r", email="me@proton.me"))
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    pending_sign_in(monkeypatch)
    monkeypatch.setattr(sessions, "end_displaced", lambda client: REJECTED)
    assert cli._login("me@proton.me") == "me@proton.me"
    assert "cannot confirm it ended" in capsys.readouterr().err


# ------------------------------------------------------- adopt consistency (F3, F5)


@pytest.mark.parametrize("field", ["uid", "access_token", "refresh_token"])
def test_adopt_refuses_null_or_non_string_tokens(field):
    good = {"uid": "u2", "access_token": "a", "refresh_token": "r", "email": "me@proton.me"}
    for bad in (None, 17, ["x"]):
        with pytest.raises(control.BadRequest):
            control.parse(json.dumps({"cmd": "adopt", "session": {**good, field: bad}}).encode())


def test_a_case_variant_address_replaces_rather_than_deletes(tmp_path):
    store = make_store(tmp_path)
    store.save(Session(uid="u1", access_token="a", refresh_token="r", email="me@proton.me", user_id="U"))
    displaced = store.replace(
        Session(uid="u2", access_token="a", refresh_token="r", email="Me@Proton.ME", user_id="U")
    )
    assert store.accounts() == ["me@proton.me"]
    assert store.load("me@proton.me").uid == "u2"
    assert [s.uid for s in displaced] == ["u1"]


def test_the_same_session_under_another_address_is_never_revoked(tmp_path):
    store = make_store(tmp_path)
    store.save(Session(uid="X", access_token="a", refresh_token="r", email="old@example.com", user_id="U"))
    ended = []
    result = sessions.adopt(
        store,
        Session(uid="X", access_token="a", refresh_token="r", email="new@proton.me", user_id="U"),
        lambda old: ended.append(old.uid) or REVOKED,
    )
    assert ended == [] and result["displaced"] == []
    assert store.accounts() == ["new@proton.me"]


def test_adopt_switches_accounts_even_if_the_keyring_cannot_be_read_afterwards(tmp_path):
    """F5: the bar must not keep polling the session it just ended."""
    store = make_store(tmp_path)
    store.save(Session(uid="u1", access_token="a", refresh_token="r", email="old@example.com", user_id="U"))
    daemon = make_daemon(store, tmp_path, shared_api_factory(FakeProton()))
    daemon._sync_accounts()
    real_replace, real_get = store.replace, store.secrets.get
    broken = {"now": False}

    def replace_then_break(session, **kw):
        result = real_replace(session, **kw)
        broken["now"] = True
        return result

    def get(key):
        if broken["now"]:
            raise StoreUnavailable("keyring locked")
        return real_get(key)

    store.replace = replace_then_break
    store.secrets.get = get
    new = Session(uid="u2", access_token="n1", refresh_token="n2", email="me@proton.me", user_id="U")
    ticket = control.Ticket()
    daemon.handle({"cmd": "adopt", "session": new, "ticket": ticket})
    assert ticket.result["saved"] is True
    assert list(daemon.accounts) == ["me@proton.me"]
    assert daemon.accounts["me@proton.me"].client.session.uid == "u2"


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


# ----------------------------------------- GPT 6 Pro design review (A to D)


def test_a_keyring_helper_that_times_out_mid_write_is_indeterminate(monkeypatch):
    backend = SecretToolBackend.__new__(SecretToolBackend)
    backend.tool = "/usr/bin/secret-tool"

    def run(*args, secret=None):
        if args[0] == "store":
            raise StoreUnavailable("Could not talk to the keyring: TimeoutExpired")
        raise AssertionError("no cleanup after an indeterminate write")

    monkeypatch.setattr(backend, "_run", run)
    with pytest.raises(SaveIndeterminate):
        backend.set("session:x", "{}")


def test_failed_legacy_cleanup_does_not_turn_a_save_into_a_failure(monkeypatch):
    import subprocess

    backend = SecretToolBackend.__new__(SecretToolBackend)
    backend.tool = "/usr/bin/secret-tool"

    def run(*args, secret=None):
        if args[0] == "store":
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")
        raise StoreUnavailable("Could not talk to the keyring: TimeoutExpired")

    monkeypatch.setattr(backend, "_run", run)
    backend.set("session:x", "{}")  # no exception: the session is stored


def test_after_the_save_lands_replace_reports_leftovers_instead_of_raising(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.save(Session(uid="u1", access_token="a", refresh_token="r", email="old@example.com", user_id="U"))

    def broken(accounts):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write_index", broken)
    displaced = store.replace(
        Session(uid="u2", access_token="a", refresh_token="r", email="me@proton.me", user_id="U")
    )
    assert [s.uid for s in displaced] == ["u1"]  # still returned for revocation
    assert store.index_failed is True
    assert store.load("me@proton.me").uid == "u2"


def test_a_write_that_may_have_landed_is_never_called_unchanged(tmp_path):
    store = make_store(tmp_path)

    def flaky(key, value):
        raise RuntimeError("helper crashed")

    store.secrets.set = flaky
    with pytest.raises(SaveIndeterminate):
        store.replace(Session(uid="u2", access_token="a", refresh_token="r", email="me@proton.me"))


def test_the_bar_says_whether_a_failed_adopt_changed_anything(tmp_path):
    store = make_store(tmp_path)
    daemon = make_daemon(store, tmp_path, shared_api_factory(FakeProton()))
    new = Session(uid="u2", access_token="n1", refresh_token="n2", email="me@proton.me")

    def refuse(key, value):
        raise StoreUnavailable("keyring locked")

    def crash(key, value):
        raise SaveIndeterminate("timed out")

    for failure, expected in ((refuse, "unchanged"), (crash, "indeterminate")):
        store.secrets.set = failure
        ticket = control.Ticket()
        daemon.handle({"cmd": "adopt", "session": new, "ticket": ticket})
        assert ticket.result["saved"] is False and ticket.result.get(expected) is True


def test_a_held_session_is_ended_even_if_its_saved_copy_cannot_be_read(tmp_path):
    """B: the bar holds the old session, but the keyring returns nothing for it."""
    server = AuthServer(uid="old")
    store = make_store(tmp_path)
    store.save(
        Session(uid="old", access_token=server.access, refresh_token=server.refresh, email="me@proton.me")
    )
    daemon = auth_daemon(store, tmp_path, server)
    daemon._sync_accounts()
    store.secrets.data.clear()  # a dismissed unlock prompt looks just like this
    new = Session(uid="new", access_token="n1", refresh_token="n2", email="me@proton.me")
    ticket = control.Ticket()
    daemon.handle({"cmd": "adopt", "session": new, "ticket": ticket})
    assert ticket.result["displaced"] == [{"email": "me@proton.me", "outcome": REVOKED}]
    assert server.revoked is True


def test_renewed_tokens_are_never_written_over_an_unreadable_entry(tmp_path):
    """B: no blind writes; the caller keeps the tokens and retries."""
    store = make_store(tmp_path)
    store.save(Session(uid="u1", access_token="a", refresh_token="r", email="me@proton.me"))
    store.secrets.data.clear()
    with pytest.raises(StoreUnavailable):
        store.update_tokens(Session(uid="u1", access_token="a2", refresh_token="r2", email="me@proton.me"))
    assert store.secrets.data == {}


def test_a_repeat_of_a_held_session_keeps_the_bars_newer_tokens(tmp_path):
    store = make_store(tmp_path)
    store.save(Session(uid="u1", access_token="a", refresh_token="r", email="me@proton.me"))
    daemon = make_daemon(store, tmp_path, shared_api_factory(FakeProton()))
    daemon._sync_accounts()
    daemon.accounts["me@proton.me"].client.session.refresh_token = "newer"
    ticket = control.Ticket()
    stale = Session(uid="u1", access_token="a", refresh_token="r", email="me@proton.me")
    daemon.handle({"cmd": "adopt", "session": stale, "ticket": ticket})
    assert ticket.result["saved"] is True and ticket.result["displaced"] == []
    assert daemon.accounts["me@proton.me"].client.session.refresh_token == "newer"


def test_an_abandoned_sign_in_that_cannot_be_ended_says_so(tmp_path, monkeypatch, capsys):
    """C: Ctrl+C at the 2FA prompt while Proton is unreachable."""
    store = make_store(tmp_path)
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    pending_sign_in(monkeypatch)
    monkeypatch.setattr(cli, "logout", lambda client: False)

    def interrupt(client):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_two_factor", interrupt)
    monkeypatch.setattr(cli, "begin_login", lambda c, u, p, hv: PendingLogin(Session("u2", "a", "r"), 1))
    with pytest.raises(KeyboardInterrupt):
        cli._login("me@proton.me")
    assert "could not be ended on Proton's side" in capsys.readouterr().err


def test_tokens_kept_only_in_memory_are_shown(tmp_path):
    """D: the bar must not look healthy while a restart would sign you out."""
    store = make_store(tmp_path)
    store.save(Session(uid="u1", access_token="a", refresh_token="r", email="me@proton.me"))
    daemon = make_daemon(store, tmp_path, shared_api_factory(FakeProton()))
    daemon.tick()
    assert daemon.snapshot()["status"] == "ok"

    def refuse(key, value):
        raise StoreUnavailable("keyring locked")

    store.secrets.set = refuse
    daemon.unsaved["me@proton.me"] = Session(
        uid="u1", access_token="a2", refresh_token="r2", email="me@proton.me"
    )
    daemon._persist_unsaved(force=True)
    state = daemon.snapshot()
    assert state["status"] == "partial"
    assert "not yet saved to the keyring" in state["message"]


# ------------------------------------------- final release review (P1, P2s)


def tool_backend(monkeypatch, run):
    backend = SecretToolBackend.__new__(SecretToolBackend)
    backend.tool = "/usr/bin/secret-tool"
    monkeypatch.setattr(backend, "_run", run)
    return backend


def completed(returncode, stdout="", stderr=""):
    import subprocess

    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def test_a_helper_killed_after_writing_is_indeterminate_not_a_refusal(monkeypatch):
    """P1-1: status -9 proves nothing about whether the item was stored."""
    for result in (completed(-9), completed(2)):
        backend = tool_backend(monkeypatch, lambda *a, secret=None, r=result: r)
        with pytest.raises(SaveIndeterminate):
            backend.set("session:x", "{}")
    refused = tool_backend(monkeypatch, lambda *a, secret=None: completed(1, stderr="Cannot create an item"))
    with pytest.raises(StoreUnavailable) as failure:
        refused.set("session:x", "{}")
    assert not isinstance(failure.value, SaveIndeterminate)


def test_a_leftover_from_an_older_version_never_shadows_the_current_item(monkeypatch):
    """P2-2: lookups prefer Promarchy's own item."""

    def run(*args, secret=None):
        if args[0] == "lookup" and "application" in args:
            return completed(0, stdout="current\n")
        if args[0] == "lookup":
            return completed(0, stdout="legacy\n")
        if args[0] == "clear":
            return completed(1, stderr="Locked")  # the legacy item survives
        return completed(0)

    backend = tool_backend(monkeypatch, run)
    backend.set("session:x", "current")  # a failed legacy clear is logged, not raised
    assert backend.get("session:x") == "current"


def test_older_items_are_still_read_when_there_is_no_current_one(monkeypatch):
    def run(*args, secret=None):
        if "application" in args:
            return completed(1)  # no match
        return completed(0, stdout="legacy\n")

    assert tool_backend(monkeypatch, run).get("session:x") == "legacy"


def test_the_unsaved_warning_survives_going_offline(tmp_path):
    """P2-1"""
    from promarchy.api import NetworkError

    store = make_store(tmp_path)
    store.save(Session(uid="u1", access_token="a", refresh_token="r", email="me@proton.me"))
    api = FakeProton()
    daemon = make_daemon(store, tmp_path, shared_api_factory(api))
    daemon.tick()

    def refuse(key, value):
        raise StoreUnavailable("keyring locked")

    store.secrets.set = refuse
    daemon.unsaved["me@proton.me"] = Session(
        uid="u1", access_token="a2", refresh_token="r2", email="me@proton.me"
    )
    daemon._persist_unsaved(force=True)
    api.fail_with = NetworkError("offline")
    daemon.accounts["me@proton.me"].next_poll = 0
    daemon.tick()
    state = daemon.snapshot()
    assert state["status"] == "offline"
    assert "not yet saved to the keyring" in state["message"]
