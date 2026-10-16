from __future__ import annotations

import io
import json

import pytest
from fakes import Clock, FakeProton, MemorySecrets

from promarchy.api import AuthExpired, NetworkError, Session
from promarchy.avatars import Avatars
from promarchy.daemon import Config, Daemon
from promarchy.notify import Watermarks
from promarchy.store import SessionStore


class RecordingNotifier:
    def __init__(self):
        self.batches = []

    def announce(self, messages, urls, inbox_url):
        self.batches.append([m["id"] for m in messages])


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
        return api

    daemon = Daemon(
        store,
        out=out,
        notifier=notifier,
        watermarks=Watermarks(tmp_path / "notified.json"),
        avatars=Avatars(root=tmp_path / "logos"),
        clock=clock,
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
        Session(uid=f"uid-{email}", access_token="a", refresh_token="r", email=email)
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
