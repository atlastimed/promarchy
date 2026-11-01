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
SETTINGS_RETRY = 300  # seconds before asking again after an odd settings reply

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
        "kind": "message",
        "unreadIds": [str(message.get("ID"))] if message.get("Unread") else [],
    }


def summarize_conversation(conversation: dict, account: str) -> dict:
    """One dropdown row per conversation, with the Inbox's own numbers.

    A conversation can span Inbox, Sent and Archive. Proton reports per-folder
    context in `Labels` (and, for the folder you queried, at the top level),
    so we take the Inbox entry first, then the top-level context, and only
    then whole-conversation totals.
    """
    cid = str(conversation.get("ID") or "")
    contexts = {
        str(entry.get("ID")): entry for entry in conversation.get("Labels") or [] if isinstance(entry, dict)
    }
    inbox = contexts.get(INBOX, {})

    def context(field: str, whole: str) -> int:
        for source, key in ((inbox, field), (conversation, field), (conversation, whole)):
            value = source.get(key)
            if isinstance(value, (int, float)):
                return int(value)
        return 0

    labels = set(contexts) | {str(label) for label in conversation.get("LabelIDs") or []}
    category = LABEL_CATEGORY.get(str(conversation.get("CategoryID") or ""), "")
    if not category:
        category = next((LABEL_CATEGORY[label] for label in labels if label in LABEL_CATEGORY), "")
    senders = [s for s in conversation.get("Senders") or [] if isinstance(s, dict)]
    names = [clean_text(s.get("Name") or s.get("Address"), 120) for s in senders]
    first = senders[0] if senders else {}
    unread = context("ContextNumUnread", "NumUnread")
    return {
        "id": cid,
        "conversationId": cid,
        "account": account,
        "senderName": clean_text(", ".join(dict.fromkeys(n for n in names if n)), 200),
        "senderAddress": clean_text(first.get("Address"), 200),
        "subject": clean_text(conversation.get("Subject"), 300),
        "time": context("ContextTime", "Time"),
        "unread": unread > 0,
        "starred": STARRED in labels,
        "attachments": context("ContextNumAttachments", "NumAttachments"),
        "category": category,
        "senderImage": bool(first.get("DisplaySenderImage", conversation.get("DisplaySenderImage"))),
        "threadCount": max(1, context("ContextNumMessages", "NumMessages")),
        "kind": "conversation",
        "unreadIds": [cid] if unread else [],
    }


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
        # Proton's mail settings are owed until read once, and again after a
        # server Refresh or an explicit refresh; kept apart from the event
        # cursor so a failed read is retried instead of forgotten.
        self.settings_pending = True
        self.settings_retry_at = 0.0
        # Whether Proton ever told us its view mode. Until then a row link
        # could open the wrong thing, so rows link to the inbox.
        self.settings_known = False
        self.unread_times: dict[str, int] = {}
        self.window_complete = True
        self.unread_total = 0
        self.recent: list[dict] = []
        self.fresh: list[dict] = []  # newest unread (matching filters), for notifications
        self.synced = False
        # Set while a resync is owed. The event cursor moves before we re-read,
        # so a failed re-read must be retried even if no new event arrives.
        self.dirty = False

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
            # A fresh cursor skips every event before it, settings changes
            # among them: read the settings again too.
            self._owe_settings()
            self.event_id = str(self.client.call("GET", "/core/v4/events/latest")["EventID"])
        self.resync()

    def resync(self, *, reload_settings: bool = False) -> None:
        self.dirty = True
        if reload_settings:  # an explicit refresh also picks up Proton's settings
            self._owe_settings()
        self._settle_settings()
        now = int(self.clock())
        self._sync_unread_window(now)
        self._sync_recent()
        self._sync_totals()
        self.synced = True
        self.dirty = False

    def poll(self) -> bool:
        """Advance the event cursor. True when state was re-read."""
        if self.event_id is None or not self.synced:
            self.full_sync()
            return True
        relevant = self.dirty
        if self.settings_pending and self.clock() >= self.settings_retry_at:
            relevant = True  # settled inside the rebuild, which then follows them
            self.dirty = True
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
            if event.get("Refresh"):
                # The cursor has moved on: owe the rebuild and a fresh look at
                # the settings now, before anything that can fail.
                self.dirty = True
                self._owe_settings()
                relevant = True
            settings = event.get("MailSettings")
            if isinstance(settings, dict) and self._apply_settings(settings):
                relevant = True  # rows and links follow Proton's view mode
                self.dirty = True
            if self.event_is_relevant(event):
                relevant = True
                self.dirty = True
            if not event.get("More"):
                break
        if relevant:
            self.resync()
        return relevant

    def event_is_relevant(self, event: dict) -> bool:
        known = set(self.unread_times)
        known.update(row["id"] for row in self.recent)
        for item in event.get("Conversations") or []:
            if isinstance(item, dict) and str(item.get("ID") or "") in known:
                return True
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

    def mark_read(self, ids: list[str], kind: str = "message") -> None:
        ids = [i for i in dict.fromkeys(ids) if i]
        endpoint = "/mail/v4/conversations/read" if kind == "conversation" else "/mail/v4/messages/read"
        for start in range(0, len(ids), 100):
            self.client.call("PUT", endpoint, body={"IDs": ids[start : start + 100]})
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
        if not row or not self.settings_known:
            return f"{WEB_BASE}/inbox"
        if row.get("kind") == "conversation":
            # Proton opens /inbox/<id> as a conversation only in conversation
            # view; until the rows are rebuilt for message view, open the inbox.
            return f"{WEB_BASE}/inbox/{row['id']}" if self.grouped else f"{WEB_BASE}/inbox"
        if self.view_mode == VIEW_MODE_MESSAGES or not row.get("conversationId"):
            return f"{WEB_BASE}/inbox/{row['id']}"
        return f"{WEB_BASE}/inbox/{row['conversationId']}/{row['id']}"

    # ----------------------------------------------------------- internals

    def _owe_settings(self) -> None:
        self.settings_pending = True
        self.settings_retry_at = 0.0
        if self.settings_known:
            # The view mode may have changed meanwhile: until it is read
            # again, links stop relying on it.
            self.settings_known = False
            for row in self.recent:
                row["url"] = self.web_url(row)

    def _settle_settings(self) -> None:
        """Read Proton's mail settings while they are owed. Network, rate-limit
        and session errors propagate (the account's scheduling must see them)
        and leave the debt in place; an odd reply keeps the last known mode
        and is retried a little later."""
        if not self.settings_pending or self.clock() < self.settings_retry_at:
            return
        try:
            settings = self.client.call("GET", "/mail/v4/settings").get("MailSettings") or {}
        except (NetworkError, RateLimited, AuthExpired):
            raise
        except ProtonError:
            self.settings_retry_at = self.clock() + SETTINGS_RETRY
            return
        self.settings_pending = False
        self._apply_settings(settings)

    def _apply_settings(self, settings: dict) -> bool:
        """Take Proton's mail settings; True when the view mode changed."""
        try:
            mode = int(settings.get("ViewMode") or 0)
        except (TypeError, ValueError):
            return False
        changed = mode != self.view_mode
        first = not self.settings_known
        self.view_mode = mode
        self.settings_known = True
        if changed or first:
            # Until the rows are rebuilt for the new mode, give the ones shown
            # links that are right in it (web_url falls back to the inbox).
            for row in self.recent:
                row["url"] = self.web_url(row)
        return changed

    @property
    def grouped(self) -> bool:
        """Rows are conversations only while Proton itself groups by
        conversation: in single-message view its links take message IDs."""
        return self.threads and self.view_mode != VIEW_MODE_MESSAGES

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
        self.window_complete = len(times) >= total
        self.fresh = fresh

    def _sync_recent(self) -> None:
        if self.recent_limit <= 0:
            self.recent = []
            return
        # PageSize is exact; Limit is floored at 50 by the API.
        params = label_params(self.categories) + [
            ("Page", "0"),
            ("PageSize", str(self.recent_limit)),
            ("Sort", "Time"),
            ("Desc", "1"),
        ]
        if self.grouped:
            resp = self.client.call("GET", "/mail/v4/conversations", params=params)
            items, build = resp.get("Conversations"), summarize_conversation
        else:
            resp = self.client.call("GET", "/mail/v4/messages", params=params)
            items, build = resp.get("Messages"), summarize
        rows = [
            build(item, self.account) for item in items or [] if isinstance(item, dict) and item.get("ID")
        ]
        for row in rows:
            row["url"] = self.web_url(row)
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
