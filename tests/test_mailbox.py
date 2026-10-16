from __future__ import annotations

from fakes import NOW, Clock, FakeProton

from promarchy.api import NetworkError, ProtonError
from promarchy.mailbox import (
    MAX_PAGES,
    PAGE_LIMIT,
    Mailbox,
    clean_text,
    label_params,
    parse_categories,
    summarize,
)

HOUR = 3600
DAY = 86400


def make(api: FakeProton, clock: Clock | None = None, **kw) -> Mailbox:
    return Mailbox(api, "me@proton.me", clock=clock or Clock(), **kw)


def test_parse_categories():
    assert parse_categories("all") == ()
    assert parse_categories("") == ()
    assert parse_categories("Primary, updates") == ("primary", "updates")
    assert parse_categories("primary+primary,bogus") == ("primary",)
    assert parse_categories(["social", "all"]) == ()
    assert parse_categories(None) == ()


def test_label_params_use_inbox_and_any_category():
    assert label_params(()) == [("LabelID", "0")]
    assert label_params(("primary", "updates")) == [
        ("LabelID[]", "0"),
        ("LabelID[]", "24"),
        ("LabelID[]", "22"),
    ]


def test_counts_per_window():
    api = FakeProton()
    for age in (60, 30 * 60, 5 * HOUR, 2 * DAY, 6 * DAY, 9 * DAY):
        api.add(age)
    api.add(10, unread=False)
    box = make(api)
    box.full_sync()
    assert box.counts(NOW) == {"1h": 2, "24h": 3, "7d": 5, "all": 6}


def test_counts_age_out_without_requests():
    api = FakeProton()
    api.add(50 * 60)  # 50 minutes old
    clock = Clock()
    box = make(api, clock)
    box.full_sync()
    calls = len(api.calls)
    assert box.counts()["1h"] == 1
    assert box.next_boundary() == NOW - 50 * 60 + HOUR + 1
    clock.advance(11 * 60)
    assert box.counts()["1h"] == 0
    assert box.counts()["24h"] == 1
    assert len(api.calls) == calls


def test_category_filter_applies_to_counts_list_and_totals():
    api = FakeProton()
    api.add(60, labels=("0", "24"), subject="primary")
    api.add(60, labels=("0", "25"), subject="newsletter")
    api.add(60, labels=("6", "24"), subject="archived primary")
    box = make(api, categories=("primary",))
    box.full_sync()
    assert box.counts(NOW)["24h"] == 1
    assert box.unread_total == 1
    assert [m["subject"] for m in box.recent] == ["primary"]


def test_conversations_give_exact_counts_and_distinct_rows():
    api = FakeProton()
    for i in range(30):  # a busy thread that would swamp a message sample
        api.add(100 + i, conversation="busy", unread=i < 2, subject="Re: plan")
    api.add(200, conversation="quiet", subject="other")
    box = make(api, recent_limit=8)
    box.full_sync()
    assert [(r["conversationId"], r["threadCount"]) for r in box.recent] == [("busy", 30), ("quiet", 1)]
    busy = box.recent[0]
    assert busy["kind"] == "conversation" and busy["unread"] is True and busy["unreadIds"] == ["busy"]
    assert busy["url"] == "https://mail.proton.me/inbox/busy"


def test_conversation_mark_read_uses_the_conversation_endpoint():
    api = FakeProton()
    api.add(100, conversation="c1")
    api.add(90, conversation="c1")
    box = make(api)
    box.full_sync()
    box.mark_read(["c1"], kind="conversation")
    assert [c for c in api.calls if c[0] == "PUT"][0][1] == "/mail/v4/conversations/read"
    assert box.counts(NOW)["1h"] == 0


def test_threads_off_lists_every_message():
    api = FakeProton()
    api.add(200, conversation="c1")
    api.add(100, conversation="c1")
    box = make(api, threads=False)
    box.full_sync()
    assert len(box.recent) == 2


def test_recent_limit_respected_and_zero_disables():
    api = FakeProton()
    for age in range(1, 20):
        api.add(age * 60)
    box = make(api, recent_limit=5)
    box.full_sync()
    assert len(box.recent) == 5
    assert box.recent[0]["time"] > box.recent[-1]["time"]
    box.configure(categories=(), recent_limit=0, threads=True)
    box.resync()
    assert box.recent == []


def test_irrelevant_event_costs_one_request():
    api = FakeProton()
    api.add(60)
    box = make(api)
    box.full_sync()
    before = len(api.calls)
    api.queue_event(Messages=[{"ID": "draft1", "Action": 2, "Message": {"LabelIDs": ["1", "8"]}}])
    assert box.poll() is False
    assert len(api.calls) == before + 1


def test_new_inbox_mail_triggers_resync():
    api = FakeProton()
    box = make(api)
    box.full_sync()
    new = api.add(5, subject="fresh")
    api.queue_event(Messages=[{"ID": new["ID"], "Action": 1, "Message": {"LabelIDs": ["0", "5"]}}])
    assert box.poll() is True
    assert box.counts(NOW)["1h"] == 1
    assert box.fresh[0]["subject"] == "fresh"


def test_flag_change_on_known_message_triggers_resync():
    api = FakeProton()
    message = api.add(60)
    box = make(api)
    box.full_sync()
    message["Unread"] = 0  # read in the web app
    api.queue_event(Messages=[{"ID": message["ID"], "Action": 3, "Message": {"Unread": 0}}])
    assert box.poll() is True
    assert box.counts(NOW)["1h"] == 0


def test_inbox_count_change_triggers_resync():
    api = FakeProton()
    box = make(api)
    box.full_sync()
    api.queue_event(MessageCounts=[{"LabelID": "0", "Unread": 3}])
    assert box.poll() is True


def test_more_flag_drains_event_pages():
    api = FakeProton()
    box = make(api)
    box.full_sync()
    api.queue_event(More=1)
    api.queue_event(More=1)
    api.queue_event()
    box.poll()
    assert api.endpoint_calls("/core/v4/events/latest") == 1
    assert len(api.events) == 0


def test_rejected_cursor_restarts_from_latest():
    api = FakeProton()
    box = make(api)
    box.full_sync()
    api.queue_event(**{"raise": ProtonError("Invalid ID", code=2061, status=400)})
    assert box.poll() is True
    assert api.endpoint_calls("/core/v4/events/latest") == 2


def test_network_errors_propagate_for_backoff():
    api = FakeProton()
    box = make(api)
    box.full_sync()
    api.queue_event(**{"raise": NetworkError("offline")})
    try:
        box.poll()
    except NetworkError:
        pass
    else:
        raise AssertionError("expected NetworkError")


def test_paging_uses_anchor_and_caps():
    api = FakeProton()
    for i in range(PAGE_LIMIT * MAX_PAGES + 40):
        api.add(60 + i)
    box = make(api)
    box.full_sync()
    anchors = [c for c in api.calls if c[1] == "/mail/v4/messages" and any(k == "AnchorID" for k, _ in c[2])]
    assert len(anchors) == MAX_PAGES - 1
    assert len(box.unread_times) == PAGE_LIMIT * MAX_PAGES
    assert box.capped() is True
    assert box.counts(NOW)["7d"] == PAGE_LIMIT * MAX_PAGES


def test_paging_stops_when_complete():
    api = FakeProton()
    for i in range(PAGE_LIMIT + 10):
        api.add(60 + i)
    box = make(api)
    box.full_sync()
    assert len(box.unread_times) == PAGE_LIMIT + 10
    assert box.capped() is False


def test_mark_read_updates_counts():
    api = FakeProton()
    a = api.add(60)
    api.add(120)
    box = make(api)
    box.full_sync()
    box.mark_read([a["ID"], a["ID"], ""])
    put = [c for c in api.calls if c[0] == "PUT"]
    assert put[0][3] == {"IDs": [a["ID"]]}
    assert box.counts(NOW)["1h"] == 1


def test_web_urls_follow_view_mode():
    api = FakeProton()
    api.add(60, conversation="conv")
    box = make(api, threads=False)
    box.full_sync()
    row = box.recent[0]
    assert row["url"] == "https://mail.proton.me/inbox/conv/m1"
    api.view_mode = 1
    box2 = make(api, threads=False)
    box2.full_sync()
    assert box2.recent[0]["url"] == "https://mail.proton.me/inbox/m1"
    assert box.web_url() == "https://mail.proton.me/inbox"


def test_summarize_cleans_untrusted_text():
    message = {
        "ID": "x",
        "Subject": "Hi‮ there\nfriend\x07",
        "Sender": {"Name": "", "Address": "bob@example.com"},
        "LabelIDs": ["0", "10", "26"],
        "Time": 5,
        "Unread": 1,
    }
    row = summarize(message, "me")
    assert row["subject"] == "Hi there friend"
    assert row["senderName"] == "bob@example.com"
    assert row["starred"] is True
    assert row["category"] == "transactions"
    assert clean_text("a" * 500, 10) == "a" * 10
