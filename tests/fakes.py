"""In-memory stand-ins for Proton and the keyring, shaped like the real API."""

from __future__ import annotations

import itertools

from promarchy.api import ProtonError

NOW = 1_800_000_000


class FakeProton:
    """Implements the handful of endpoints Promarchy calls, with the
    filtering semantics verified against the live API (inbox AND any-of
    categories, Begin/Unread filters, Anchor/AnchorID paging)."""

    def __init__(self, now: int = NOW, clock=None) -> None:
        self._now = now
        self._clock = clock
        self.messages: list[dict] = []
        self.events: list[dict] = []
        self.calls: list[tuple[str, str, object, object]] = []
        self.view_mode = 0
        self.session = None
        self.fail_with: ProtonError | None = None
        self._ids = itertools.count(1)
        self._event = itertools.count(1)
        self.event_id = "ev0"

    @property
    def now(self) -> int:
        return int(self._clock()) if self._clock else self._now

    # -------------------------------------------------------------- fixtures

    def add(
        self,
        age: int,
        *,
        unread: bool = True,
        labels: tuple[str, ...] = ("0", "5", "24"),
        conversation: str | None = None,
        subject: str = "Hello",
        sender: str = "Alice",
        address: str = "alice@example.com",
        attachments: int = 0,
        logo: bool = False,
    ) -> dict:
        number = next(self._ids)
        message = {
            "ID": f"m{number}",
            "ConversationID": conversation or f"c{number}",
            "Subject": subject,
            "Sender": {"Name": sender, "Address": address},
            "SenderName": sender,
            "SenderAddress": address,
            "Time": self.now - age,
            "Unread": 1 if unread else 0,
            "LabelIDs": list(labels),
            "NumAttachments": attachments,
            "DisplaySenderImage": 1 if logo else 0,
        }
        self.messages.append(message)
        return message

    def queue_event(self, **event) -> None:
        self.events.append(event)

    def endpoint_calls(self, path: str) -> int:
        return sum(1 for call in self.calls if call[1] == path)

    # ------------------------------------------------------------------- API

    def call(self, method, path, *, params=None, body=None, auth=True, headers=None, _retry_auth=True):
        self.calls.append((method, path, params, body))
        if self.fail_with is not None:
            raise self.fail_with
        if path == "/core/v4/events/latest":
            return {"Code": 1000, "EventID": self.event_id}
        if path.startswith("/core/v4/events/"):
            event = self.events.pop(0) if self.events else {}
            if "raise" in event:
                raise event["raise"]
            self.event_id = f"ev{next(self._event)}"
            return {"Code": 1000, "EventID": self.event_id, "More": 0, "Refresh": 0, **event}
        if path == "/mail/v4/settings":
            return {"Code": 1000, "MailSettings": {"ViewMode": self.view_mode}}
        if path == "/mail/v4/messages" and method == "GET":
            return self._list(params)
        if path == "/mail/v4/messages/count":
            return self._counts()
        if path == "/mail/v4/messages/read" and method == "PUT":
            for message in self.messages:
                if message["ID"] in body["IDs"]:
                    message["Unread"] = 0
            return {"Code": 1000}
        raise AssertionError(f"unexpected call {method} {path}")

    def fetch_bytes(self, path, *, params=None):
        self.calls.append(("GET", path, params, None))
        return 200, "image/png", b"\x89PNG fake"

    def close(self) -> None:
        pass

    # ------------------------------------------------------------- internals

    def _list(self, params) -> dict:
        pairs = list(params.items()) if isinstance(params, dict) else list(params)
        single = dict(pairs)
        labels = [str(v) for k, v in pairs if k in ("LabelID", "LabelID[]")]
        location, categories = labels[0], set(labels[1:])
        unread_only = single.get("Unread") == "1"
        begin = int(single.get("Begin", 0))
        matches = [
            m
            for m in self.messages
            if location in m["LabelIDs"]
            and (not categories or categories & set(m["LabelIDs"]))
            and (not unread_only or m["Unread"])
            and m["Time"] >= begin
        ]
        matches.sort(key=lambda m: (m["Time"], m["ID"]), reverse=True)
        total = len(matches)
        if "AnchorID" in single:
            ids = [m["ID"] for m in matches]
            matches = matches[ids.index(single["AnchorID"]) + 1 :]
        limit = int(single.get("PageSize") or max(50, int(single.get("Limit", 50))))
        return {"Code": 1000, "Total": total, "Messages": [dict(m) for m in matches[:limit]]}

    def _counts(self) -> dict:
        counts: dict[str, int] = {}
        for message in self.messages:
            if not message["Unread"] or "0" not in message["LabelIDs"]:
                continue
            for label in message["LabelIDs"]:
                counts[label] = counts.get(label, 0) + 1
        return {"Code": 1000, "Counts": [{"LabelID": k, "Unread": v, "Total": v} for k, v in counts.items()]}


class MemorySecrets:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value

    def delete(self, key: str) -> None:
        self.data.pop(key, None)


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
