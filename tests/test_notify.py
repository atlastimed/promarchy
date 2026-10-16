from __future__ import annotations

import subprocess

from fakes import NOW

from promarchy import notify
from promarchy.notify import Mark, Notifier, Watermarks, select_new


def msg(mid: str, age: int, unread: bool = True, sender: str = "Alice", subject: str = "Hi") -> dict:
    return {"id": mid, "time": NOW - age, "unread": unread, "senderName": sender, "subject": subject}


def index(*msgs):
    return {m["id"]: m["time"] for m in msgs if m["unread"]}


def test_first_run_announces_nothing():
    a, b = msg("a", 10), msg("b", 20)
    shown, total, mark = select_new(index(a, b), [a, b], None, NOW)
    assert (shown, total) == ([], 0)
    assert mark.time == NOW


def test_only_unseen_newer_mail_is_new():
    a, b, c = msg("a", 10), msg("b", 100), msg("c", 400, unread=False)
    shown, total, mark = select_new(index(a, b, c), [a, b, c], Mark(NOW - 200), NOW)
    assert [m["id"] for m in shown] == ["a", "b"] and total == 2
    assert mark.time == NOW - 10


def test_same_second_arrivals_are_both_announced():
    first = msg("a", 50)
    _, _, mark = select_new(index(first), [first], Mark(NOW - 100), NOW)
    second = msg("b", 50)  # different message, same timestamp second
    shown, total, mark = select_new(index(first, second), [first, second], mark, NOW)
    assert [m["id"] for m in shown] == ["b"] and total == 1
    shown, total, _ = select_new(index(first, second), [first, second], mark, NOW)
    assert total == 0  # nothing announced twice


def test_slightly_out_of_order_delivery_is_not_lost():
    newer = msg("a", 10)
    _, _, mark = select_new(index(newer), [newer], Mark(NOW - 100), NOW)
    late = msg("b", 60)  # stamped earlier, delivered later
    shown, total, _ = select_new(index(newer, late), [newer, late], mark, NOW)
    assert [m["id"] for m in shown] == ["b"] and total == 1


def test_burst_beyond_fresh_details_is_counted_not_dropped():
    burst = [msg(f"m{i}", 10 + i) for i in range(60)]
    shown, total, mark = select_new(index(*burst), burst[:50], Mark(NOW - 1000), NOW)
    assert total == 60 and len(shown) == 50
    again, total_again, _ = select_new(index(*burst), burst[:50], mark, NOW)
    assert total_again == 0


def test_catch_up_is_bounded_after_long_sleep():
    a, b = msg("a", 600), msg("b", 5 * 3600)
    shown, total, _ = select_new(index(a, b), [a, b], Mark(NOW - 86400), NOW)
    assert [m["id"] for m in shown] == ["a"] and total == 1


def test_mark_never_goes_backwards():
    old = msg("a", 5000)
    shown, total, mark = select_new(index(old), [old], Mark(NOW - 100), NOW)
    assert total == 0 and mark.time == NOW - 100


def test_watermarks_persist_and_read_the_old_format(tmp_path):
    path = tmp_path / "notified.json"
    path.write_text('{"legacy@proton.me": 123}')
    marks = Watermarks(path)
    assert marks.get("legacy@proton.me") == Mark(123, {})
    marks.set("me@proton.me", Mark(456, {"x": 456}))
    assert Watermarks(path).get("me@proton.me") == Mark(456, {"x": 456})
    marks.forget("me@proton.me")
    assert Watermarks(path).get("me@proton.me") is None
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_summary_counts_everything_new(monkeypatch):
    calls = []
    monkeypatch.setattr(notify.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(argv))
    Notifier().announce([msg("a", 5)], {}, "https://mail.proton.me/inbox", total=60)
    assert len(calls) == 1 and "60 new messages" in calls[0]


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
