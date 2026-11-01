"""Regressions from the ChatGPT Pro review of 111e7ff (findings C01 to C10)."""

from __future__ import annotations

import errno
import json
import threading

import pytest
from fakes import Clock, FakeProton
from test_hardening import daemon_env  # noqa: F401  (fixture)
from test_ownership import daemon_running, make_daemon, make_store, shared_api_factory
from test_probe import ProbeAPI, probe
from test_rereview import AuthServer, stored_session
from test_review_round_two import recording_factory, session, two_account_daemon

from promarchy import cli, control
from promarchy import store as store_module
from promarchy.api import ProtonClient, ProtonError, RateLimited, Session
from promarchy.auth import REVOKED
from promarchy.mailbox import Mailbox
from promarchy.paths import runtime_dir
from promarchy.store import StoreUnavailable

INBOX = "https://mail.proton.me/inbox"


# --------------------------------------------- C01: a retry keeps renewed tokens


def test_a_retried_hand_over_keeps_the_tokens_renewed_since(tmp_path, monkeypatch):
    """C01: the first hand-over landed but its answer was lost; the owner then
    renewed the session and exited. The retry, now the owner itself, must not
    write the original (already used) pair over the working one."""
    store = make_store(tmp_path)
    server = AuthServer(uid="fresh")
    original = stored_session(server)
    owned = Session.from_dict(original.to_dict())
    store.save(owned)
    ProtonClient(owned, http=server, on_refresh=store.update_tokens).refresh()
    assert store.load(original.email).refresh_token == server.refresh

    monkeypatch.setattr(control, "ask", lambda request, **kw: None)  # that owner has exited
    outcome, _ = cli._attempt(store, original)
    assert outcome == "saved"
    assert store.load(original.email).refresh_token == server.refresh
    assert store.accounts() == [original.email]


def test_a_retried_hand_over_still_finishes_registering(tmp_path, monkeypatch):
    """C01's fix must not skip the rest: a session saved but left unlisted is
    listed by the retry."""
    store = make_store(tmp_path)
    fresh = session("fresh")
    store.secrets.set(store_module._key(fresh.email), json.dumps(fresh.to_dict()))
    assert store.accounts() == []
    monkeypatch.setattr(control, "ask", lambda request, **kw: None)
    outcome, _ = cli._attempt(store, fresh)
    assert outcome == "saved" and store.accounts() == [fresh.email]


# ------------------------------- C02: a signed-out session is never confirmed


def test_a_late_retry_never_confirms_a_session_that_was_signed_out(tmp_path, monkeypatch):
    """C02: Proton confirmed the sign-out but the keyring would not delete the
    entry; a late retry of that sign-in must not be told it is signed in."""
    store = make_store(tmp_path)
    ended = []
    daemon = make_daemon(store, tmp_path, recording_factory(ended))
    fresh = session("fresh")
    assert daemon._adopt(fresh)["saved"]

    def refuse_delete(key):
        raise StoreUnavailable("keyring locked")

    monkeypatch.setattr(store.secrets, "delete", refuse_delete)
    result = daemon._logout(fresh.email)[0]
    assert result["outcome"] == REVOKED and result["removeFailed"]
    assert ended == ["fresh"]
    reply = daemon._adopt(fresh)
    assert reply.get("saved") is not True and reply.get("superseded") is True


# ------------------------ C03: a lost retirement record never revokes B


def test_a_failed_retirement_write_still_protects_the_newer_sign_in(tmp_path, monkeypatch):
    """C03: the state directory stops taking writes while the keyring works.
    B replaces A; A's late retry must neither come back nor end B."""
    store = make_store(tmp_path)
    ended = []
    daemon = make_daemon(store, tmp_path, recording_factory(ended))
    a, b = session("a"), session("b", created_at=2.0)
    assert daemon._adopt(a)["saved"]

    def disk_full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(store_module, "write_private", disk_full)
    assert daemon._adopt(b)["saved"]
    assert store.load(a.email).uid == "b" and ended == ["a"]

    reply = daemon._adopt(a)
    assert reply.get("superseded") is True
    assert "b" not in ended and store.load(a.email).uid == "b"


# ---------------------------- C04: a fresh event cursor re-reads the settings


def test_a_fresh_event_cursor_reads_the_settings_again():
    """C04: a rejected cursor is replaced with the latest one, skipping the
    events in between, a view-mode change among them."""
    clock = Clock()
    api = FakeProton(clock=clock)
    api.add(60, conversation="c1")
    api.add(30, conversation="c1")
    mailbox = Mailbox(api, "me@proton.me", clock=clock)
    mailbox.full_sync()
    assert mailbox.view_mode == 0

    api.view_mode = 1
    api.queue_event(**{"raise": ProtonError("Invalid ID", code=2061, status=400)})
    assert mailbox.poll() is True
    assert api.endpoint_calls("/core/v4/events/latest") == 2
    assert mailbox.view_mode == 1
    assert {row["kind"] for row in mailbox.recent} == {"message"}


# --------------------------- C05: links are never guessed from unread settings


def test_links_are_never_guessed_while_the_settings_cannot_be_read(daemon_env):  # noqa: F811
    """C05: the first settings read fails with a server error. Counts and rows
    still show, but no row links into a view mode Proton has not confirmed;
    once it answers, the links follow it."""
    d, api, store, clock, states = daemon_env
    api.view_mode = 1
    api.add(60, conversation="c1")
    api.add(30, conversation="c1")
    real_call = api.call

    def unavailable(method, path, **kwargs):
        if path == "/mail/v4/settings":
            raise ProtonError("Internal server error", status=500)
        return real_call(method, path, **kwargs)

    api.call = unavailable
    store.save(session("u"))
    d.tick()
    state = d.snapshot()
    assert state["status"] == "ok" and state["counts"]["all"] == 2
    assert state["messages"] and {row["url"] for row in state["messages"]} == {INBOX}

    api.call = real_call
    clock.advance(301)
    d.tick()
    rows = d.snapshot()["messages"]
    assert {row["kind"] for row in rows} == {"message"}
    assert all(row["url"].startswith(INBOX + "/m") for row in rows)


def test_settings_known_from_an_event_also_unlock_the_links():
    """Settings that arrive in an event count as confirmed, like a read."""
    clock = Clock()
    api = FakeProton(clock=clock)
    api.add(30)
    mailbox = Mailbox(api, "me@proton.me", clock=clock)
    real_call = api.call

    def unavailable(method, path, **kwargs):
        if path == "/mail/v4/settings":
            raise ProtonError("Internal server error", status=500)
        return real_call(method, path, **kwargs)

    api.call = unavailable
    mailbox.full_sync()
    assert {row["url"] for row in mailbox.recent} == {INBOX}
    api.queue_event(MailSettings={"ViewMode": 0})
    mailbox.poll()
    assert all(row["url"] != INBOX for row in mailbox.recent)


# ------------------------------- C06: the probe writes only what it restores


def test_roundtrip_leaves_a_reply_arriving_before_the_read_alone(monkeypatch):
    """C06: a reply landed between the re-check and the read; marking the
    whole conversation read would mark it too, and only the original is
    restored."""
    monkeypatch.setattr(probe, "snapshot", lambda: {"counts": {"all": 1}})
    monkeypatch.setattr(probe, "wait_for", lambda *a, **k: 0.0)

    class ReplyBeforeRead(ProbeAPI):
        def put(self, path, body):
            if path.endswith("/read") and len(self.messages) == 1:
                self.messages.append({"labels": {"0"}, "unread": True})
            return super().put(path, body)

    api = ReplyBeforeRead([{"labels": {"0"}, "unread": True}])
    result = probe.roundtrip(api, probe.Aliases())
    assert result["restored"] is True
    assert [m["unread"] for m in api.messages] == [True, True]


# ------------------------- C07: a total still missing an account is not current


def test_a_total_still_missing_an_account_is_not_shown_as_current(tmp_path):
    """C07: the first account's count was emitted as "ok" while the second
    was still connecting, then grew from 3 to 13."""
    daemon, _, _ = two_account_daemon(tmp_path)
    daemon.tick()
    states = [json.loads(line) for line in daemon.out.getvalue().splitlines()]
    states = [s for s in states if s.get("type") == "state"]
    incomplete = [s for s in states if any(a["status"] == "starting" for a in s.get("accounts") or [])]
    assert incomplete and incomplete[0]["counts"]["all"] == 3
    assert all(s["status"] == "starting" for s in incomplete)
    assert states[-1]["status"] == "ok" and states[-1]["counts"]["all"] == 13


# --------------------- C08: a daemon vouches only for its own snapshot


def test_status_never_vouches_for_another_daemons_snapshot(tmp_path, monkeypatch, capsys):
    """C08: a new daemon answers on its socket while it is still reading the
    keyring; the snapshot on disk is its predecessor's."""
    store = make_store(tmp_path)
    store.save(session("u1"))
    daemon = make_daemon(store, tmp_path, shared_api_factory(FakeProton()))
    daemon.state_path = None
    path = runtime_dir() / "state.json"
    old = {"type": "state", "status": "ok", "counts": {"1h": 999}}
    path.write_text(json.dumps(old))

    entered, release = threading.Event(), threading.Event()
    real_get = store.secrets.get

    def slow_get(key):
        if key.startswith("session:"):
            entered.set()
            assert release.wait(5)
        return real_get(key)

    monkeypatch.setattr(store.secrets, "get", slow_get)
    with daemon_running(daemon):
        try:
            assert entered.wait(2)
            assert control.ask({"cmd": "ping"}, timeout=1)["alive"] is True
            code = cli.main(["status", "--window", "1h"])
            output = capsys.readouterr()
        finally:
            release.set()
    assert code != 0 and "999" not in output.out
    assert json.loads(path.read_text()) != old  # the new daemon did publish there


def test_status_trusts_the_answering_daemons_own_snapshot(tmp_path, capsys):
    store = make_store(tmp_path)
    api = FakeProton()
    api.add(30)
    store.save(session("u1"))
    daemon = make_daemon(store, tmp_path, shared_api_factory(api))
    daemon.state_path = None
    with daemon_running(daemon):
        for _ in range(50):
            state = json.loads((runtime_dir() / "state.json").read_text() or "{}")
            if state.get("status") == "ok":
                break
            threading.Event().wait(0.05)
        code = cli.main(["status", "--window", "all"])
    assert code == 0 and capsys.readouterr().out.strip() == "1"


# --------------------- C09: a bare number only when it is current and complete


@pytest.mark.parametrize(
    "status,message",
    [
        ("throttled", "Proton asked to wait; next check at 14:05."),
        ("offline", "No connection to Proton."),
        ("partial", "b@example.com: Sign in again."),
        ("starting", ""),
        ("error", "Proton had a problem."),
    ],
)
def test_status_window_refuses_counts_that_are_not_current(monkeypatch, capsys, status, message):
    """C09: scripts reading `status --window` could not tell a paused, offline
    or incomplete count from a current one."""
    state = {"type": "state", "status": status, "message": message, "counts": {"1h": 13}}
    (runtime_dir() / "state.json").write_text(json.dumps(state))
    monkeypatch.setattr(control, "ask", lambda request, **kw: {"alive": True})
    code = cli.main(["status", "--window", "1h"])
    output = capsys.readouterr()
    assert code != 0 and output.out == ""
    assert message in output.err or status in output.err


def test_status_window_refuses_a_real_throttled_snapshot(tmp_path, monkeypatch, capsys):
    daemon, _, _ = two_account_daemon(tmp_path)
    daemon.state_path = None
    daemon.tick()
    for account in daemon.accounts.values():
        daemon._record_failure(account, RateLimited("wait", 600))
    daemon.emit(force=True)
    state = json.loads((runtime_dir() / "state.json").read_text())
    assert state["status"] == "throttled"
    alive = {"alive": True, "instance": state.get("instance")}
    monkeypatch.setattr(control, "ask", lambda request, **kw: alive)
    assert cli.main(["status", "--window", "1h"]) != 0
    assert capsys.readouterr().out == ""
    assert cli.main(["status", "--json"]) == 0  # the full picture, with its status, still prints
    assert json.loads(capsys.readouterr().out)["status"] == "throttled"
