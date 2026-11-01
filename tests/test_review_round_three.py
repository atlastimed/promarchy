"""Regressions from the ChatGPT Pro review of 111e7ff (findings C01 to C10)
and its follow-up on e07cc75 (V01 to V05)."""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import threading

import pytest
from fakes import Clock, FakeProton, MemorySecrets
from test_hardening import daemon_env  # noqa: F401  (fixture)
from test_ownership import daemon_running, make_daemon, make_store, pending_sign_in, shared_api_factory
from test_probe import ProbeAPI, probe
from test_rereview import AuthServer, stored_session
from test_review_round_two import FlakyKeyring, recording_factory, session, two_account_daemon

from promarchy import cli, control, sessions
from promarchy import daemon as daemon_module
from promarchy import store as store_module
from promarchy.api import AuthExpired, ProtonClient, ProtonError, RateLimited, Session
from promarchy.auth import REVOKED, TWO_FACTOR_TOTP, PendingLogin
from promarchy.mailbox import Mailbox
from promarchy.paths import runtime_dir
from promarchy.store import RETIRED_KEEP, SaveIndeterminate, SessionStore, StoreUnavailable

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
    outcome, _, _ = cli._attempt(store, original)
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
    outcome, _, _ = cli._attempt(store, fresh)
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
    alive = {"alive": True, "snapshot": daemon._published}
    monkeypatch.setattr(control, "ask", lambda request, **kw: alive)
    assert cli.main(["status", "--window", "1h"]) != 0
    assert capsys.readouterr().out == ""
    assert cli.main(["status", "--json"]) == 0  # the full picture, with its status, still prints
    assert json.loads(capsys.readouterr().out)["status"] == "throttled"


# ===================================== follow-up review of e07cc75 (V01 to V05)


def disk_full(*args, **kwargs):
    raise OSError(errno.ENOSPC, "No space left on device")


def read_only(*args, **kwargs):
    raise OSError(errno.EROFS, "Read-only file system")


def answering(daemon):
    """control.ask, answered by `daemon` in this thread (request parsing included)."""
    return lambda request, **kw: daemon._answer(control.parse(json.dumps(request).encode()))


def first_answer_lost(daemon):
    """control.ask whose first answer never arrives; `daemon` answers the rest."""
    answers = iter([{"error": "no answer"}])
    return lambda request, **kw: next(answers, None) or answering(daemon)(request)


def test_a_confirmed_sign_out_counts_even_if_the_state_directory_cannot_be_locked(tmp_path, monkeypatch):
    """V01: removing the account failed before reaching the keyring, so the
    sign-out was not recorded at all, and a late retry was told "saved"."""
    store = make_store(tmp_path)
    ended = []
    daemon = make_daemon(store, tmp_path, recording_factory(ended))
    a = session("a")
    assert daemon._adopt(a)["saved"]
    monkeypatch.setattr(store_module, "open_private", read_only)
    result = daemon._logout(a.email)[0]
    assert result["outcome"] == REVOKED and result["removeFailed"] and ended == ["a"]
    reply = daemon._adopt(a)
    assert reply.get("saved") is not True and reply.get("superseded") is True


def test_a_saved_sign_in_is_never_revoked_over_a_bookkeeping_failure(tmp_path, monkeypatch):
    """V02: the new session was saved, then remembering the one it replaced
    failed; the owner answered "unchanged" and the CLI revoked the saved one."""
    store = make_store(tmp_path)
    daemon = make_daemon(store, tmp_path, recording_factory([]))
    assert daemon._adopt(session("old"))["saved"]
    real_open, real_set = store_module.open_private, store.secrets.set
    armed = []

    def saves_then_arms(key, value):
        real_set(key, value)
        armed.append(True)

    def fails_once_armed(path):
        if armed:
            armed.clear()
            raise OSError(errno.EROFS, "Read-only file system")
        return real_open(path)

    monkeypatch.setattr(store.secrets, "set", saves_then_arms)
    monkeypatch.setattr(store_module, "open_private", fails_once_armed)
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    monkeypatch.setattr(cli, "ProtonClient", FakeProton)
    monkeypatch.setattr(control, "ask", answering(daemon))
    revoked = pending_sign_in(monkeypatch, uid="new", user_id="U")
    with contextlib.suppress(cli.LoginFailed):
        cli._login("me@proton.me")
    assert store.load("me@proton.me").uid == "new" and revoked == []


@pytest.mark.parametrize("a_started", [1.0, 20.0], ids=["started-first", "started-last"])
def test_a_retry_after_the_owner_restarted_never_undoes_a_later_sign_in(tmp_path, monkeypatch, a_started):
    """V03 and W01: A is adopted but its answer is lost; B replaces A while the
    state directory is full, so only the running bar remembers A is over; the
    bar restarts; A's retry reaches the new one. Whichever of the two started
    first (B may have waited for its 2FA code), B was adopted last."""
    store = make_store(tmp_path)
    ended, calls = [], []
    first = make_daemon(store, tmp_path, recording_factory(ended))
    a, b = session("a", created_at=a_started), session("b", created_at=10.0)
    owner = {"now": first}

    def lost_answer_then_restart(request, **kw):
        parsed = control.parse(json.dumps(request).encode())
        calls.append((parsed["session"].uid, parsed["retry"]))
        if len(calls) == 1:
            assert first._answer(parsed)["saved"]
            with monkeypatch.context() as fault:
                fault.setattr(store_module, "write_private", disk_full)
                assert first._adopt(b)["saved"]
            assert first._adopt(a).get("superseded") is True  # that bar remembers
            reopened = SessionStore(secrets=store.secrets, root=store.root)
            owner["now"] = make_daemon(reopened, tmp_path, recording_factory(ended))
            owner["now"]._sync_accounts()
            assert owner["now"].accounts[b.email].client.session.uid == "b"
            return {"error": "no answer"}
        return owner["now"]._answer(parsed)

    monkeypatch.setattr(control, "ask", lost_answer_then_restart)
    result = cli._hand_over(store, a)
    assert calls == [("a", False), ("a", True)] and result.get("unconfirmed") is True
    assert ended == ["a"] and owner["now"].store.load(b.email).uid == "b"


def test_a_sign_in_that_finished_its_2fa_last_is_kept_even_if_it_started_first(tmp_path, monkeypatch):
    """W01: A waits for its 2FA code while B completes; A's save then lands but
    cannot be read back. Start order is not adoption order: A is the latest,
    so its retry finishes it and ends B (never the other way round)."""
    store = SessionStore(secrets=FlakyKeyring(), root=make_store(tmp_path).root)
    ended = []
    daemon = make_daemon(store, tmp_path, recording_factory(ended))
    pending = session("started-first", created_at=10.0)
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    monkeypatch.setattr(cli, "ProtonClient", FakeProton)
    revoked = pending_sign_in(monkeypatch, uid=pending.uid, user_id="U")
    monkeypatch.setattr(cli, "begin_login", lambda *args: PendingLogin(pending, TWO_FACTOR_TOTP))

    def finishing_2fa_after_another_sign_in(client):
        assert daemon._adopt(session("finished-first", created_at=20.0))["saved"]
        store.secrets.arm = True  # the next save lands, and reading it back fails once
        return True

    monkeypatch.setattr(cli, "_two_factor", finishing_2fa_after_another_sign_in)
    monkeypatch.setattr(control, "ask", answering(daemon))
    assert cli._login(pending.email) == pending.email
    assert store.load(pending.email).uid == "started-first" and revoked == []
    assert ended == ["finished-first"]
    assert daemon.accounts[pending.email].client.session.uid == "started-first"


def test_a_signed_out_sign_in_is_not_brought_back_after_a_restart(tmp_path, monkeypatch):
    """W02: A is adopted, its answer lost; the user signs out (Proton confirms,
    the keyring entry is deleted) while the state directory takes no writes;
    the bar restarts. An empty account no longer proves the first save never
    landed, so the retry keeps the sign-in unconfirmed instead of saving a
    revoked session and calling it signed in."""
    store = make_store(tmp_path)
    ended, retries = [], []
    first = make_daemon(store, tmp_path, recording_factory(ended))
    a = session("signed-out")
    owner = {"now": first}

    def lost_answer_then_restart(request, **kw):
        parsed = control.parse(json.dumps(request).encode())
        retries.append(parsed["retry"])
        reply = owner["now"]._answer(parsed)
        if len(retries) == 1:
            assert reply["saved"]
            with monkeypatch.context() as fault:
                fault.setattr(store_module, "write_private", disk_full)
                assert first._logout(a.email)[0]["outcome"] == REVOKED
                assert store.load(a.email) is None
            reopened = SessionStore(secrets=store.secrets, root=store.root)
            owner["now"] = make_daemon(reopened, tmp_path, recording_factory(ended))
            owner["now"]._sync_accounts()
            assert not owner["now"].accounts
            return {"error": "no answer"}
        return reply

    monkeypatch.setattr(control, "ask", lost_answer_then_restart)
    result = cli._hand_over(store, a)
    assert retries == [False, True] and result.get("unconfirmed") is True
    assert owner["now"].store.load(a.email) is None


def test_a_retry_still_finishes_a_sign_in_whose_first_save_never_landed(tmp_path, monkeypatch):
    """The other side of the retry rule: when the bar that handled the first
    attempt saw its save fail and nothing else change, the retry replaces and
    ends the older session after all."""
    store = make_store(tmp_path)
    ended = []
    daemon = make_daemon(store, tmp_path, recording_factory(ended))
    assert daemon._adopt(session("old"))["saved"]
    real_set, failures = store.secrets.set, [SaveIndeterminate("helper died before it wrote")]

    def first_save_never_lands(key, value):
        if failures:
            raise failures.pop()
        real_set(key, value)

    monkeypatch.setattr(store.secrets, "set", first_save_never_lands)
    monkeypatch.setattr(control, "ask", answering(daemon))
    reply = cli._hand_over(store, session("new", created_at=2.0))
    assert reply["saved"] is True and ended == ["old"] and store.load("me@proton.me").uid == "new"


def test_a_retry_an_owner_never_saw_stays_unconfirmed(tmp_path, monkeypatch):
    """Y02's rule: an owner that did not handle the first attempt cannot tell
    "never landed" from "landed, signed out, and back to how it looked"."""
    store = make_store(tmp_path)
    ended = []
    daemon = make_daemon(store, tmp_path, recording_factory(ended))
    assert daemon._adopt(session("old"))["saved"]
    monkeypatch.setattr(control, "ask", first_answer_lost(daemon))  # lost before the owner acted
    reply = cli._hand_over(store, session("new", created_at=2.0))
    assert reply.get("unconfirmed") is True and ended == [] and store.load("me@proton.me").uid == "old"


def test_a_retry_never_ends_a_newer_sign_in_the_bar_holds(tmp_path):
    """The same rule for a session the bar holds whose keyring entry is gone."""
    store = make_store(tmp_path)
    ended = []
    daemon = make_daemon(store, tmp_path, recording_factory(ended))
    a, b = session("a"), session("b", created_at=2.0)
    assert daemon._adopt(b)["saved"]
    store.secrets.delete(store_module._key(b.email))
    reply = answering(daemon)({"cmd": "adopt", "session": a.to_dict(), "retry": True})
    assert reply.get("saved") is not True and ended == []  # it cannot tell: nothing is replaced
    reply = answering(daemon)({"cmd": "adopt", "session": a.to_dict(), "retry": True, "expect": ["b"]})
    assert reply.get("saved") is not True and ended == []  # B is held, and its entry gone: no proof


def test_a_later_retirement_write_carries_the_ones_that_failed(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    with monkeypatch.context() as fault:
        fault.setattr(store_module, "write_private", disk_full)
        store.retire(["a"])
    assert "a" in store.retired()  # this process remembers at once
    store.retire(["b"])
    assert {"a", "b"} <= SessionStore(secrets=store.secrets, root=store.root).retired()


@pytest.mark.parametrize("reset", ["refresh", "invalid_cursor"])
def test_a_view_mode_owed_again_stops_steering_the_links(reset):
    """V04: after a Refresh or a fresh cursor the mode may have changed; if
    reading it again fails, links must not keep using the old one."""
    clock = Clock()
    api = FakeProton(clock=clock)
    api.add(60, conversation="c1")
    api.add(30, conversation="c1")
    box = Mailbox(api, "me@proton.me", clock=clock)
    box.full_sync()
    assert box.recent[0]["url"] == INBOX + "/c1"

    api.view_mode = 1
    real_call = api.call

    def unavailable(method, path, **kwargs):
        if path == "/mail/v4/settings":
            raise ProtonError("Internal server error", status=500)
        return real_call(method, path, **kwargs)

    api.call = unavailable
    if reset == "refresh":
        api.queue_event(Refresh=1)
    else:
        api.queue_event(**{"raise": ProtonError("Invalid ID", code=2061, status=400)})
    assert box.poll()
    assert box.settings_pending and box.synced and not box.dirty
    assert {row["url"] for row in box.recent} == {INBOX}

    api.call = real_call
    clock.advance(301)
    box.poll()
    assert {row["kind"] for row in box.recent} == {"message"}
    assert all(row["url"].startswith(INBOX + "/m") for row in box.recent)


def test_status_never_vouches_for_a_snapshot_that_failed_to_save(tmp_path, monkeypatch, capsys):
    """V05: the bar became paused, but writing that snapshot failed; the file
    still said "ok", from the same bar."""
    daemon, _, _ = two_account_daemon(tmp_path)
    daemon.state_path = None
    daemon.tick()
    path = runtime_dir() / "state.json"
    before = json.loads(path.read_text())
    assert before["status"] == "ok" and before["counts"]["1h"] == 13
    for account in daemon.accounts.values():
        daemon._record_failure(account, RateLimited("wait", 600))
    with monkeypatch.context() as fault:
        fault.setattr(daemon_module, "write_private", disk_full)
        daemon.emit()
    assert json.loads(daemon.out.getvalue().splitlines()[-1])["status"] == "throttled"
    assert json.loads(path.read_text()) == before

    monkeypatch.setattr(control, "ask", lambda request, **kw: {"alive": True, "snapshot": daemon._published})
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    assert cli.main(["status", "--window", "1h"]) == 1
    assert capsys.readouterr().out == ""

    daemon.emit()  # storage is back: the unchanged snapshot is written after all
    assert json.loads(path.read_text())["status"] == "throttled"


def test_status_keeps_the_instance_check_of_a_daemon_from_e07cc75(monkeypatch, capsys):
    """W03: during an update the new CLI may ask a daemon still running
    e07cc75, which names its snapshots with an instance ID instead."""
    state = {"type": "state", "status": "ok", "counts": {"1h": 999}, "instance": "old-owner"}
    (runtime_dir() / "state.json").write_text(json.dumps(state))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(control, "ask", lambda request, **kw: {"alive": True, "instance": "running-owner"})
    assert cli.main(["status", "--window", "1h"]) == 1 and capsys.readouterr().out == ""
    monkeypatch.setattr(control, "ask", lambda request, **kw: {"alive": True, "instance": "old-owner"})
    assert cli.main(["status", "--window", "1h"]) == 0 and capsys.readouterr().out.strip() == "999"


# ======================================== fourth pass, on 4e725fd (X01, X02, W02)


class QuietAfterWrite(MemorySecrets):
    """The armed write lands but reports failure, and the next reads find
    nothing: what a locked keyring whose unlock prompt was dismissed answers."""

    def __init__(self):
        super().__init__()
        self.arm = False
        self.empty_reads = 0

    def set(self, key, value):
        super().set(key, value)
        if self.arm:
            self.arm = False
            self.empty_reads = 2
            raise SaveIndeterminate("helper outcome unknown")

    def get(self, key):
        if self.empty_reads:
            self.empty_reads -= 1
            return None
        return super().get(key)


def test_a_keyring_that_answers_nothing_never_gets_a_saved_sign_in_revoked(tmp_path, monkeypatch):
    """X01: the save landed, then every lookup came back empty. Nothing found
    is no proof of a sign-out: the sign-in is kept, not revoked."""
    secrets = QuietAfterWrite()
    store = SessionStore(secrets=secrets, root=make_store(tmp_path).root)
    daemon = make_daemon(store, tmp_path, recording_factory([]))
    assert daemon._adopt(session("old"))["saved"]
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    monkeypatch.setattr(cli, "ProtonClient", FakeProton)
    monkeypatch.setattr(control, "ask", answering(daemon))
    revoked = pending_sign_in(monkeypatch, uid="new", user_id="U")
    secrets.arm = True
    with pytest.raises(cli.LoginFailed):
        cli._login("me@proton.me")
    assert store.load("me@proton.me").uid == "new" and revoked == []


def test_a_retry_never_ends_a_sign_in_it_did_not_expect(tmp_path, monkeypatch):
    """X02: A's save landed but stayed unlisted (its read-back failed); B then
    signed in through another address of the same account, unaware of A. A's
    retry finds itself saved, but B is not one it meant to replace."""
    store = SessionStore(secrets=FlakyKeyring(), root=make_store(tmp_path).root)
    ended, requests = [], []
    daemon = make_daemon(store, tmp_path, recording_factory(ended))
    a = session("a", email="old@proton.me", created_at=10.0)
    b = session("b", email="new@proton.me", created_at=20.0)

    def b_signs_in_meanwhile(request, **kw):
        parsed = control.parse(json.dumps(request).encode())
        requests.append(parsed)
        if len(requests) == 1:
            store.secrets.arm = True
            reply = daemon._answer(parsed)
            assert reply.get("indeterminate") is True and store.accounts() == []
            assert daemon._adopt(b)["saved"] and store.accounts() == [b.email]
            return reply
        return daemon._answer(parsed)

    monkeypatch.setattr(control, "ask", b_signs_in_meanwhile)
    result = cli._hand_over(store, a)
    assert requests[1]["retry"] is True and requests[1]["expect"] == []
    assert result.get("unconfirmed") is True
    assert ended == [] and daemon.accounts[b.email].client.session.uid == "b"


def test_a_retry_never_ends_a_session_only_the_bar_holds(tmp_path, monkeypatch):
    """X02, held: B was adopted through another address while the keyring kept
    the old alias and the account list could not be written, so B is held
    where no lookup finds it. A's retry sees exactly what it expected saved."""
    store = make_store(tmp_path)
    ended, requests = [], []
    daemon = make_daemon(store, tmp_path, recording_factory(ended))
    predecessor = session("old", email="old@proton.me", created_at=1.0)
    attempted = session("a", email="old@proton.me", created_at=2.0)
    later = session("b", email="new@proton.me", created_at=3.0)
    assert daemon._adopt(predecessor)["saved"]
    real_write = store_module.write_private

    def never_lands(key, value):
        raise SaveIndeterminate("helper died before it wrote")

    def cannot_delete(key):
        raise StoreUnavailable("keyring refused")

    def no_index(path, value):
        if path == store._index:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(path, value)

    def b_signs_in_meanwhile(request, **kw):
        parsed = control.parse(json.dumps(request).encode())
        requests.append(parsed)
        if len(requests) == 1:
            with monkeypatch.context() as fault:
                fault.setattr(store.secrets, "set", never_lands)
                reply = daemon._answer(parsed)
            assert reply.get("indeterminate") is True
            with monkeypatch.context() as fault:
                fault.setattr(store.secrets, "delete", cannot_delete)
                fault.setattr(store_module, "write_private", no_index)
                adopted = daemon._adopt(later)
            assert adopted["saved"] and adopted["indexFailed"]
            assert store.accounts() == [predecessor.email]
            return reply
        return daemon._answer(parsed)

    monkeypatch.setattr(control, "ask", b_signs_in_meanwhile)
    result = cli._hand_over(store, attempted)
    assert requests[1]["expect"] == ["old"] and result.get("unconfirmed") is True
    assert "b" not in ended and daemon.accounts[later.email].client.session.uid == "b"


def test_a_restarted_bar_never_vouches_for_a_sign_in_it_did_not_take_over(tmp_path, monkeypatch):
    """W02, the rest: the sign-out's keyring delete failed too, so the
    restarted bar loads the revoked copy. Only the bar that handled the first
    attempt can tell that no sign-out happened since."""
    store = make_store(tmp_path)
    ended, retries = [], []
    first = make_daemon(store, tmp_path, recording_factory(ended))
    a = session("signed-out")
    owner = {"now": first}

    def cannot_delete(key):
        raise StoreUnavailable("keyring locked")

    def lost_answer_then_restart(request, **kw):
        parsed = control.parse(json.dumps(request).encode())
        retries.append(parsed["retry"])
        reply = owner["now"]._answer(parsed)
        if len(retries) == 1:
            assert reply["saved"]
            with monkeypatch.context() as fault:
                fault.setattr(store_module, "write_private", disk_full)
                fault.setattr(store.secrets, "delete", cannot_delete)
                result = first._logout(a.email)[0]
            assert result["outcome"] == REVOKED and result["removeFailed"]
            reopened = SessionStore(secrets=store.secrets, root=store.root)
            owner["now"] = make_daemon(reopened, tmp_path, recording_factory(ended))
            owner["now"]._sync_accounts()
            assert owner["now"].accounts[a.email].client.session.uid == a.uid  # the revoked copy
            return {"error": "no answer"}
        return reply

    monkeypatch.setattr(control, "ask", lost_answer_then_restart)
    result = cli._hand_over(store, a)
    assert retries == [False, True] and result.get("unconfirmed") is True


def test_a_cli_taking_over_never_vouches_for_a_sign_in_it_did_not_handle(tmp_path, monkeypatch):
    """W02, standalone: the bar handled the first attempt, then exited; the
    CLI, owner for the retry, cannot know of the sign-out in between."""
    store = make_store(tmp_path)
    cli_store = SessionStore(secrets=store.secrets, root=store.root)
    ended, calls = [], []
    bar = make_daemon(store, tmp_path, recording_factory(ended))
    a = session("a")

    def cannot_delete(key):
        raise StoreUnavailable("keyring locked")

    def ask(request, **kw):
        parsed = control.parse(json.dumps(request).encode())
        calls.append(parsed["retry"])
        if len(calls) == 1:
            assert bar._answer(parsed)["saved"]
            with monkeypatch.context() as fault:
                fault.setattr(store_module, "write_private", disk_full)
                fault.setattr(store.secrets, "delete", cannot_delete)
                assert bar._logout(a.email)[0]["removeFailed"]
            return {"error": "no answer"}
        return None  # the bar has exited: the CLI is the owner now

    monkeypatch.setattr(control, "ask", ask)
    result = cli._hand_over(cli_store, a)
    assert calls == [False, True] and ended == [a.uid] and result.get("unconfirmed") is True


def test_a_retry_to_the_bar_that_took_the_first_attempt_is_confirmed_before_any_poll(tmp_path):
    """The common case those rules keep: the bar adopted the sign-in, its
    answer was lost, and the retry reaches the same bar before it polled."""
    store = make_store(tmp_path)
    bar = make_daemon(store, tmp_path, recording_factory([]))
    a = session("a")
    assert bar._adopt(a)["saved"]
    assert not bar.accounts[a.email].mailbox.synced
    assert bar._adopt(a, retry=True, expect=[])["saved"]


# ========================================= fifth pass, on eeba389 (Y01 to Y04)


def test_a_cli_keeps_ownership_from_its_first_attempt_to_its_retry(tmp_path, monkeypatch):
    """Y01: between a local first attempt and its retry no other owner may
    act (it could sign the new session out unseen), so the CLI keeps the
    lock, and the retry, seeing its own save, finishes the sign-in."""
    store = SessionStore(secrets=FlakyKeyring(), root=make_store(tmp_path).root)
    store.save(session("old"))
    asked, contested = [], []

    def no_bar(request, **kw):
        asked.append(request)
        store.secrets.arm = True  # the first save lands, and reading it back fails
        return None

    real_adopt = sessions.adopt

    def adopt(*args, **kw):
        if kw.get("retry"):
            contested.append(control.take_lock())  # another would-be owner, right now
        return real_adopt(*args, **kw)

    monkeypatch.setattr(control, "ask", no_bar)
    monkeypatch.setattr(sessions, "adopt", adopt)
    monkeypatch.setattr(cli, "ProtonClient", lambda *a, **kw: FakeProton())
    result = cli._hand_over(store, session("a"))
    assert len(asked) == 1 and contested == [None]
    assert result["saved"] is True and store.load("me@proton.me").uid == "a"
    assert control.take_lock() is not None  # released afterwards


def test_a_bar_restored_to_how_it_looked_never_brings_a_signed_out_sign_in_back(tmp_path, monkeypatch):
    """Y02: A replaced O (whose old alias entry could not be deleted), was
    signed out, and the bar restarted: the account looks exactly as A's
    first attempt found it. That proves nothing about A."""
    store = make_store(tmp_path)
    ended, requests = [], []
    first = make_daemon(store, tmp_path, recording_factory(ended))
    predecessor = session("old", email="old@proton.me")
    a = session("a", email="new@proton.me")
    assert first._adopt(predecessor)["saved"]
    owner = {"now": first}
    real_delete = store.secrets.delete

    def cannot_delete_the_alias(key):
        if key == "session:" + predecessor.email:
            raise StoreUnavailable("keyring refused old alias cleanup")
        return real_delete(key)

    def answer(request, **kw):
        parsed = control.parse(json.dumps(request).encode())
        requests.append(parsed)
        if len(requests) == 1:
            with monkeypatch.context() as fault:
                fault.setattr(store_module, "write_private", disk_full)
                fault.setattr(store.secrets, "delete", cannot_delete_the_alias)
                assert first._answer(parsed)["saved"]
                result = first._answer(control.parse(b'{"cmd":"logout"}'))["results"][0]
                assert result["outcome"] == REVOKED and store.load(a.email) is None
            reopened = SessionStore(secrets=store.secrets, root=store.root)
            assert reopened.account_uids(a) == [predecessor.uid]
            owner["now"] = make_daemon(reopened, tmp_path, recording_factory(ended))
            owner["now"]._sync_accounts()
            return {"error": "no answer"}
        return owner["now"]._answer(parsed)

    monkeypatch.setattr(control, "ask", answer)
    result = cli._hand_over(store, a)
    assert requests[1]["expect"] == [predecessor.uid] and result.get("unconfirmed") is True
    assert owner["now"].store.load(a.email) is None


def test_a_lock_that_cannot_be_opened_never_gets_a_saved_sign_in_revoked(tmp_path, monkeypatch):
    """Y03: the bar saved A, its answer was lost and it exited; the CLI cannot
    even open the ownership lock for the retry. That says nothing about A."""
    store = make_store(tmp_path)
    bar = make_daemon(store, tmp_path, recording_factory([]))
    calls = []

    def read_only(path):
        raise OSError(errno.EROFS, "Read-only file system")

    def ask(request, **kw):
        calls.append(request)
        if len(calls) == 1:
            assert answering(bar)(request)["saved"]
            return {"error": "no answer"}
        return None  # the bar has exited

    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    monkeypatch.setattr(cli, "ProtonClient", FakeProton)
    monkeypatch.setattr(control, "ask", ask)
    monkeypatch.setattr(control, "open_private", read_only)
    revoked = pending_sign_in(monkeypatch, uid="a", user_id="U")
    with pytest.raises(cli.LoginFailed):
        cli._login("me@proton.me")
    assert store.load("me@proton.me").uid == "a" and revoked == []


def test_a_bar_never_confirms_a_sign_in_it_has_seen_fail(tmp_path, monkeypatch, capsys):
    """Y04: between the lost answer and the retry, the bar found the new
    session rejected; the CLI must not print that it signed in."""
    store = make_store(tmp_path)
    bar = make_daemon(store, tmp_path, recording_factory([]))
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    monkeypatch.setattr(cli, "ProtonClient", FakeProton)
    pending_sign_in(monkeypatch, uid="a", user_id="U")
    calls = []

    def ask(request, **kw):
        calls.append(request)
        reply = answering(bar)(request)
        if len(calls) == 1:
            assert reply["saved"]
            account = bar.accounts["me@proton.me"]
            account.client.fail_with = AuthExpired("Session rejected")
            bar._poll(account)
            assert account.status == "auth-expired"
            return {"error": "no answer"}
        return reply

    monkeypatch.setattr(control, "ask", ask)
    code = cli.cmd_login(argparse.Namespace(username="me@proton.me", wait=False))
    assert code != 0 and "Signed in as" not in capsys.readouterr().out


def test_a_sign_out_ends_the_bars_word_for_earlier_sign_ins(tmp_path, monkeypatch):
    """Y04, eviction: A's sign-out could not delete its keyring entry, and 64
    later sign-outs pushed A out of the bounded record of ended sessions.
    The bar's note that it handled A's first attempt must not outlive A's
    sign-out."""
    store = make_store(tmp_path)
    others = [session(f"o{i}", email=f"o{i}@example.test", user_id=f"U{i}") for i in range(RETIRED_KEEP)]
    for other in others:
        store.save(other)
    ended, calls = [], []
    bar = make_daemon(store, tmp_path, recording_factory(ended))
    bar._sync_accounts()
    a = session("a")

    def cannot_delete(key):
        raise StoreUnavailable("keyring refused deletion")

    def ask(request, **kw):
        calls.append(request)
        if len(calls) == 1:
            assert answering(bar)(request)["saved"]
            with monkeypatch.context() as fault:
                fault.setattr(store.secrets, "delete", cannot_delete)
                assert bar._logout(a.email)[0]["removeFailed"]
            for other in others:
                assert bar._logout(other.email)[0]["outcome"] == REVOKED
            assert a.uid not in store.retired()
            assert bar.accounts[a.email].client.session.uid == a.uid
            return {"error": "no answer"}
        return answering(bar)(request)

    monkeypatch.setattr(control, "ask", ask)
    result = cli._hand_over(store, a)
    assert ended.count(a.uid) == 1 and result.get("unconfirmed") is True
