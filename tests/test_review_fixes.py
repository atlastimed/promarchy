"""Regressions for the two code reviews of e6ed073 (November 2026).

Each test reproduces the reviewer's failure sequence and asserts the fixed
behaviour.
"""

from __future__ import annotations

import argparse
import json
import time

import pytest
from fakes import FakeProton, MemorySecrets
from test_daemon import RecordingNotifier
from test_hardening import FakeClient, daemon_env  # noqa: F401  (fixture)
from test_ownership import make_store, pending_sign_in
from test_rereview import Resp

from promarchy import auth, cli
from promarchy import store as store_module
from promarchy.api import AuthExpired, ProtonClient, ProtonError, RateLimited, Session
from promarchy.avatars import FETCH_BUDGET, Avatars
from promarchy.daemon import Daemon
from promarchy.mailbox import WEB_BASE, Mailbox
from promarchy.notify import Mark, Watermarks
from promarchy.store import SaveIndeterminate, SessionStore


def session(email="me@proton.me", uid="u1", user_id="", created_at=0.0):
    return Session(
        uid=uid, access_token="a", refresh_token="r", email=email, user_id=user_id, created_at=created_at
    )


# ------------------------------------------- store changes are never missed


def test_bumps_never_collide_even_when_the_clock_stands_still(tmp_path, monkeypatch):
    """A filesystem with coarse timestamps gave two quick changes the same
    stamp, so the bar missed the second (a sign-out, say)."""
    store = make_store(tmp_path)
    monkeypatch.setattr(store_module.time, "time_ns", lambda: 1)
    seen = [store.generation()]
    for _ in range(50):
        store._bump()
        seen.append(store.generation())
    assert seen == sorted(set(seen))  # strictly increasing


def test_an_empty_stamp_from_an_older_version_still_counts(tmp_path):
    store = make_store(tmp_path)
    store._stamp.write_text("")
    before = store.generation()  # the old timestamp-only stamp
    store._bump()
    assert store.generation() > before


def test_the_daemon_sees_a_sign_out_right_after_a_sign_in(daemon_env, monkeypatch):  # noqa: F811
    d, api, store, clock, states = daemon_env
    monkeypatch.setattr(store_module.time, "time_ns", lambda: 1)
    store.save(session())
    d.tick()
    store.save(session(email="other@proton.me", uid="u2"))
    d.tick()
    store.remove("other@proton.me")
    d.tick()
    assert list(d.accounts) == ["me@proton.me"]


# ------------------------------------------------- malformed sign-in replies


class FakeSRPUser:
    def __init__(self, password, modulus):
        pass

    def get_challenge(self):
        return b"challenge"

    def process_challenge(self, salt, ephemeral, version):
        return b"proof"

    def verify_session(self, proof):
        pass

    def authenticated(self):
        return True


class ScriptedSignIn:
    def __init__(self, info, response):
        self.info, self.response = info, response

    def call(self, method, path, **kw):
        return self.info if path == "/auth/v4/info" else self.response


GOOD_INFO = {"Modulus": "m", "Salt": "c2FsdA==", "ServerEphemeral": "ZXBo", "Version": 4, "SRPSession": "s"}
GOOD_AUTH = {"UID": "uid1", "AccessToken": "acc", "RefreshToken": "ref", "ServerProof": "cHJvb2Y="}


@pytest.fixture
def srp(monkeypatch):
    monkeypatch.setattr(auth, "_proton_srp", lambda: (FakeSRPUser, None, None))
    monkeypatch.setattr(auth, "verify_modulus", lambda armored: b"modulus")


def test_a_well_formed_sign_in_still_works(srp):
    pending = auth.begin_login(ScriptedSignIn(GOOD_INFO, GOOD_AUTH), "me@proton.me", "pw")
    assert (pending.session.uid, pending.session.access_token) == ("uid1", "acc")


@pytest.mark.parametrize(
    ("info", "response"),
    [
        ({**GOOD_INFO, "Salt": "not base64!"}, GOOD_AUTH),
        ({k: v for k, v in GOOD_INFO.items() if k != "ServerEphemeral"}, GOOD_AUTH),
        ({**GOOD_INFO, "Version": "four"}, GOOD_AUTH),
        (GOOD_INFO, {**GOOD_AUTH, "ServerProof": "abc"}),
        (GOOD_INFO, {**GOOD_AUTH, "UID": None}),
        (GOOD_INFO, {**GOOD_AUTH, "AccessToken": "bad token\r\nX-Injected: 1"}),
        (GOOD_INFO, {k: v for k, v in GOOD_AUTH.items() if k != "RefreshToken"}),
        (GOOD_INFO, {**GOOD_AUTH, "2FA": "yes"}),
    ],
)
def test_malformed_sign_in_replies_fail_cleanly(srp, info, response):
    """A traceback before; now the sign-in reports a normal failure."""
    with pytest.raises(ProtonError, match="unexpected sign-in response"):
        auth.begin_login(ScriptedSignIn(info, response), "me@proton.me", "pw")


# -------------------------------------------------- small daemon contracts


def test_mark_read_needs_a_list_of_ids(daemon_env):  # noqa: F811
    d, api, store, clock, states = daemon_env
    store.save(session())
    d.tick()
    before = len(api.calls)
    d.handle({"cmd": "markRead", "account": "me@proton.me", "ids": "m1"})  # not split into "m", "1"
    assert [c for c in api.calls[before:] if c[0] == "PUT"] == []


def with_session(s, **kw):
    api = FakeProton()
    api.session = s
    return api


def test_an_evicted_twin_forgets_its_notification_watermark(tmp_path):
    store = make_store(tmp_path)
    old = session(email="old@example.com", uid="u1", user_id="U", created_at=1)
    store.secrets.set("session:old@example.com", json.dumps(old.to_dict()))
    store._write_index(["old@example.com"])
    store._bump()
    watermarks = Watermarks(tmp_path / "n.json")
    daemon = Daemon(
        store,
        out=__import__("io").StringIO(),
        notifier=RecordingNotifier(),
        watermarks=watermarks,
        avatars=Avatars(root=tmp_path / "logos"),
        client_factory=with_session,
        state_path=str(tmp_path / "state.json"),
    )
    daemon._sync_accounts()
    watermarks.set("old@example.com", Mark(time=123, seen={"m1": 123}))
    new = session(email="new@proton.me", uid="u2", user_id="U", created_at=2)
    store.secrets.set("session:new@proton.me", json.dumps(new.to_dict()))
    store._write_index(["old@example.com", "new@proton.me"])
    store._bump()
    daemon._sync_accounts()
    assert list(daemon.accounts) == ["new@proton.me"]
    assert watermarks.get("old@example.com") is None


def test_a_refresh_refused_as_wrong_credentials_means_sign_in_again():
    class Http:
        headers: dict = {}

        def request(self, method, url, **kw):
            if url.endswith("/auth/v4/refresh"):
                return Resp(500, {"Code": 8002, "Error": "Incorrect login credentials"})
            return Resp(401, {"Code": 401, "Error": "Invalid access token"})

    client = ProtonClient(session(), http=Http())
    with pytest.raises(AuthExpired):
        client.call("GET", "/mail/v4/messages")


def test_bare_logout_with_several_accounts_asks_which(tmp_path, monkeypatch, capsys):
    store = make_store(tmp_path)
    store.save(session(email="a@proton.me", uid="u1"))
    store.save(session(email="b@proton.me", uid="u2"))
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    assert cli.cmd_logout(argparse.Namespace(email=None, all=False, local=False)) == 1
    assert "Several accounts are signed in" in capsys.readouterr().err
    assert store.accounts() == ["a@proton.me", "b@proton.me"]


# ------------------------------- a sign-in is saved *and* registered, or unconfirmed


class LandsThenDies(MemorySecrets):
    """secret-tool stores the item, then is killed before it can say so."""

    def __init__(self, failures=0):
        super().__init__()
        self.failures = failures

    def set(self, key, value):
        super().set(key, value)
        if self.failures:
            self.failures -= 1
            raise SaveIndeterminate("secret-tool ended with status -9")


class DiesFirst(MemorySecrets):
    """The first write never lands, and its outcome is unknown."""

    def __init__(self, failures=1):
        super().__init__()
        self.failures = failures

    def set(self, key, value):
        if self.failures:
            self.failures -= 1
            raise SaveIndeterminate("timed out")
        super().set(key, value)


def store_with(tmp_path, secrets):
    root = tmp_path / "state"
    root.mkdir()
    return SessionStore(secrets=secrets, root=root)


def test_a_write_confirmed_by_reading_it_back_is_fully_registered(tmp_path):
    store = store_with(tmp_path, LandsThenDies())
    store.save(session(uid="u1"))
    store.secrets.failures = 1
    displaced = store.replace(session(uid="u2"))
    assert store.accounts() == ["me@proton.me"] and store.load("me@proton.me").uid == "u2"
    assert [s.uid for s in displaced] == ["u1"]  # still ended by the owner


def test_standalone_sign_in_after_an_interrupted_write_is_discoverable(tmp_path, monkeypatch, capsys):
    """The reviewer saw "Signed in", then an empty account list, and
    `logout --all` saying "Not signed in" while the session sat in the keyring."""
    store = store_with(tmp_path, LandsThenDies(failures=1))
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    monkeypatch.setattr(cli, "ProtonClient", FakeClient)
    FakeClient.revoked, FakeClient.outcome = [], "ok"
    revoked = pending_sign_in(monkeypatch, uid="u2")
    assert cli._login("new@example.com") == "new@example.com"
    assert revoked == [] and store.accounts() == ["new@example.com"]
    assert cli.cmd_logout(argparse.Namespace(email=None, all=True, local=False)) == 0
    assert FakeClient.revoked == ["u2"] and store.accounts() == []


def test_an_unknown_first_write_is_retried_and_confirmed(tmp_path, monkeypatch, capsys):
    secrets = DiesFirst(failures=0)
    store = store_with(tmp_path, secrets)
    store.save(session(uid="u1"))  # what the sign-in replaces
    secrets.failures = 1
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    monkeypatch.setattr(cli, "ProtonClient", lambda *a, **kw: FakeProton())
    revoked = pending_sign_in(monkeypatch, uid="u2")
    assert cli._login("me@proton.me") == "me@proton.me"
    assert revoked == [] and store.accounts() == ["me@proton.me"]
    assert store.load("me@proton.me").uid == "u2"
    assert "could not confirm whether an older session" in capsys.readouterr().err


def test_a_first_sign_in_whose_save_cannot_be_traced_is_kept_and_reported(tmp_path, monkeypatch):
    """With nothing saved before, an empty account after a lost save could
    also mean a sign-out meanwhile: keep the session and say so, never guess."""
    store = store_with(tmp_path, DiesFirst())
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    revoked = pending_sign_in(monkeypatch, uid="u2")
    with pytest.raises(cli.LoginFailed, match="could not confirm the keyring saved"):
        cli._login("me@proton.me")
    assert revoked == []


def test_a_write_that_never_confirms_keeps_the_session_and_says_so(tmp_path, monkeypatch):
    store = store_with(tmp_path, DiesFirst(failures=99))
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    revoked = pending_sign_in(monkeypatch, uid="u2")
    with pytest.raises(cli.LoginFailed, match="could not confirm the keyring saved"):
        cli._login("me@proton.me")
    assert revoked == []  # never revoked on a guess


# ----------------------------------------------- links follow Proton's view mode


def conversation_mailbox(view_mode, threads=True):
    api = FakeProton()
    api.view_mode = view_mode
    api.add(60, conversation="c1", subject="First")
    api.add(30, conversation="c1", subject="Second")
    mailbox = Mailbox(api, "me@proton.me", categories=(), recent_limit=8, threads=threads)
    return api, mailbox


def test_in_single_message_view_every_link_is_a_message_link():
    """Proton's router hands the ID to MessageOnlyView in this mode: a
    conversation ID there opens nothing."""
    api, mailbox = conversation_mailbox(view_mode=1)
    mailbox.full_sync()
    assert {row["kind"] for row in mailbox.recent} == {"message"}
    assert [row["url"] for row in mailbox.recent] == [f"{WEB_BASE}/inbox/{r['id']}" for r in mailbox.recent]


def test_in_conversation_view_grouped_rows_link_to_conversations():
    api, mailbox = conversation_mailbox(view_mode=0)
    mailbox.full_sync()
    assert [(row["kind"], row["url"]) for row in mailbox.recent] == [("conversation", f"{WEB_BASE}/inbox/c1")]


def test_a_view_mode_change_during_a_session_rebuilds_rows_and_links():
    api, mailbox = conversation_mailbox(view_mode=0)
    mailbox.full_sync()
    api.view_mode = 1
    api.queue_event(MailSettings={"ViewMode": 1})
    assert mailbox.poll() is True
    assert {row["kind"] for row in mailbox.recent} == {"message"}


def test_an_explicit_refresh_reloads_proton_settings():
    api, mailbox = conversation_mailbox(view_mode=0)
    mailbox.full_sync()
    api.view_mode = 1  # changed elsewhere, no event seen yet
    mailbox.resync(reload_settings=True)
    assert {row["kind"] for row in mailbox.recent} == {"message"}


# ---------------------- a throttled renewal holds the account, whatever triggered it


class ThrottledRenewal:
    """Every authenticated request is 401; the refresh endpoint answers 429."""

    def __init__(self, ok_paths=()):
        self.log = []
        self.ok_paths = ok_paths
        self.headers = {}

    def request(self, method, url, params=None, json=None, headers=None, timeout=None, max_bytes=None):
        path = url.split(".proton.me", 1)[-1]
        self.log.append((method, path))
        if path == "/auth/v4/refresh":
            resp = Resp(429, {"Code": 2028, "Error": "Too many requests"})
            resp.headers["retry-after"] = "300"
            return resp
        if path in self.ok_paths:
            return Resp(200, {"Code": 1000, "EventID": "e1"})
        return Resp(401, {"Code": 401, "Error": "Invalid access token"})

    def close(self):
        pass

    def refreshes(self):
        return sum(1 for _, path in self.log if path == "/auth/v4/refresh")


def test_the_client_marks_a_throttled_renewal():
    client = ProtonClient(session(), http=ThrottledRenewal())
    with pytest.raises(RateLimited) as caught:
        client.fetch_bytes("/core/v4/images/logo", params={}, max_bytes=1000)
    assert caught.value.during_refresh and caught.value.retry_after == 300


def test_a_throttled_renewal_during_the_settings_request_holds_the_account(daemon_env):  # noqa: F811
    d, api, store, clock, states = daemon_env
    http = ThrottledRenewal(ok_paths=("/core/v4/events/latest",))
    d.client_factory = lambda s, on_refresh=None, **kw: ProtonClient(s, on_refresh=on_refresh, http=http)
    store.save(session())
    d.tick()  # events/latest ok, settings 401, refresh 429 (Retry-After 300)
    assert http.refreshes() == 1
    clock.advance(31)
    d.tick()
    clock.advance(200)
    d.tick()
    assert http.refreshes() == 1  # nothing until Proton's deadline
    clock.advance(100)
    d.tick()
    assert http.refreshes() == 2


def test_a_throttled_renewal_during_a_logo_request_holds_the_account(daemon_env):  # noqa: F811
    d, api, store, clock, states = daemon_env
    api.add(10, logo=True)
    throttled = RateLimited("slow down", 300)
    throttled.during_refresh = True
    api.logo_error = throttled
    store.save(session())
    d.tick()
    account = d.accounts["me@proton.me"]
    assert account.rate_limited_until >= clock() + 299
    calls = len(api.calls)
    clock.advance(31)
    d.tick()
    assert len(api.calls) == calls


def test_a_throttled_logo_endpoint_only_pauses_logos(daemon_env):  # noqa: F811
    d, api, store, clock, states = daemon_env
    api.add(10, logo=True)
    api.logo_error = RateLimited("slow down", 300)  # the image proxy, not the session
    store.save(session())
    d.tick()
    assert d.accounts["me@proton.me"].rate_limited_until == 0
    assert d.avatars.paused_until >= time.time() + 299  # the logo cache keeps its own clock


# ----------------------------------------------------------- demo mode is a demo


def test_demo_mode_never_touches_real_accounts(daemon_env):  # noqa: F811
    d, api, store, clock, states = daemon_env
    api.add(10)
    store.save(session())
    d.tick()
    d.handle({"cmd": "configure", "settings": {"demo": True}})
    d.handle({"cmd": "logout"})
    assert "Demo mode: nothing was changed" in d.snapshot()["message"]
    d.handle({"cmd": "markRead", "account": "me@proton.me", "ids": ["m1"]})
    assert store.accounts() == ["me@proton.me"]
    assert not getattr(api, "revoked", False)
    assert [c for c in api.calls if c[0] in ("PUT", "DELETE")] == []
    state = d.snapshot()
    assert state["demo"] is True and "Demo mode: nothing was marked read" in state["message"]


# ------------------------------------------- logos finish loading on idle polls


def test_logos_keep_loading_on_idle_polls(daemon_env):  # noqa: F811
    d, api, store, clock, states = daemon_env
    for index in range(8):
        api.add(100 + index, sender=f"Sender {index}", address=f"s{index}@example.com", logo=True)
    store.save(session())
    d.tick()
    rows = d.accounts["me@proton.me"].mailbox.recent
    assert sum(1 for row in rows if row.get("avatar")) == FETCH_BUDGET < 8
    clock.advance(31)
    d.tick()  # no new mail: the next batch still loads
    assert sum(1 for row in rows if row.get("avatar")) == 8


@pytest.mark.parametrize(
    "kind",
    ["ok", "offline", "throttled", "expired", "partial", "warning", "signed-out", "empty", "long", "multi"],
)
def test_every_demo_state_is_example_data(kind):
    from promarchy.demo import demo_state, variant

    assert variant(kind) == kind and variant("true") == "ok" and variant("TRUE ") == "ok"
    state = demo_state(1_800_000_000, 8, kind)
    assert state["demo"] is True and state["type"] == "state"
    assert all(a["email"].endswith(("example.com", "example.org")) for a in state["accounts"])
    assert len(state["messages"]) <= 8
    assert state["multiAccount"] == (len(state["accounts"]) > 1)
    if kind == "signed-out":
        assert state["status"] == "signed-out" and state["accounts"] == [] and state["messages"] == []
    if kind == "warning":
        assert state["status"] == "ok" and "not yet saved" in state["warning"]
