from __future__ import annotations

import subprocess

from fakes import NOW

from promarchy import notify
from promarchy.notify import Notifier, Watermarks, select_new


def msg(mid: str, age: int, unread: bool = True, sender: str = "Alice", subject: str = "Hi") -> dict:
    return {"id": mid, "time": NOW - age, "unread": unread, "senderName": sender, "subject": subject}


def test_first_run_announces_nothing():
    new, mark = select_new([msg("a", 10), msg("b", 20)], None, NOW)
    assert new == []
    assert mark == NOW


def test_only_newer_than_watermark():
    fresh = [msg("a", 10), msg("b", 100), msg("c", 400, unread=False)]
    new, mark = select_new(fresh, NOW - 200, NOW)
    assert [m["id"] for m in new] == ["a", "b"]
    assert mark == NOW - 10


def test_catch_up_is_bounded_after_long_sleep():
    fresh = [msg("a", 600), msg("b", 5 * 3600)]
    new, _ = select_new(fresh, NOW - 86400, NOW)
    assert [m["id"] for m in new] == ["a"]


def test_watermark_never_goes_backwards():
    new, mark = select_new([msg("a", 500)], NOW - 100, NOW)
    assert new == []
    assert mark == NOW - 100


def test_watermarks_persist(tmp_path):
    path = tmp_path / "notified.json"
    marks = Watermarks(path)
    marks.set("me@proton.me", 123)
    assert Watermarks(path).get("me@proton.me") == 123
    marks.forget("me@proton.me")
    assert Watermarks(path).get("me@proton.me") is None
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_toast_argv_keeps_untrusted_text_as_data(monkeypatch):
    calls = []
    monkeypatch.setattr(notify.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(argv))
    n = Notifier(icon="/x/icon.svg")
    n.announce(
        [msg("a", 5, sender="--exec rm", subject="$(reboot)")],
        {"a": "https://mail.proton.me/inbox/c/a"},
        "inbox",
    )
    argv = calls[0]
    assert argv[0] == "/usr/bin/omarchy-notification-send"
    exec_at = argv.index("--exec")
    assert argv[exec_at - 2 : exec_at] == ["\u2010-exec rm", "$(reboot)"]
    assert argv[exec_at + 1 :] == ["omarchy-launch-browser", "https://mail.proton.me/inbox/c/a"]


def test_burst_collapses_into_one_summary(monkeypatch):
    calls = []
    monkeypatch.setattr(notify.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(argv))
    batch = [msg(str(i), i, sender=f"S{i % 5}") for i in range(8)]
    Notifier().announce(batch, {}, "https://mail.proton.me/inbox")
    assert len(calls) == 1
    assert "8 new messages" in calls[0]
    assert calls[0][-1] == "https://mail.proton.me/inbox"


def test_flag_shaped_sender_and_subject_cannot_become_options(monkeypatch):
    calls = []
    monkeypatch.setattr(notify.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(argv))
    Notifier().announce(
        [msg("a", 5, sender="--urgency=critical", subject="-r 5")], {"a": "https://mail.proton.me/x"}, "inbox"
    )
    argv = calls[0]
    exec_at = argv.index("--exec")
    title, body = argv[exec_at - 2 : exec_at]
    assert not title.startswith("-") and not body.startswith("-")
    assert title.endswith("-urgency=critical") and body.endswith("r 5")
    assert "--urgency=critical" not in argv
