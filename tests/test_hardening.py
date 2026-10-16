"""Regression tests for the public-release review (Codex, 16 Oct 2026)."""

from __future__ import annotations

import argparse
import gzip
import http.client
import io
import os
import stat

import pytest
from fakes import NOW, Clock, FakeProton, MemorySecrets
from test_daemon import RecordingNotifier
from test_transport import FakeConnection

from promarchy import cli, transport
from promarchy.api import AuthExpired, NetworkError, ProtonClient, RateLimited, Session, checked_token
from promarchy.avatars import Avatars
from promarchy.daemon import DEMO_INTERVAL, Daemon
from promarchy.log import log_error
from promarchy.notify import Watermarks
from promarchy.paths import UnsafePath, ensure_private_dir, write_private
from promarchy.store import SessionStore
from promarchy.transport import HttpTransport, TransportError, gunzip_bounded

SECRET = "SECRETTOKENVALUE"


def session(email="me@proton.me", uid="uid1", access="acc", refresh="ref"):
    return Session(uid=uid, access_token=access, refresh_token=refresh, email=email)


def make_store(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    return SessionStore(secrets=MemorySecrets(), root=root)


@pytest.fixture
def daemon_env(tmp_path):
    clock = Clock()
    api = FakeProton(clock=clock)
    store = make_store(tmp_path)
    out = io.StringIO()

    def factory(s, on_refresh=None):
        api.session = s
        return api

    d = Daemon(
        store,
        out=out,
        notifier=RecordingNotifier(),
        watermarks=Watermarks(tmp_path / "n.json"),
        avatars=Avatars(root=tmp_path / "logos"),
        clock=clock,
        mono=clock,
        client_factory=factory,
        state_path=str(tmp_path / "state.json"),
    )
    import json

    return d, api, store, clock, (lambda: [json.loads(x) for x in out.getvalue().splitlines()])


# ---------------------------------------------------------------- F01 secrets


def test_logged_errors_never_include_the_message(capsys):
    try:
        raise ValueError(f"Invalid header value b'Bearer {SECRET}\\n'")
    except ValueError as exc:
        log_error("polling", exc)
    err = capsys.readouterr().err
    assert "ValueError" in err and "test_hardening.py" in err
    assert SECRET not in err


def test_malformed_stored_tokens_are_rejected_without_echo():
    with pytest.raises(ValueError) as err:
        checked_token(f"{SECRET}\nInjected: header")
    assert SECRET not in str(err.value)
    with pytest.raises(ValueError):
        Session.from_dict({"uid": "u", "access_token": "a b", "refresh_token": "r"})


def test_corrupted_keyring_value_is_treated_as_missing(tmp_path):
    store = make_store(tmp_path)
    store.secrets.set("session:me@proton.me", '{"uid": "u", "access_token": "x\\ny", "refresh_token": "r"}')
    assert store.load("me@proton.me") is None


def test_bad_header_error_does_not_quote_the_header(monkeypatch):
    FakeConnection.instances, FakeConnection.script = [], []
    monkeypatch.setattr(http.client, "HTTPSConnection", FakeConnection)

    def reject(self, method, path, body=None, headers=None):
        raise ValueError(f"Invalid header value {headers['Authorization']!r}")

    monkeypatch.setattr(FakeConnection, "request", reject)
    with pytest.raises(TransportError) as err:
        HttpTransport().request("GET", "https://mail-api.proton.me/x", headers={"Authorization": SECRET})
    assert SECRET not in str(err.value) and err.value.__cause__ is None


def test_captcha_link_is_not_printed(monkeypatch, capsys):
    from promarchy.api import HumanVerificationRequired

    monkeypatch.setattr(cli, "open_in_browser", lambda url: None)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    details = {"HumanVerificationToken": SECRET, "HumanVerificationMethods": ["captcha"]}
    cli._human_verification(None, HumanVerificationRequired("hv", code=9001, details=details))
    out = capsys.readouterr()
    assert SECRET not in out.out and SECRET not in out.err


# --------------------------------------------------------- F02/F03 lifecycle


class FakeClient:
    """ProtonClient stand-in for logout: DELETE succeeds, fails or is offline."""

    outcome = "ok"
    revoked: list[str] = []

    def __init__(self, s=None, on_refresh=None, **kw):
        self.session = s

    def call(self, method, path, **kw):
        if self.outcome == "offline":
            raise NetworkError("offline")
        if self.outcome == "dead":
            raise AuthExpired("dead")
        FakeClient.revoked.append(self.session.uid if self.session else "?")
        return {"Code": 1000}


@pytest.fixture
def cli_store(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    monkeypatch.setattr(cli, "ProtonClient", FakeClient)
    FakeClient.revoked = []
    FakeClient.outcome = "ok"
    return store


def logout_args(**kw):
    return argparse.Namespace(**{"email": None, "all": True, "local": False, **kw})


def test_logout_offline_keeps_the_session_and_fails(cli_store, capsys):
    cli_store.save(session())
    FakeClient.outcome = "offline"
    assert cli.cmd_logout(logout_args()) == 1
    assert cli_store.accounts() == ["me@proton.me"]
    assert "still signed in" in capsys.readouterr().err


def test_logout_local_forgets_even_when_offline(cli_store, capsys):
    cli_store.save(session())
    FakeClient.outcome = "offline"
    assert cli.cmd_logout(logout_args(local=True)) == 0
    assert cli_store.accounts() == []
    assert "could not be ended" in capsys.readouterr().out


def test_logout_of_an_already_dead_session_succeeds(cli_store):
    cli_store.save(session())
    FakeClient.outcome = "dead"
    assert cli.cmd_logout(logout_args()) == 0
    assert cli_store.accounts() == []


def test_logout_does_not_throw_away_a_newer_sign_in(cli_store):
    cli_store.save(session(uid="old"))
    assert cli_store.remove("me@proton.me", expected_uid="old") is True
    cli_store.save(session(uid="new"))
    assert cli_store.remove("me@proton.me", expected_uid="old") is False
    assert cli_store.load("me@proton.me").uid == "new"


def test_keyring_delete_failure_leaves_the_index_alone(tmp_path):
    store = make_store(tmp_path)
    store.save(session())

    def broken(key):
        raise cli.StoreUnavailable("keyring locked")

    store.secrets.delete = broken
    with pytest.raises(cli.StoreUnavailable):
        store.remove("me@proton.me")
    assert store.accounts() == ["me@proton.me"]


def test_replace_returns_exactly_the_displaced_session(tmp_path):
    store = make_store(tmp_path)
    assert store.replace(session(uid="a")) == []
    assert [s.uid for s in store.replace(session(uid="b"))] == ["a"]
    assert store.replace(session(uid="b")) == []  # same session: nothing displaced


def test_ctrl_c_during_two_factor_revokes_the_pending_session(cli_store, monkeypatch):
    from promarchy.auth import PendingLogin

    revoked = []
    monkeypatch.setattr(cli, "begin_login", lambda c, u, p, hv: PendingLogin(session(uid="pending"), 1))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "pw")
    monkeypatch.setattr(cli, "logout", lambda client: revoked.append(client.session.uid) or True)

    def interrupt(client):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_two_factor", interrupt)
    with pytest.raises(KeyboardInterrupt):
        cli._login("me@proton.me")
    assert revoked == ["pending"]
    assert cli_store.accounts() == []


def test_daemon_sign_out_only_forgets_after_revocation(daemon_env, monkeypatch):
    from promarchy import daemon as daemon_module

    d, api, store, clock, states = daemon_env
    store.save(session(uid="uid1"))
    d.tick()
    monkeypatch.setattr(daemon_module, "end_session", lambda client, reload: "unconfirmed")
    d.handle({"cmd": "logout"})
    assert store.accounts() == ["me@proton.me"]
    assert "Could not sign out" in states()[-1]["message"]
    monkeypatch.setattr(daemon_module, "end_session", lambda client, reload: "revoked")
    d.handle({"cmd": "logout"})
    assert store.accounts() == []
    assert states()[-1]["status"] == "signed-out"


# ----------------------------------------------------------- F04 gzip bound


def test_decompression_bomb_is_refused():
    bomb = gzip.compress(b"\0" * (transport.MAX_BODY + 1))
    assert len(bomb) < 64 * 1024
    with pytest.raises(TransportError, match="too large"):
        gunzip_bounded(bomb, transport.MAX_BODY)


def test_concatenated_gzip_members_are_bounded_together():
    two = gzip.compress(b"a" * 600) + gzip.compress(b"b" * 600)
    assert gunzip_bounded(two, 2000) == b"a" * 600 + b"b" * 600
    with pytest.raises(TransportError):
        gunzip_bounded(two, 1000)


def test_truncated_gzip_is_an_error():
    with pytest.raises(TransportError, match="truncated"):
        gunzip_bounded(gzip.compress(b"x" * 1000)[:-12], 5000)


def test_logo_downloads_have_a_small_cap(daemon_env):
    d, api, store, clock, states = daemon_env
    seen = {}

    def spy(path, *, params=None, max_bytes):
        seen["max"] = max_bytes
        return 200, "image/png", b"png"

    api.fetch_bytes = spy
    api.add(60, logo=True)
    store.save(session())
    d.tick()
    assert seen["max"] <= 512 * 1024


# --------------------------------------------------------- F07 retry deadlines


def test_opening_the_menu_cannot_override_a_rate_limit(daemon_env):
    d, api, store, clock, states = daemon_env
    store.save(session())
    d.tick()
    api.fail_with = RateLimited("slow down", 120)
    clock.advance(31)
    d.tick()
    api.fail_with = None
    calls = len(api.calls)
    d.handle({"cmd": "poll"})
    d.handle({"cmd": "refresh"})
    d.tick()
    assert len(api.calls) == calls  # both waited for Retry-After
    clock.advance(121)
    d.tick()
    assert len(api.calls) > calls


def test_menu_polls_respect_backoff_but_refresh_may_retry(daemon_env):
    d, api, store, clock, states = daemon_env
    store.save(session())
    d.tick()
    api.fail_with = NetworkError("offline")
    clock.advance(31)
    d.tick()
    api.fail_with = None
    calls = len(api.calls)
    d.handle({"cmd": "poll"})
    d.tick()
    assert len(api.calls) == calls
    d.handle({"cmd": "refresh"})
    d.tick()
    assert len(api.calls) > calls


def test_logo_pass_stops_and_pauses_on_rate_limit(daemon_env):
    d, api, store, clock, states = daemon_env
    for i in range(6):
        api.add(60 + i, logo=True, address=f"s{i}@example.com", conversation=f"c{i}")
    api.logo_error = RateLimited("slow down", 300)
    store.save(session())
    d.tick()
    assert api.endpoint_calls("/core/v4/images/logo") == 1
    d.handle({"cmd": "refresh"})
    d.tick()
    assert api.endpoint_calls("/core/v4/images/logo") == 1  # paused, not hammering


# ------------------------------------------------------ F09 partial accounts


def test_one_failing_account_is_reported_not_hidden(daemon_env):
    d, api, store, clock, states = daemon_env
    store.save(session(email="a@proton.me", uid="ua"))
    store.save(session(email="b@proton.me", uid="ub"))
    d.tick()
    d.accounts["b@proton.me"].status = "auth-expired"
    d.accounts["b@proton.me"].error = "Your Proton session expired."
    d.emit()
    state = states()[-1]
    assert state["status"] == "partial"
    assert "b@proton.me" in state["message"]


# --------------------------------------------------------- F10 demo schedule


def test_demo_mode_sleeps_on_its_own_schedule(daemon_env):
    d, api, store, clock, states = daemon_env
    store.save(session())
    d.tick()
    d.accounts["me@proton.me"].next_poll = clock() - 100  # overdue real poll
    d.handle({"cmd": "configure", "settings": {"demo": True}})
    d.tick()
    first = states()[-1]
    assert d._sleep_for() == DEMO_INTERVAL
    clock.advance(DEMO_INTERVAL)
    d.tick()
    assert states()[-1] == first  # stable snapshot, nothing rewritten


# ----------------------------------------------------------- F11 file safety


def test_private_writes_never_follow_a_planted_symlink(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep me")
    target = tmp_path / "state.json"
    (tmp_path / ".state.json.tmp").symlink_to(victim)  # an old predictable name
    target.symlink_to(victim)
    write_private(target, "new")
    assert victim.read_text() == "keep me"
    assert not target.is_symlink() and target.read_text() == "new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_loose_directory_permissions_are_tightened(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o777)
    os.chmod(loose, 0o777)
    ensure_private_dir(loose)
    assert stat.S_IMODE(loose.stat().st_mode) == 0o700


def test_symlinked_state_directory_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(UnsafePath):
        ensure_private_dir(link)


# ------------------------------------------------------------- logo safety


def test_svg_logos_with_external_references_are_discarded(tmp_path):
    avatars = Avatars(root=tmp_path / "logos")

    class Client:
        def fetch_bytes(self, path, *, params=None, max_bytes):
            return 200, "image/svg+xml", b'<svg><image href="file:///etc/passwd"/></svg>'

    row = {"senderImage": True, "senderAddress": "x@example.com"}
    avatars.attach([row], Client())
    assert row.get("avatar", "") == ""
    assert list((tmp_path / "logos").glob("*.svg")) == []


def test_expired_logos_are_pruned(tmp_path):
    clock = Clock(NOW)
    avatars = Avatars(root=tmp_path / "logos", clock=clock)
    old = tmp_path / "logos" / "abc.png"
    old.write_bytes(b"png")
    os.utime(old, (NOW - 30 * 86400, NOW - 30 * 86400))
    fresh = tmp_path / "logos" / "def.png"
    fresh.write_bytes(b"png")
    os.utime(fresh, (NOW, NOW))
    assert avatars.prune() == 1
    assert not old.exists() and fresh.exists()


def test_proton_client_is_still_importable_without_extras():
    assert ProtonClient().base.startswith("https://")
