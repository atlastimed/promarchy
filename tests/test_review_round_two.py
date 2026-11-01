"""Regressions for the second round of reviews (three reviews of 58806c4).

Each test reproduces a reviewer's failure sequence and asserts the fixed
behaviour.
"""

from __future__ import annotations

import json
import time

import pytest
from fakes import FakeProton, MemorySecrets
from test_hardening import daemon_env  # noqa: F401  (fixture)
from test_ownership import daemon_running, make_daemon, make_store, pending_sign_in, shared_api_factory

from promarchy import cli, control
from promarchy.api import Session
from promarchy.store import SaveIndeterminate, SessionStore, StoreUnavailable, Superseded


def session(uid, email="me@proton.me", user_id="U", created_at=1.0):
    return Session(
        uid=uid,
        access_token=f"{uid}-a",
        refresh_token=f"{uid}-r",
        email=email,
        user_id=user_id,
        created_at=created_at,
    )


def recording_factory(ended):
    """Each session gets its own fake client; DELETE /auth/v4 records its UID."""

    def factory(s, on_refresh=None, **kw):
        api = FakeProton()
        api.session = s
        real = api.call

        def call(method, path, **kw):
            if method == "DELETE" and path == "/auth/v4":
                ended.append(s.uid)
            return real(method, path, **kw)

        api.call = call
        return api

    return factory


# ------------------------------------- a late retry never undoes a newer sign-in


def test_a_late_retry_never_undoes_a_newer_sign_in(tmp_path, monkeypatch):
    """Codex P1-1 / review A R3: A's reply is lost; meanwhile B signs in and
    replaces A; A's retry used to bring A back and revoke B."""
    store = make_store(tmp_path)
    store.save(session("old"))
    ended: list[str] = []
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    with daemon_running(make_daemon(store, tmp_path, recording_factory(ended))) as daemon:
        real_ask = control.ask
        calls = []

        def lossy_ask(request, timeout=control.REPLY_TIMEOUT):
            calls.append(request["session"]["uid"])
            reply = real_ask(request, timeout)
            if len(calls) == 1:  # A was adopted, but its answer never arrives
                assert real_ask({"cmd": "adopt", "session": session("b", created_at=2.0).to_dict()})["saved"]
                return {"error": "no answer"}
            return reply

        monkeypatch.setattr(control, "ask", lossy_ask)
        revoked = pending_sign_in(monkeypatch, uid="a", user_id="U")
        with pytest.raises(cli.LoginFailed, match="replaced this one"):
            cli._login("me@proton.me")
        assert calls == ["a", "a"]
        assert store.load("me@proton.me").uid == "b"
        assert daemon.accounts["me@proton.me"].client.session.uid == "b"
        assert "b" not in ended  # the newer sign-in survives
        assert revoked == ["a"]  # the superseded one is ended


def test_a_signed_out_session_is_never_saved_again(tmp_path):
    store = make_store(tmp_path)
    store.save(session("u1"))
    assert store.remove("me@proton.me", expected_uid="u1")
    with pytest.raises(Superseded):
        store.replace(session("u1"))
    assert store.accounts() == []


def test_the_superseded_list_stays_small(tmp_path):
    store = make_store(tmp_path)
    store.retire(f"u{i}" for i in range(200))
    assert len(store.retired()) == 64 and "u199" in store.retired()


# --------------------- an unknown save is settled by the owner, not by the keyring


class FlakyKeyring(MemorySecrets):
    """The armed write lands but reports failure; the read-back right after it
    fails once (review B F01/F02's fault model)."""

    def __init__(self):
        super().__init__()
        self.arm = False
        self.fail_reads = 0

    def set(self, key, value):
        super().set(key, value)
        if self.arm:
            self.arm = False
            self.fail_reads = 1
            raise SaveIndeterminate("Could not confirm the keyring saved it: Timeout was reached")

    def get(self, key):
        if self.fail_reads:
            self.fail_reads -= 1
            raise StoreUnavailable("keyring busy")
        return super().get(key)


def test_a_listed_address_is_no_substitute_for_the_owner_switching(tmp_path, monkeypatch):
    """Review B F02: same address, helper error after the write, read-back
    blip. The CLI used to report success while the bar kept the old session."""
    root = tmp_path / "state"
    root.mkdir()
    store = SessionStore(secrets=FlakyKeyring(), root=root)
    store.save(session("old"))
    ended: list[str] = []
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    with daemon_running(make_daemon(store, tmp_path, recording_factory(ended))) as daemon:
        store.secrets.arm = True
        revoked = pending_sign_in(monkeypatch, uid="new", user_id="U")
        assert cli._login("me@proton.me") == "me@proton.me"
        assert revoked == []
        assert daemon.accounts["me@proton.me"].client.session.uid == "new"  # the owner switched
        assert "old" in ended and "new" not in ended
        assert store.load("me@proton.me").uid == "new"


# ------------------------------------------------- small contracts from the reviews


def test_a_damaged_keyring_entry_never_stops_other_accounts(tmp_path):
    """Review A M3: valid JSON that is not a session object."""
    store = make_store(tmp_path)
    store.secrets.set("session:bad@example.com", "[]")
    store.secrets.set("session:me@proton.me", json.dumps(session("u1").to_dict()))
    store._write_index(["bad@example.com", "me@proton.me"])
    store._bump()
    assert store.load("bad@example.com") is None
    daemon = make_daemon(store, tmp_path, shared_api_factory(FakeProton()))
    daemon._sync_accounts()
    assert list(daemon.accounts) == ["me@proton.me"]


def test_status_trusts_a_snapshot_only_while_a_daemon_answers(tmp_path, monkeypatch, capsys):
    from promarchy.paths import runtime_dir

    store = make_store(tmp_path)
    store.save(session("u1"))
    api = FakeProton()
    for age in (60, 120, 180, 240, 300):
        api.add(age)
    daemon = make_daemon(store, tmp_path, shared_api_factory(api))
    daemon.state_path = None  # the real snapshot file, the one status reads
    path = runtime_dir() / "state.json"
    with daemon_running(daemon):
        for _ in range(100):
            if path.exists() and json.loads(path.read_text()).get("status") == "ok":
                break
            time.sleep(0.05)
        assert cli.main(["status", "--window", "1h"]) == 0
    assert cli.main(["status", "--window", "1h"]) == 1  # the same snapshot, no daemon
    assert "5" in capsys.readouterr().out


# ------------------------------- settings and refresh obligations survive failures


def failing_once(api, path, exc):
    real = api.call
    armed = {"on": True}

    def call(method, p, **kw):
        if p == path and armed["on"]:
            armed["on"] = False
            raise exc
        return real(method, p, **kw)

    api.call = call


def conversation_inbox(view_mode, threads=True):
    from fakes import NOW

    from promarchy.mailbox import Mailbox

    api = FakeProton()
    api.view_mode = view_mode
    api.add(60, conversation="c1")
    api.add(30, conversation="c1")
    return api, Mailbox(
        api, "me@proton.me", categories=(), recent_limit=8, threads=threads, clock=lambda: NOW
    )


@pytest.mark.parametrize("fault", ["network", "rate-limit"])
def test_a_failed_first_settings_read_is_retried_not_forgotten(fault):
    """Codex P1-2 / review A R4 / review B F05."""
    from promarchy.api import NetworkError, RateLimited
    from promarchy.mailbox import WEB_BASE

    api, mailbox = conversation_inbox(view_mode=1)
    failing_once(
        api, "/mail/v4/settings", NetworkError("offline") if fault == "network" else RateLimited("wait", 60)
    )
    with pytest.raises((NetworkError, RateLimited)):
        mailbox.poll()
    assert mailbox.poll() is True  # recovered
    assert {row["kind"] for row in mailbox.recent} == {"message"}
    assert [row["url"] for row in mailbox.recent] == [f"{WEB_BASE}/inbox/{r['id']}" for r in mailbox.recent]
    assert mailbox.poll() is False and mailbox.settings_pending is False


def test_a_refresh_event_survives_a_failed_settings_read():
    """Review A R2 / review B N01: the rebuild used to be consumed with the cursor."""
    from promarchy.api import RateLimited

    api, mailbox = conversation_inbox(view_mode=0, threads=False)
    mailbox.full_sync()
    assert mailbox.counts()["7d"] == 2
    api.add(10, conversation="c2")  # new mail, announced only by a Refresh
    api.queue_event(Refresh=1)
    failing_once(api, "/mail/v4/settings", RateLimited("wait", 60))
    with pytest.raises(RateLimited):
        mailbox.poll()
    for _ in range(3):  # recovered, and no further events arrive
        mailbox.poll()
    assert mailbox.counts()["7d"] == 3
    assert len(mailbox.recent) == 3


def test_links_follow_a_view_mode_change_before_the_rows_are_rebuilt():
    """Review B F04: an interrupted rebuild used to keep conversation links."""
    from promarchy.api import RateLimited
    from promarchy.mailbox import WEB_BASE

    api, mailbox = conversation_inbox(view_mode=0)
    mailbox.full_sync()
    assert [row["url"] for row in mailbox.recent] == [f"{WEB_BASE}/inbox/c1"]
    api.view_mode = 1
    api.queue_event(MailSettings={"ViewMode": 1})
    failing_once(api, "/mail/v4/messages", RateLimited("wait", 60))
    with pytest.raises(RateLimited):
        mailbox.poll()
    assert [row["url"] for row in mailbox.recent] == [f"{WEB_BASE}/inbox"]  # never a wrong route
    mailbox.poll()
    assert {row["kind"] for row in mailbox.recent} == {"message"}


# ---------------------------------------- logos never hold up the mail for long


def test_a_struggling_logo_service_never_holds_up_the_mail(daemon_env):  # noqa: F811
    """Review A R5: six 20-second timeouts per pass, repeated on idle polls."""
    from promarchy.api import NetworkError

    d, api, store, clock, states = daemon_env
    for index in range(8):
        api.add(100 + index, sender=f"Sender {index}", address=f"s{index}@example.com", logo=True)
    calls = []

    def timing_out(path, *, params=None, max_bytes, timeout=None):
        calls.append(timeout)
        raise NetworkError("Proton did not respond in time")

    api.fetch_bytes = timing_out
    store.save(session("u1", user_id=""))
    d.tick()
    assert len(calls) == 1 and calls[0] <= 10  # one short request, then the pass stops
    clock.advance(31)
    d.tick()  # an idle poll inside the back-off asks for no logos
    assert len(calls) == 1
    assert not list(d.avatars.root.glob("*.miss"))  # nothing remembered as "no logo"


class StatusLogoClient:
    def __init__(self, status, ctype="text/html", body=b"oops"):
        self.status, self.ctype, self.body = status, ctype, body

    def fetch_bytes(self, path, *, params=None, max_bytes, timeout=None):
        return self.status, self.ctype, self.body


@pytest.mark.parametrize("status", [500, 502, 504, 401])
def test_a_server_error_is_not_remembered_as_no_logo(tmp_path, status):
    """Review B F07: a 5xx used to be cached as "no logo" for two days."""
    import time

    from promarchy.avatars import Avatars

    avatars = Avatars(root=tmp_path / "logos")
    rows = [{"senderImage": True, "senderAddress": "a@example.com"}]
    assert avatars.attach(rows, StatusLogoClient(status)) is False
    assert not list((tmp_path / "logos").glob("*.miss"))
    assert avatars.paused_until > time.time()


def test_a_definite_no_logo_is_remembered(tmp_path):
    from promarchy.avatars import Avatars

    avatars = Avatars(root=tmp_path / "logos")
    avatars.attach([{"senderImage": True, "senderAddress": "a@example.com"}], StatusLogoClient(404))
    assert len(list((tmp_path / "logos").glob("*.miss"))) == 1
    assert avatars.paused_until == 0


# ------------------------------------ what the bar shows is current, or says it is not


def two_account_daemon(tmp_path):
    import io

    from fakes import Clock
    from test_daemon import RecordingNotifier

    from promarchy.avatars import Avatars
    from promarchy.daemon import Daemon
    from promarchy.notify import Watermarks

    clock = Clock()
    apis = {}

    def factory(s, on_refresh=None, **kw):
        api = apis.get(s.email)
        if api is None:
            api = apis[s.email] = FakeProton(clock=clock)
            unread = 3 if s.email == "a@example.com" else 10
            for index in range(unread):
                api.add(600 + index, address=f"{s.email}-{index}@example.com")
        api.session = s
        return api

    store = make_store(tmp_path)
    store.save(session("ua", email="a@example.com", user_id="A"))
    store.save(session("ub", email="b@example.com", user_id="B"))
    daemon = Daemon(
        store,
        out=io.StringIO(),
        notifier=RecordingNotifier(),
        watermarks=Watermarks(tmp_path / "n.json"),
        avatars=Avatars(root=tmp_path / "logos"),
        clock=clock,
        mono=clock,
        client_factory=factory,
        state_path=str(tmp_path / "state.json"),
    )
    return daemon, apis, clock


def test_a_stale_account_is_never_added_to_a_current_total(tmp_path):
    """Review A R6: an expired account's cached 10 were added to a healthy 3,
    under the words "not counted"."""
    from promarchy.api import AuthExpired

    daemon, apis, clock = two_account_daemon(tmp_path)
    daemon.tick()
    assert daemon.snapshot()["counts"]["24h"] == 13
    apis["b@example.com"].fail_with = AuthExpired("Your Proton session expired. Sign in again.")
    clock.advance(31)
    daemon.tick()
    state = daemon.snapshot()
    assert state["status"] == "partial" and "b@example.com" in state["message"]
    assert state["counts"]["24h"] == 3
    assert {row["account"] for row in state["messages"]} == {"a@example.com"}


def test_a_throttled_account_says_so_instead_of_looking_current(daemon_env):  # noqa: F811
    """Codex P2-1 / review A R6: status stayed "ok" for the whole Retry-After."""
    from promarchy.api import RateLimited

    d, api, store, clock, states = daemon_env
    api.add(60)
    store.save(session("u1", user_id=""))
    d.tick()
    assert d.snapshot()["status"] == "ok"
    api.fail_with = RateLimited("Too many requests", 600)
    clock.advance(31)
    d.tick()
    state = d.snapshot()
    assert state["status"] == "throttled"
    assert state["message"].startswith("Next check at")
    assert state["counts"]["24h"] == 1  # what it last saw, labelled as paused
    api.fail_with = None
    clock.advance(601)
    d.tick()
    assert d.snapshot()["status"] == "ok"
