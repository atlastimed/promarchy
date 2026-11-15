"""One message, one toast: Promarchy defers to a browser that has Proton Mail
open and announces mail itself, and a restart never announces mail twice."""

from __future__ import annotations

import io
import json

import pytest
from fakes import NOW, Clock, FakeProton, MemorySecrets

from promarchy import echo
from promarchy.api import NetworkError, Session
from promarchy.avatars import Avatars
from promarchy.daemon import Daemon
from promarchy.echo import HOLD_SECONDS, BrowserEchoes, announced, carries_code
from promarchy.notify import Mark, Watermarks
from promarchy.store import SessionStore


def web(summary, body, ts, source="mail.proton.me", app="Chromium"):
    """A row as Omarchy's notification service stores a browser's toast."""
    return {"key": f"k{ts}", "app": app, "source": source, "summary": summary, "body": body, "ts": ts}


def write(root, live=(), history=()):
    root.mkdir(parents=True, exist_ok=True)
    (root / "live.json").write_text(json.dumps(list(live)))
    (root / "history.json").write_text(json.dumps(list(history)))


def message(subject="Lunch on Friday?", sender="Ada Lovelace", time=NOW, mid="m1"):
    return {"id": mid, "subject": subject, "senderName": sender, "time": time}


def rows_for(tmp_path, *entries):
    write(tmp_path / "n", live=entries)
    return BrowserEchoes(tmp_path / "n").rows()


# ---------------------------------------------------------------- matching


def test_a_browsers_toast_for_the_same_message_is_recognised(tmp_path):
    rows = rows_for(tmp_path, web("New email received", "From: Ada Lovelace - Lunch on Friday?", NOW + 4))
    assert announced(message(), rows)
    assert not announced(message(subject="Quarterly report"), rows)


def test_matching_ignores_language_case_spacing_and_html_escaping(tmp_path):
    rows = rows_for(
        tmp_path,
        web("Nouvel e-mail reçu", "De : Q&amp;A Team - re:  don&#39;t   miss &lt;this&gt;", NOW + 1),
    )
    assert announced(message(subject="Re: Don't miss <this>", sender="Q&A Team"), rows)


def test_a_message_without_a_subject_matches_on_its_sender(tmp_path):
    rows = rows_for(tmp_path, web("New email received", "From: Ada Lovelace - ", NOW))
    assert announced(message(subject=""), rows)
    assert not announced(message(subject="", sender="Grace Hopper"), rows)
    assert not announced(message(subject="", sender=""), rows)
    # ... but not on a toast for another of that sender's messages.
    assert not announced(
        message(subject=""), rows_for(tmp_path, web("New email received", "From: Ada Lovelace - Hi", NOW))
    )


def test_a_toast_is_this_message_only_with_its_sender_and_its_subject(tmp_path):
    """A browser signed in to another account announces other mail: a short
    subject found somewhere in its toast must not silence this message."""
    rows = rows_for(
        tmp_path, web("New email received", "From: Sophie - Booking confirmed, OK? Hi! Re: New", NOW)
    )
    for subject in ("OK", "Hi", "New", "Re", "Booking confirmed, OK? Hi! Re: New"):
        assert not announced(message(subject=subject, sender="Bo"), rows)
    assert not announced(message(subject="Something else", sender="Sophie"), rows)
    assert announced(message(subject="Booking confirmed, OK? Hi! Re: New", sender="Sophie"), rows)


def test_other_languages_separators_and_invisible_characters_still_match(tmp_path):
    german = rows_for(tmp_path, web("Neue E-Mail erhalten", "Von: Ada Lovelace \u2013 Lunch on Friday?", NOW))
    assert announced(message(), german)
    # Promarchy's copy of a subject has lost its zero-width characters (here
    # the joiners of a family emoji); the browser's toast still has them.
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467 photos"
    rows = rows_for(tmp_path, web("New email received", f"From: Ada Lovelace - {family}", NOW))
    assert announced(message(subject=family.replace("\u200d", "")), rows)


def test_an_older_toast_with_the_same_subject_is_not_this_message(tmp_path):
    rows = rows_for(tmp_path, web("New email received", "From: Shop - Your order", NOW - 3600))
    assert not announced(message(subject="Your order"), rows)


def test_only_protons_web_app_counts(tmp_path):
    rows = rows_for(
        tmp_path,
        web("New email received", "From: Ada Lovelace - Lunch on Friday?", NOW, source="web.telegram.org"),
        # Promarchy's own toast: its source is its app name, not a site.
        web("Ada Lovelace", "Lunch on Friday?", NOW, source="Proton Mail", app="Proton Mail"),
    )
    assert rows == []


def test_a_redacted_toast_never_counts_as_the_browsers(tmp_path):
    """Omarchy stores any program's toast with a sign-in code as the same
    placeholder: it says nothing about who announced what."""
    coded = message(subject="Your verification code is 482913")
    rows = rows_for(tmp_path, web(*echo.REDACTED, NOW + 2, source="", app=""))
    assert not announced(coded, rows)
    echoes = BrowserEchoes(tmp_path / "n")
    assert echoes.unannounced("me@proton.me", [coded]) == [coded]
    assert not echoes.present("me@proton.me")


def test_mail_with_a_sign_in_code_is_recognised_broadly():
    for subject in (
        "Your verification code is 482913",
        "G-482913 is your Google verification code",
        "Use code A9F3K2 to sign in",
        "Confirm your order 2026",
    ):
        assert carries_code(message(subject=subject)), subject
    assert not carries_code(message())
    assert not carries_code(message(subject="Minutes of 12 March"))


def test_a_redacted_toast_since_the_mail_arrived_makes_the_wait_pointless(tmp_path):
    write(tmp_path / "n", live=[web(*echo.REDACTED, NOW - 60, source="", app="")])
    echoes = BrowserEchoes(tmp_path / "n")
    assert not echoes.obscured([message()])  # older than the message
    write(tmp_path / "n", live=[web(*echo.REDACTED, NOW + 20, source="", app="")])
    assert echoes.obscured([message()])
    echoes.sent("me@proton.me", [message(mid="m0")], NOW + 21)  # ... unless it is our own
    assert not echoes.obscured([message()])


# ------------------------------------------------------------------ reading


def test_unreadable_or_foreign_files_mean_nothing_was_announced(tmp_path):
    root = tmp_path / "n"
    assert BrowserEchoes(root).rows() == []  # no such directory
    root.mkdir()
    (root / "live.json").write_text("{not json")
    (root / "history.json").write_text(json.dumps({"entries": "elsewhere"}))
    assert BrowserEchoes(root).rows() == []
    (root / "live.json").write_text(
        json.dumps([None, 3, {"ts": "soon"}, {"ts": True, "source": "mail.proton.me"}])
    )
    assert BrowserEchoes(root).rows() == []


def test_only_a_plain_file_is_read(tmp_path):
    import os

    root = tmp_path / "n"
    write(root)
    (root / "live.json").unlink()
    os.mkfifo(root / "live.json")  # opening it would wait for ever
    assert BrowserEchoes(root).rows() == []


def test_an_oversized_file_is_not_parsed(tmp_path, monkeypatch):
    write(tmp_path / "n", live=[web("New email received", "From: A - B", NOW)])
    monkeypatch.setattr(echo, "MAX_BYTES", 10)
    assert BrowserEchoes(tmp_path / "n").rows() == []


def test_rows_are_parsed_again_only_when_a_file_changes(tmp_path, monkeypatch):
    write(tmp_path / "n", live=[web("New email received", "From: A - B", NOW)])
    echoes = BrowserEchoes(tmp_path / "n")
    first = echoes.rows()
    reads = []
    real = BrowserEchoes._read
    monkeypatch.setattr(BrowserEchoes, "_read", lambda self, name: reads.append(name) or real(self, name))
    assert echoes.rows() is first and reads == []
    write(tmp_path / "n", history=[web("New email received", "From: A - B and more", NOW + 9)])
    assert len(echoes.rows()) == 1 and reads == ["live.json", "history.json"]


def test_a_browser_is_there_for_an_account_once_it_announced_its_mail(tmp_path):
    echoes = BrowserEchoes(tmp_path / "n")
    assert not echoes.present("me@proton.me")  # no record at all
    write(tmp_path / "n", history=[web("New email received", "From: Ada Lovelace - Lunch on Friday?", NOW)])
    assert echoes.unannounced("me@proton.me", [message(), message(subject="Other", mid="m2")]) == [
        message(subject="Other", mid="m2")
    ]
    assert echoes.present("me@proton.me")
    echoes.missed("me@proton.me")
    assert not echoes.present("me@proton.me")


def test_a_browser_repeating_our_own_toast_shows_it_is_there(tmp_path):
    write(tmp_path / "n")
    echoes = BrowserEchoes(tmp_path / "n")
    echoes.missed("me@proton.me")
    echoes.sent("me@proton.me", [message()], NOW)
    assert not echoes.present("me@proton.me")
    write(tmp_path / "n", live=[web("New email received", "From: Ada Lovelace - Lunch on Friday?", NOW + 30)])
    assert echoes.present("me@proton.me")


def test_accounts_are_judged_separately(tmp_path):
    write(tmp_path / "n", history=[web("New email received", "From: Ada Lovelace - Lunch on Friday?", NOW)])
    echoes = BrowserEchoes(tmp_path / "n")
    # Before anything is known, a browser that announced any Proton mail may
    # be there for every account.
    assert echoes.present("a@proton.me") and echoes.present("b@proton.me")
    echoes.missed("b@proton.me")  # it never announces b's mail
    assert echoes.unannounced("a@proton.me", [message()]) == []
    assert echoes.present("a@proton.me")
    assert not echoes.present("b@proton.me")
    echoes.forget("a@proton.me")
    assert echoes.present("a@proton.me")  # unknown again: judged by the record


def test_a_redacted_toast_alone_does_not_make_a_browser_present(tmp_path):
    rows_for(tmp_path, web(*echo.REDACTED, NOW, source="", app=""))
    assert not BrowserEchoes(tmp_path / "n").present("me@proton.me")


# ------------------------------------------------------------------- daemon


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
    (tmp_path / "state").mkdir()
    store = SessionStore(secrets=MemorySecrets(), root=tmp_path / "state")
    store.save(Session(uid="u1", access_token="a", refresh_token="r", email="me@proton.me"))
    notifier = RecordingNotifier()
    desk = tmp_path / "omarchy"
    write(desk)
    daemon = Daemon(
        store,
        out=io.StringIO(),
        notifier=notifier,
        watermarks=Watermarks(tmp_path / "notified.json"),
        avatars=Avatars(root=tmp_path / "logos"),
        echoes=BrowserEchoes(desk),
        clock=clock,
        mono=clock,
        client_factory=lambda session, on_refresh=None, **kw: api,
        state_path=str(tmp_path / "state.json"),
    )
    daemon.tick()  # the first sync: old mail is not news

    class Env:
        pass

    e = Env()
    e.clock, e.api, e.notifier, e.daemon, e.desk, e.tmp, e.store = (
        clock,
        api,
        notifier,
        daemon,
        desk,
        tmp_path,
        store,
    )

    def arrive(subject="Lunch on Friday?", sender="Ada Lovelace"):
        clock.advance(31)
        new = api.add(1, subject=subject, sender=sender)
        api.queue_event(Messages=[{"ID": new["ID"], "Action": 1, "Message": {"LabelIDs": ["0"]}}])
        daemon.tick()
        return new

    e.arrive = arrive
    e.browser = lambda new, late=0: web(
        "New email received", f"From: {new['SenderName']} - {new['Subject']}", clock() + late
    )
    return e


def test_without_a_browser_new_mail_is_announced_at_once(env):
    new = env.arrive()
    assert env.notifier.batches == [[new["ID"]]]
    assert env.daemon.held == []


def test_mail_the_browser_already_announced_gets_no_second_toast(env):
    clock, api = env.clock, env.api
    clock.advance(31)
    new = api.add(1)
    write(env.desk, live=[env.browser(new)])
    api.queue_event(Messages=[{"ID": new["ID"], "Action": 1, "Message": {"LabelIDs": ["0"]}}])
    env.daemon.tick()
    assert env.notifier.batches == []
    assert env.daemon.held == []
    # The watermark still moved on: it is not announced later either.
    clock.advance(31)
    write(env.desk)
    api.queue_event(MessageCounts=[{"LabelID": "0", "Unread": 1}])
    env.daemon.tick()
    assert env.notifier.batches == []


def test_with_a_browser_announcing_mail_the_toast_waits_for_it(env):
    first = env.arrive()  # shown by us: nothing said a browser was there
    write(env.desk, history=[env.browser(first)])  # ... and then by the browser too
    second = env.arrive(subject="Minutes attached")
    assert env.notifier.batches == [[first["ID"]]]
    assert [m["id"] for held in env.daemon.held for m in held.messages] == [second["ID"]]
    env.clock.advance(20)
    write(env.desk, history=[env.browser(first)], live=[env.browser(second)])
    env.daemon.tick()
    assert env.notifier.batches == [[first["ID"]]]  # the browser told the user
    assert env.daemon.held == []


def test_a_toast_the_browser_never_shows_is_shown_late_and_the_next_does_not_wait(env):
    first = env.arrive()
    write(env.desk, history=[env.browser(first)])
    second = env.arrive(subject="Minutes attached")
    env.clock.advance(HOLD_SECONDS - 40)
    env.daemon.tick()
    assert env.notifier.batches == [[first["ID"]]]  # still waiting
    env.clock.advance(41)
    env.daemon.tick()
    assert env.notifier.batches == [[first["ID"]], [second["ID"]]]
    third = env.arrive(subject="One more thing")  # the tab is gone: no wait
    assert env.notifier.batches[-1] == [third["ID"]]
    assert env.daemon.held == []


def test_a_waiting_toast_is_dropped_when_the_mail_is_read_meanwhile(env):
    first = env.arrive()
    write(env.desk, history=[env.browser(first)])
    second = env.arrive(subject="Minutes attached")
    second["Unread"] = 0  # read in the browser
    env.clock.advance(31)
    env.api.queue_event(Messages=[{"ID": second["ID"], "Action": 3, "Message": {"LabelIDs": ["0"]}}])
    env.daemon.tick()
    env.clock.advance(HOLD_SECONDS)
    env.daemon.tick()
    assert env.notifier.batches == [[first["ID"]]]
    # Nobody missed anything, so the browser still counts as there.
    third = env.arrive(subject="One more thing")
    assert [m["id"] for held in env.daemon.held for m in held.messages] == [third["ID"]]


def test_a_waiting_toast_is_dropped_when_notifications_are_switched_off(env):
    first = env.arrive()
    write(env.desk, history=[env.browser(first)])
    env.arrive(subject="Minutes attached")
    env.daemon.handle({"cmd": "configure", "settings": {"notify": False}})
    env.clock.advance(HOLD_SECONDS + 1)
    env.daemon.tick()
    assert env.notifier.batches == [[first["ID"]]]
    assert env.daemon.held == []


def successor(env):
    """The daemon the shell starts next: same account, watermark file and desktop."""
    return Daemon(
        env.store,
        out=io.StringIO(),
        notifier=env.notifier,
        watermarks=Watermarks(env.tmp / "notified.json"),
        avatars=Avatars(root=env.tmp / "logos"),
        echoes=BrowserEchoes(env.desk),
        clock=env.clock,
        mono=env.clock,
        client_factory=lambda session, on_refresh=None, **kw: env.api,
        state_path=str(env.tmp / "state2.json"),
    )


def test_a_toast_still_waiting_when_the_daemon_stops_is_announced_by_the_next(env):
    """The shell that shows toasts may be going down with the daemon, so a
    waiting toast is not shown on the way out: the watermark never covered
    it, and whoever runs next finds the mail again."""
    first = env.arrive()
    write(env.desk, history=[env.browser(first)])
    second = env.arrive(subject="Minutes attached")
    env.daemon.run(io.StringIO(""))  # stdin at EOF: it quits straight away
    assert env.notifier.batches == [[first["ID"]]]
    nxt = successor(env)
    nxt.tick()
    env.clock.advance(HOLD_SECONDS + 1)  # it waits for the browser in its turn
    nxt.tick()
    assert env.notifier.batches == [[first["ID"]], [second["ID"]]]
    env.clock.advance(31)
    env.api.queue_event(MessageCounts=[{"LabelID": "0", "Unread": 2}])
    nxt.tick()
    assert env.notifier.batches == [[first["ID"]], [second["ID"]]]  # once


def test_a_killed_daemon_loses_no_waiting_toast(env):
    first = env.arrive()
    write(env.desk, history=[env.browser(first)])
    second = env.arrive(subject="Minutes attached")
    nxt = successor(env)  # the first one never got to run its exit path
    nxt.tick()
    env.clock.advance(HOLD_SECONDS + 1)
    nxt.tick()
    assert env.notifier.batches == [[first["ID"]], [second["ID"]]]


def test_a_settled_toast_is_not_announced_again_after_a_restart(env):
    first = env.arrive()
    write(env.desk, history=[env.browser(first)])
    second = env.arrive(subject="Minutes attached")
    write(env.desk, history=[env.browser(first)], live=[env.browser(second)])
    env.daemon.tick()  # the browser announced it: settled
    write(env.desk, history=[env.browser(first)])  # ... and its toast has since left the record
    nxt = successor(env)
    nxt.tick()
    env.clock.advance(HOLD_SECONDS + 1)
    nxt.tick()
    assert env.notifier.batches == [[first["ID"]]]


def test_mail_with_a_sign_in_code_never_waits(env):
    first = env.arrive()
    write(env.desk, history=[env.browser(first)])
    code = env.arrive(subject="Your verification code is 482913")
    assert env.notifier.batches == [[first["ID"]], [code["ID"]]]
    assert env.daemon.held == []


def test_a_redacted_toast_ends_the_wait_without_writing_the_browser_off(env):
    first = env.arrive()
    write(env.desk, history=[env.browser(first)])
    second = env.arrive(subject="Minutes attached")
    env.clock.advance(10)
    hidden = web(*echo.REDACTED, env.clock(), source="", app="")
    write(env.desk, history=[env.browser(first)], live=[hidden])
    env.daemon.tick()
    assert env.notifier.batches == [[first["ID"]], [second["ID"]]]  # shown: nothing can tell whose it was
    third = env.arrive(subject="One more thing")
    assert [m["id"] for held in env.daemon.held for m in held.messages] == [third["ID"]]  # still there


def test_a_waiting_toast_survives_signing_in_again(env):
    """A new sign-in starts with an empty mailbox: that is not "read"."""
    first = env.arrive()
    write(env.desk, history=[env.browser(first)])
    second = env.arrive(subject="Minutes attached")
    env.store.save(Session(uid="u2", access_token="a2", refresh_token="r2", email="me@proton.me"))
    env.api.fail_with = NetworkError("offline")  # the new session has not synced yet
    env.clock.advance(6)
    env.daemon.tick()
    assert [m["id"] for held in env.daemon.held for m in held.messages] == [second["ID"]]
    env.api.fail_with = None
    env.clock.advance(HOLD_SECONDS)
    env.daemon.handle({"cmd": "refresh"})
    env.daemon.tick()
    env.daemon.tick()
    assert env.notifier.batches.count([second["ID"]]) == 1


def test_a_browser_that_announces_part_of_a_burst_is_still_there(env):
    first = env.arrive()
    write(env.desk, history=[env.browser(first)])
    env.clock.advance(31)
    one = env.api.add(2, subject="Minutes attached")
    two = env.api.add(1, subject="Invoice 12")
    env.api.queue_event(Messages=[{"ID": two["ID"], "Action": 1, "Message": {"LabelIDs": ["0"]}}])
    env.daemon.tick()
    write(env.desk, history=[env.browser(first)], live=[env.browser(one)])
    env.clock.advance(HOLD_SECONDS + 1)
    env.daemon.tick()
    assert env.notifier.batches == [[first["ID"]], [two["ID"]]]  # only the one it skipped
    third = env.arrive(subject="One more thing")
    assert [m["id"] for held in env.daemon.held for m in held.messages] == [third["ID"]]


def test_a_broken_record_never_costs_a_notification(env, monkeypatch):
    def boom(self):
        raise RuntimeError("unexpected")

    first = env.arrive()
    write(env.desk, history=[env.browser(first)])
    second = env.arrive(subject="Minutes attached")  # waits for the browser
    monkeypatch.setattr(BrowserEchoes, "rows", boom)
    env.daemon.tick()
    assert env.notifier.batches == [[first["ID"]], [second["ID"]]]  # shown rather than lost
    third = env.arrive(subject="One more thing")
    assert env.notifier.batches[-1] == [third["ID"]]


def test_a_restart_takes_the_watermark_its_predecessor_left(env):
    """The shell starts the new daemon while the old one may still be
    announcing mail: what the old one wrote while we waited for its lock must
    count, or the same mail is announced twice."""
    clock, api = env.clock, env.api
    marks = Watermarks(env.tmp / "notified.json")
    successor = Daemon(
        env.store,
        out=io.StringIO(),
        notifier=env.notifier,
        watermarks=marks,  # read here, before the predecessor's last sync
        avatars=Avatars(root=env.tmp / "logos"),
        clock=clock,
        mono=clock,
        client_factory=lambda session, on_refresh=None, **kw: api,
        state_path=str(env.tmp / "state2.json"),
    )
    new = env.arrive()  # the predecessor announces it and moves the watermark
    assert env.notifier.batches == [[new["ID"]]]
    assert marks.get("me@proton.me") != Watermarks(env.tmp / "notified.json").get("me@proton.me")
    successor.run(io.StringIO(""))
    successor.tick()
    assert env.notifier.batches == [[new["ID"]]]
    assert isinstance(marks.get("me@proton.me"), Mark)
