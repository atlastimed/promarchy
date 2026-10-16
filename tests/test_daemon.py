from __future__ import annotations

import io
import json

import pytest
from fakes import Clock, FakeProton, MemorySecrets

from promarchy.api import AuthExpired, NetworkError, Session
from promarchy.avatars import Avatars
from promarchy.daemon import Config, Daemon
from promarchy.notify import Watermarks
from promarchy.store import SessionStore, StoreUnavailable


class RecordingNotifier:
    def __init__(self):
        self.batches = []
        self.totals = []

    def announce(self, messages, urls, inbox_url, total=None):
        self.batches.append([m["id"] for m in messages])
        self.totals.append(len(messages) if total is None else total)


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    api = FakeProton(clock=clock)
    store = SessionStore(secrets=MemorySecrets(), root=tmp_path / "state")
    (tmp_path / "state").mkdir()
    out = io.StringIO()
    notifier = RecordingNotifier()

    def factory(session, on_refresh=None):
        api.session = session
        api.on_refresh = on_refresh
        return api

    daemon = Daemon(
        store,
        out=out,
        notifier=notifier,
        watermarks=Watermarks(tmp_path / "notified.json"),
        avatars=Avatars(root=tmp_path / "logos"),
        clock=clock,
        mono=clock,
        client_factory=factory,
        state_path=str(tmp_path / "state.json"),
    )

    class Env:
        pass

    e = Env()
    e.clock, e.api, e.store, e.out, e.notifier, e.daemon, e.tmp = (
        clock,
        api,
        store,
        out,
        notifier,
        daemon,
        tmp_path,
    )
    e.states = lambda: [json.loads(line) for line in out.getvalue().splitlines()]
    e.sign_in = lambda email="me@proton.me": store.save(
        Session(uid="uid-" + email.split("@")[0], access_token="a", refresh_token="r", email=email)
    )
    return e


def test_signed_out_state(env):
    env.daemon.tick()
    state = env.states()[-1]
    assert state["status"] == "signed-out"
    assert state["accounts"] == []
    assert json.loads((env.tmp / "state.json").read_text()) == state


def test_first_sync_reports_counts_and_messages(env):
    env.api.add(120, subject="Fresh")
    env.api.add(3 * 86400, subject="Older")
    env.sign_in()
    env.daemon.tick()
    state = env.states()[-1]
    assert state["status"] == "ok"
    assert state["counts"] == {"1h": 1, "24h": 1, "7d": 2, "all": 2}
    assert [m["subject"] for m in state["messages"]] == ["Fresh", "Older"]
    assert state["accounts"][0]["email"] == "me@proton.me"
    assert env.notifier.batches == []  # old mail is not news


def test_new_mail_is_announced_once(env):
    env.sign_in()
    env.daemon.tick()
    env.clock.advance(31)
    new = env.api.add(1, subject="Ping")
    env.api.queue_event(Messages=[{"ID": new["ID"], "Action": 1, "Message": {"LabelIDs": ["0"]}}])
    env.daemon.tick()
    assert env.notifier.batches == [[new["ID"]]]
    env.clock.advance(31)
    env.api.queue_event(MessageCounts=[{"LabelID": "0", "Unread": 1}])
    env.daemon.tick()
    assert env.notifier.batches == [[new["ID"]]]


def test_paused_notifications_still_advance_watermark(env):
    env.sign_in()
    env.daemon.handle({"cmd": "configure", "settings": {"notify": False}})
    env.daemon.tick()
    env.clock.advance(31)
    new = env.api.add(1)
    env.api.queue_event(Messages=[{"ID": new["ID"], "Message": {"LabelIDs": ["0"]}}])
    env.daemon.tick()
    env.daemon.handle({"cmd": "configure", "settings": {"notify": True}})
    env.clock.advance(31)
    env.api.queue_event(MessageCounts=[{"LabelID": "0"}])
    env.daemon.tick()
    assert env.notifier.batches == []


def test_unchanged_state_is_not_reemitted(env):
    env.sign_in()
    env.daemon.tick()
    lines = len(env.states())
    env.clock.advance(31)
    env.daemon.tick()
    assert len(env.states()) == lines


def test_refresh_always_answers(env):
    env.sign_in()
    env.daemon.tick()
    lines = len(env.states())
    env.daemon.handle({"cmd": "refresh"})
    env.daemon.tick()
    assert len(env.states()) == lines + 1


def test_counts_age_out_on_their_own(env):
    env.api.add(3500)
    env.sign_in()
    env.daemon.tick()
    calls = len(env.api.calls)
    assert env.daemon._sleep_for() <= 5  # wakes for the stamp check at least
    env.daemon.accounts["me@proton.me"].next_poll = float("inf")
    env.clock.advance(101)
    env.daemon.tick()
    assert env.states()[-1]["counts"]["1h"] == 0
    assert len(env.api.calls) == calls


def test_configure_categories_resyncs(env):
    env.api.add(60, labels=("0", "25"))
    env.sign_in()
    env.daemon.tick()
    assert env.states()[-1]["counts"]["1h"] == 1
    env.daemon.handle({"cmd": "configure", "settings": {"categories": "primary"}})
    env.daemon.tick()
    assert env.states()[-1]["counts"]["1h"] == 0


def test_mark_read_command(env):
    message = env.api.add(60)
    env.sign_in()
    env.daemon.tick()
    env.daemon.handle({"cmd": "markRead", "account": "me@proton.me", "ids": [message["ID"], 5]})
    assert env.states()[-1]["counts"]["1h"] == 0


def test_expired_session_stops_polling_until_new_sign_in(env):
    env.sign_in()
    env.daemon.tick()
    env.api.fail_with = AuthExpired("gone")
    env.clock.advance(31)
    env.daemon.tick()
    assert env.states()[-1]["status"] == "auth-expired"
    calls = len(env.api.calls)
    env.clock.advance(600)
    env.daemon.tick()
    assert len(env.api.calls) == calls
    env.api.fail_with = None
    env.store.save(Session(uid="fresh", access_token="a", refresh_token="r", email="me@proton.me"))
    env.daemon.tick()
    assert env.states()[-1]["status"] == "ok"


def test_offline_backs_off(env):
    env.sign_in()
    env.daemon.tick()
    env.api.fail_with = NetworkError("down")
    delays = []
    for _ in range(3):
        env.clock.advance(1000)
        env.daemon.tick()
        delays.append(env.daemon.accounts["me@proton.me"].next_poll - env.clock())
    assert delays == sorted(delays) and delays[0] < delays[-1] <= 300
    assert env.states()[-1]["status"] in ("ok", "offline")


def test_sign_out_removes_account(env):
    env.sign_in()
    env.daemon.tick()
    env.store.remove("me@proton.me")
    env.daemon.tick()
    assert env.states()[-1]["status"] == "signed-out"


def test_two_accounts_merge(env):
    env.api.add(60, subject="one")
    env.sign_in("a@proton.me")
    env.sign_in("b@proton.me")
    env.daemon.tick()
    state = env.states()[-1]
    assert state["multiAccount"] is True
    assert state["counts"]["1h"] == 2  # same fake mailbox behind both
    assert len(state["messages"]) == 2


def test_config_clamps_and_parses():
    config = Config.from_dict(
        {"pollSeconds": 1, "recentLimit": "99", "notify": "false", "categories": "primary"}
    )
    assert config.poll_seconds == 10
    assert config.recent_limit == 25
    assert config.notify is False
    assert config.categories == ("primary",)
    assert Config.from_dict({}).poll_seconds == 30


def test_sender_logos_attached(env):
    env.api.add(60, logo=True, address="news@github.com")
    env.sign_in()
    env.daemon.tick()
    message = env.states()[-1]["messages"][0]
    assert message["avatar"].endswith(".png")
    calls = env.api.endpoint_calls("/core/v4/images/logo")
    env.daemon.handle({"cmd": "refresh"})
    env.daemon.tick()
    assert env.api.endpoint_calls("/core/v4/images/logo") == calls  # cached


def test_demo_mode_needs_no_account_or_network(env):
    env.daemon.handle({"cmd": "configure", "settings": {"demo": True, "recentLimit": 5}})
    env.daemon.tick()
    state = env.states()[-1]
    assert state["status"] == "ok"
    assert len(state["messages"]) == 5
    assert env.api.calls == []


class FlakyStore(SessionStore):
    """A store whose keyring reads can be made to fail."""

    fail = False
    hide = False

    def load(self, email):
        if self.fail:
            raise StoreUnavailable("keyring locked")
        if self.hide:
            return None
        return super().load(email)


def make_daemon(env, store):
    def factory(session, on_refresh=None):
        env.api.session = session
        env.api.on_refresh = on_refresh
        return env.api

    return Daemon(
        store,
        out=env.out,
        notifier=env.notifier,
        watermarks=Watermarks(env.tmp / "n2.json"),
        avatars=Avatars(root=env.tmp / "logos2"),
        clock=env.clock,
        mono=env.clock,
        client_factory=factory,
        state_path=str(env.tmp / "state2.json"),
    )


def test_keyring_failure_is_retried(env):
    store = FlakyStore(secrets=MemorySecrets(), root=env.tmp / "flaky")
    (env.tmp / "flaky").mkdir()
    store.save(Session(uid="u", access_token="a", refresh_token="r", email="me@proton.me"))
    daemon = make_daemon(env, store)
    store.fail = True
    daemon.tick()
    assert env.states()[-1]["status"] == "error"
    store.fail = False
    daemon.tick()  # still inside the backoff window
    assert env.states()[-1]["status"] == "error"
    env.clock.advance(15)
    daemon.tick()
    assert env.states()[-1]["status"] == "ok"


def test_empty_keyring_answer_keeps_a_running_account(env):
    store = FlakyStore(secrets=MemorySecrets(), root=env.tmp / "flaky")
    (env.tmp / "flaky").mkdir()
    store.save(Session(uid="u", access_token="a", refresh_token="r", email="me@proton.me"))
    daemon = make_daemon(env, store)
    daemon.tick()
    assert env.states()[-1]["status"] == "ok"
    store.hide = True  # e.g. a dismissed unlock prompt
    store.save(Session(uid="u", access_token="a", refresh_token="r", email="other@proton.me"))
    daemon.tick()
    assert "me@proton.me" in daemon.accounts
    state = env.states()[-1]
    assert state["status"] == "partial"  # me works; other's session could not be read
    assert "other@proton.me" in state["message"]


def test_missing_session_asks_to_sign_in_again(env):
    store = FlakyStore(secrets=MemorySecrets(), root=env.tmp / "flaky")
    (env.tmp / "flaky").mkdir()
    store.save(Session(uid="u", access_token="a", refresh_token="r", email="me@proton.me"))
    store.hide = True
    daemon = make_daemon(env, store)
    daemon.tick()
    state = env.states()[-1]
    assert state["status"] == "auth-expired"
    assert "me@proton.me" in state["message"]


def test_rotated_tokens_are_saved_and_retried(env, monkeypatch):
    env.sign_in()
    env.daemon.tick()
    rotated = Session(uid="uid-me", access_token="a2", refresh_token="r2", email="me@proton.me")
    calls = {"n": 0}
    real = env.store.update_tokens

    def flaky(session):
        calls["n"] += 1
        if calls["n"] == 1:
            raise StoreUnavailable("keyring busy")
        return real(session)

    monkeypatch.setattr(env.store, "update_tokens", flaky)
    env.daemon.unsaved["me@proton.me"] = rotated  # what the on_refresh callback records
    env.daemon._persist_unsaved()
    assert "me@proton.me" in env.daemon.unsaved  # first save failed, kept for retry
    env.clock.advance(15)  # past the retry backoff
    env.daemon.tick()
    assert env.daemon.unsaved == {}
    assert env.store.load("me@proton.me").refresh_token == "r2"


def test_rotation_does_not_resurrect_a_signed_out_account(env):
    env.sign_in()
    env.daemon.tick()
    env.store.remove("me@proton.me")
    stale = Session(uid="uid-me", access_token="a2", refresh_token="r2", email="me@proton.me")
    assert env.store.update_tokens(stale) is False
    assert env.store.accounts() == []


def test_rotation_does_not_clobber_a_newer_sign_in(env):
    env.sign_in()
    env.store.save(Session(uid="newer", access_token="n", refresh_token="nr", email="me@proton.me"))
    stale = Session(uid="uid-me", access_token="a2", refresh_token="r2", email="me@proton.me")
    assert env.store.update_tokens(stale) is False
    assert env.store.load("me@proton.me").uid == "newer"


def test_bad_command_does_not_stop_the_daemon(env, monkeypatch):
    env.sign_in()
    env.daemon.tick()

    def boom(*a, **kw):
        raise RuntimeError("bug")

    monkeypatch.setattr(env.daemon, "_mark_read", boom)
    env.daemon.commands.put({"cmd": "markRead", "account": "me@proton.me", "ids": ["x"]})
    env.daemon._drain(timeout=0.01)
    assert env.daemon.stopping is False


def test_mark_read_internal_error_is_contained(env, monkeypatch):
    env.sign_in()
    env.daemon.tick()

    def boom(ids):
        raise ValueError("unexpected")

    monkeypatch.setattr(env.daemon.accounts["me@proton.me"].mailbox, "mark_read", boom)
    env.daemon.handle({"cmd": "markRead", "account": "me@proton.me", "ids": ["x"]})
    assert env.daemon.accounts["me@proton.me"].status == "error"


def test_wall_clock_jumping_back_does_not_stall_polling(env):
    wall = Clock()
    env.daemon.clock = wall
    env.sign_in()
    env.daemon.tick()
    calls = len(env.api.calls)
    wall.advance(-3600)  # NTP correction at boot
    env.clock.advance(31)  # monotonic time moves on
    env.daemon.tick()
    assert len(env.api.calls) > calls


def test_token_rotation_reaches_the_keyring(env):
    env.sign_in()
    env.daemon.tick()
    generation = env.store.generation()
    session = env.api.session
    session.access_token, session.refresh_token = "a2", "r2"
    env.api.on_refresh(session)  # what ProtonClient.refresh() calls
    assert env.store.load("me@proton.me").refresh_token == "r2"
    assert env.daemon.unsaved == {}
    assert env.store.generation() == generation  # not mistaken for a new sign-in


def test_empty_keyring_answer_at_start_is_retried(env):
    store = FlakyStore(secrets=MemorySecrets(), root=env.tmp / "flaky")
    (env.tmp / "flaky").mkdir()
    store.save(Session(uid="u", access_token="a", refresh_token="r", email="me@proton.me"))
    store.hide = True  # keyring locked when the session starts
    daemon = make_daemon(env, store)
    daemon.tick()
    assert env.states()[-1]["status"] == "auth-expired"
    store.hide = False  # user unlocks the keyring
    env.clock.advance(15)
    daemon.tick()
    assert env.states()[-1]["status"] == "ok"


def test_failed_token_saves_back_off(env, monkeypatch):
    env.sign_in()
    env.daemon.tick()
    attempts = []

    def always_fails(session):
        attempts.append(session)
        raise StoreUnavailable("locked")

    monkeypatch.setattr(env.store, "update_tokens", always_fails)
    env.daemon.unsaved["me@proton.me"] = env.api.session
    env.daemon._persist_unsaved(force=True)
    for _ in range(5):
        env.clock.advance(1)
        env.daemon._persist_unsaved()
    assert len(attempts) == 1  # waits out the backoff instead of retrying every tick
    env.clock.advance(60)
    env.daemon._persist_unsaved()
    assert len(attempts) == 2


def test_removed_account_with_unwritable_watermarks_does_not_crash(env, monkeypatch):
    env.sign_in()
    env.daemon.tick()

    def broken(email):
        raise OSError("read-only")

    monkeypatch.setattr(env.daemon.watermarks, "forget", broken)
    env.store.remove("me@proton.me")
    env.daemon.tick()
    assert env.states()[-1]["status"] == "signed-out"


def test_sigterm_handler_uses_a_signal_safe_queue(env):
    import queue

    assert isinstance(env.daemon.commands, queue.SimpleQueue)
