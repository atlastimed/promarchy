"""One account's view of the inbox, kept current with Proton's event stream.

Idle cost is a single small ``GET /core/v4/events/<cursor>`` per poll. Only
when an event touches the inbox (new mail, something read or moved) do we
re-query, and then just three cheap calls: unread messages from the last
seven days, the newest messages for the dropdown, and the unread totals.

Window counts (1h / 24h / 7d) are computed locally from message timestamps,
so they stay exact as mail ages out of a window without any extra requests.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from typing import Any

from .api import AuthExpired, NetworkError, ProtonClient, ProtonError, RateLimited

INBOX = "0"
STARRED = "10"
CATEGORY_LABELS = {
    "social": "20",
    "promotions": "21",
    "updates": "22",
    "forums": "23",
    "primary": "24",
    "newsletters": "25",
    "transactions": "26",
}
LABEL_CATEGORY = {label: name for name, label in CATEGORY_LABELS.items()}

WINDOWS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}
MAX_WINDOW = max(WINDOWS.values())

PAGE_LIMIT = 150
MAX_PAGES = 5  # beyond 750 unread in a week, counts saturate (shown as "750+")
MAX_EVENT_PAGES = 10
MAX_RECENT = 50
FRESH_KEEP = 50

WEB_BASE = "https://mail.proton.me"
VIEW_MODE_MESSAGES = 1

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁦-⁩]")


def parse_categories(value: Any) -> tuple[str, ...]:
    """Accept "all", "primary", "primary,updates" or a list. Empty means all."""
    if isinstance(value, str):
        items: Iterable[Any] = re.split(r"[\s,+]+", value)
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        items = ()
    names = []
    for item in items:
        name = str(item).strip().lower()
        if name == "all":
            return ()
        if name in CATEGORY_LABELS and name not in names:
            names.append(name)
    return tuple(names)


def clean_text(value: Any, limit: int) -> str:
    text = _CONTROL.sub("", str(value or "")).replace("\n", " ").strip()
    return text[:limit]


def label_params(categories: tuple[str, ...]) -> list[tuple[str, str]]:
    if not categories:
        return [("LabelID", INBOX)]
    # Inbox AND any of the categories (verified against the live API).
    return [("LabelID[]", INBOX)] + [("LabelID[]", CATEGORY_LABELS[c]) for c in categories]


def summarize(message: dict, account: str) -> dict:
    sender = message.get("Sender") if isinstance(message.get("Sender"), dict) else {}
    labels = [str(label) for label in message.get("LabelIDs") or []]
    category = next((LABEL_CATEGORY[label] for label in labels if label in LABEL_CATEGORY), "")
    name = clean_text(sender.get("Name") or message.get("SenderName"), 120)
    address = clean_text(sender.get("Address") or message.get("SenderAddress"), 200)
    return {
        "id": str(message.get("ID") or ""),
        "conversationId": str(message.get("ConversationID") or ""),
        "account": account,
        "senderName": name or address,
        "senderAddress": address,
        "subject": clean_text(message.get("Subject"), 300),
        "time": int(message.get("Time") or 0),
        "unread": bool(message.get("Unread")),
        "starred": STARRED in labels,
        "attachments": int(message.get("NumAttachments") or 0),
        "category": category,
        "senderImage": bool(message.get("DisplaySenderImage")),
        "threadCount": 1,
        "unreadIds": [str(message.get("ID"))] if message.get("Unread") else [],
    }


def group_threads(messages: list[dict], limit: int) -> list[dict]:
    """Collapse a busy conversation into its newest message plus a count."""
    rows: list[dict] = []
    by_conversation: dict[str, dict] = {}
    for message in messages:
        key = message["conversationId"] or message["id"]
        row = by_conversation.get(key)
        if row is None:
            if len(rows) >= limit:
                continue
            row = dict(message, unreadIds=list(message["unreadIds"]))
            by_conversation[key] = row
            rows.append(row)
            continue
        row["threadCount"] += 1
        row["unreadIds"].extend(message["unreadIds"])
        row["unread"] = row["unread"] or message["unread"]
        row["starred"] = row["starred"] or message["starred"]
    return rows


class Mailbox:
    def __init__(
        self,
        client: ProtonClient,
        account: str,
        *,
        categories: tuple[str, ...] = (),
        recent_limit: int = 8,
        threads: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client = client
        self.account = account
        self.categories = categories
        self.recent_limit = recent_limit
        self.threads = threads
        self.clock = clock

        self.event_id: str | None = None
        self.view_mode = 0
        self.unread_times: dict[str, int] = {}
        self.window_total = 0
        self.window_complete = True
        self.unread_total = 0
        self.recent: list[dict] = []
        self.fresh: list[dict] = []  # newest unread (matching filters), for notifications
        self.synced = False

    # ------------------------------------------------------------ settings

    def configure(self, *, categories: tuple[str, ...], recent_limit: int, threads: bool) -> bool:
        """Apply new settings. True when a resync is needed to honour them."""
        changed = (categories, recent_limit, threads) != (self.categories, self.recent_limit, self.threads)
        self.categories, self.recent_limit, self.threads = categories, recent_limit, threads
        return changed and self.synced

    # ---------------------------------------------------------------- sync

    def full_sync(self) -> None:
        # Take the event cursor *before* reading data so nothing slips between.
        if self.event_id is None:
            self.event_id = str(self.client.call("GET", "/core/v4/events/latest")["EventID"])
            self._load_settings()
        self.resync()

    def resync(self) -> None:
        now = int(self.clock())
        self._sync_unread_window(now)
        self._sync_recent()
        self._sync_totals()
        self.synced = True

    def poll(self) -> bool:
        """Advance the event cursor. True when state was re-read."""
        if self.event_id is None or not self.synced:
            self.full_sync()
            return True
        relevant = False
        for _ in range(MAX_EVENT_PAGES):
            try:
                event = self.client.call("GET", f"/core/v4/events/{self.event_id}")
            except (NetworkError, RateLimited, AuthExpired):
                raise
            except ProtonError:
                # The cursor expired or was rejected: start from a fresh one.
                self.event_id = None
                self.full_sync()
                return True
            self.event_id = str(event.get("EventID") or self.event_id)
            if event.get("Refresh") or self.event_is_relevant(event):
                relevant = True
            if not event.get("More"):
                break
        if relevant:
            self.resync()
        return relevant

    def event_is_relevant(self, event: dict) -> bool:
        known = set(self.unread_times)
        known.update(row["id"] for row in self.recent)
        for item in event.get("Messages") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("ID") or "") in known:
                return True
            message = item.get("Message") if isinstance(item.get("Message"), dict) else {}
            for key in ("LabelIDs", "LabelIDsAdded", "LabelIDsRemoved"):
                if INBOX in [str(label) for label in message.get(key) or []]:
                    return True
        watched = {INBOX} | {CATEGORY_LABELS[c] for c in self.categories}
        for count in event.get("MessageCounts") or []:
            if isinstance(count, dict) and str(count.get("LabelID")) in watched:
                return True
        return False

    def mark_read(self, ids: list[str]) -> None:
        ids = [i for i in dict.fromkeys(ids) if i]
        for start in range(0, len(ids), 100):
            self.client.call("PUT", "/mail/v4/messages/read", body={"IDs": ids[start : start + 100]})
        if ids:
            self.resync()

    # -------------------------------------------------------------- derive

    def counts(self, now: int | None = None) -> dict[str, int]:
        now = int(self.clock() if now is None else now)
        times = self.unread_times.values()
        oldest = min(times) if self.unread_times else now
        result: dict[str, int] = {}
        for name, span in WINDOWS.items():
            start = now - span
            if self.window_complete or oldest <= start:
                result[name] = sum(1 for t in times if t >= start)
            else:
                # More unread mail than we page through: report what we hold.
                result[name] = len(self.unread_times)
        result["all"] = self.unread_total
        return result

    def capped(self) -> bool:
        return not self.window_complete

    def next_boundary(self, now: int | None = None) -> int | None:
        """When the next message ages out of a window (no API call needed)."""
        now = int(self.clock() if now is None else now)
        best: int | None = None
        for span in WINDOWS.values():
            inside = [t for t in self.unread_times.values() if t >= now - span]
            if inside:
                candidate = min(inside) + span + 1
                best = candidate if best is None else min(best, candidate)
        return best

    def web_url(self, row: dict | None = None) -> str:
        if not row:
            return f"{WEB_BASE}/inbox"
        if self.view_mode == VIEW_MODE_MESSAGES or not row.get("conversationId"):
            return f"{WEB_BASE}/inbox/{row['id']}"
        return f"{WEB_BASE}/inbox/{row['conversationId']}/{row['id']}"

    # ----------------------------------------------------------- internals

    def _load_settings(self) -> None:
        try:
            settings = self.client.call("GET", "/mail/v4/settings").get("MailSettings") or {}
            self.view_mode = int(settings.get("ViewMode") or 0)
        except ProtonError:
            self.view_mode = 0

    def _sync_unread_window(self, now: int) -> None:
        base = label_params(self.categories) + [
            ("Unread", "1"),
            ("Begin", str(now - MAX_WINDOW)),
            ("Limit", str(PAGE_LIMIT)),
            ("Sort", "Time"),
            ("Desc", "1"),
        ]
        times: dict[str, int] = {}
        fresh: list[dict] = []
        total = 0
        anchor: tuple[int, str] | None = None
        for page in range(MAX_PAGES):
            params = list(base)
            if anchor:
                params += [("Anchor", str(anchor[0])), ("AnchorID", anchor[1])]
            resp = self.client.call("GET", "/mail/v4/messages", params=params)
            messages = [m for m in resp.get("Messages") or [] if isinstance(m, dict) and m.get("ID")]
            if page == 0:
                total = int(resp.get("Total") or 0)
            for message in messages:
                mid = str(message["ID"])
                if mid in times:
                    continue
                times[mid] = int(message.get("Time") or 0)
                if len(fresh) < FRESH_KEEP:
                    fresh.append(summarize(message, self.account))
            if len(messages) < PAGE_LIMIT or len(times) >= total:
                break
            last = messages[-1]
            anchor = (int(last.get("Time") or 0), str(last["ID"]))
        self.unread_times = times
        self.window_total = max(total, len(times))
        self.window_complete = len(times) >= total
        self.fresh = fresh

    def _sync_recent(self) -> None:
        if self.recent_limit <= 0:
            self.recent = []
            return
        fetch = min(100, self.recent_limit * (3 if self.threads else 1))
        params = label_params(self.categories) + [
            ("Limit", str(fetch)),
            ("Sort", "Time"),
            ("Desc", "1"),
        ]
        resp = self.client.call("GET", "/mail/v4/messages", params=params)
        rows = [
            summarize(m, self.account)
            for m in resp.get("Messages") or []
            if isinstance(m, dict) and m.get("ID")
        ]
        for row in rows:
            row["url"] = self.web_url(row)
        if self.threads:
            self.recent = group_threads(rows, self.recent_limit)
        else:
            self.recent = rows[: self.recent_limit]

    def _sync_totals(self) -> None:
        resp = self.client.call("GET", "/mail/v4/messages/count", params={"OnlyInInboxForCategories": 1})
        unread = {
            str(c.get("LabelID")): int(c.get("Unread") or 0)
            for c in resp.get("Counts") or []
            if isinstance(c, dict)
        }
        if self.categories:
            self.unread_total = sum(unread.get(CATEGORY_LABELS[c], 0) for c in self.categories)
        else:
            self.unread_total = unread.get(INBOX, 0)
