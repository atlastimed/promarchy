"""Mail that Proton's own web app has already announced.

With Proton Mail open in a browser that may show notifications, the web app
announces new mail itself, and so would Promarchy: two toasts for one message.
Nothing tells a plugin which tabs are open, but Omarchy's notification service
records what it showed and where each toast came from, so this reads that
record (read-only) and answers two questions:

* was this message announced by a browser already? Then Promarchy stays quiet.
* is a browser announcing this account's mail at all? Then a new toast waits a
  little for the browser's, and is shown only if none comes.

Only rows from Proton's web app are looked at, nothing is kept or logged, and
any failure (no such file, a format this does not know) means "no", so the
worst case is the double toast this exists to prevent, never a lost one.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

HOSTS = ("mail.proton.me",)
# Proton's web app polls about as often as Promarchy does, and a background tab
# may run its timers only once a minute: wait that long for its toast.
HOLD_SECONDS = 90
# A browser's toast for a message cannot be much older than the message; the
# margin covers a clock that differs from Proton's.
SKEW = 120
# A redacted row this close to one of Promarchy's own toasts is that toast.
OWN_MARGIN = 5
MAX_BYTES = 4 * 1024 * 1024
SHOWN_KEEP = 8  # of our own latest toasts per account, to spot a browser repeating them
FILES = ("live.json", "history.json")

# What Omarchy stores instead of a notification that carried a sign-in code
# (its sender and text are gone, so it can only be matched by its time).
REDACTED = ("Verification notification", "[redacted]")
_CODE_WORD = re.compile(
    r"\b(code|otp|one[\s-]?time|verification|verify|passcode|pin|2fa|two[\s-]factor|security key"
    r"|confirm(?:ation)?)\b",
    re.IGNORECASE,
)
_CODE_SHAPE = re.compile(
    r"(?<![\w-])(?:\d{2,4}[\s-]\d{2,5}|\d{4,8}|(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{5,8})(?![\w-])"
)


def fold(text: object) -> str:
    """Text as a browser may have re-spelled it: entities, case, spacing."""
    return " ".join(html.unescape(str(text or "")).casefold().split())


def carries_code(message: dict) -> bool:
    """Whether Omarchy would store this message's toast as a redacted one."""
    text = f"{message.get('senderName') or ''} - {message.get('subject') or ''}"
    return bool(_CODE_WORD.search(text) and _CODE_SHAPE.search(text))


def announced(message: dict, rows: list[dict], own: tuple[float, ...] = ()) -> bool:
    """True when one of `rows` is a browser's toast for `message`.

    Proton's web app writes "From: <sender> - <subject>" in the reader's own
    language, so the match is on the subject (or, without one, the sender),
    never on the wording around it. `own` holds the times of Promarchy's own
    toasts, whose redacted rows look like anyone else's.
    """
    after = int(message.get("time") or 0)
    needle = fold(message.get("subject"))[:80] or fold(message.get("senderName"))[:80]
    coded = carries_code(message)
    for row in rows:
        if row["redacted"]:
            if coded and row["ts"] >= after - 5 and not any(abs(row["ts"] - t) <= OWN_MARGIN for t in own):
                return True
        elif needle and row["ts"] >= after - SKEW and needle in row["text"]:
            return True
    return False


class BrowserEchoes:
    """What Omarchy's notification service recorded for Proton's web app, and
    which accounts a browser is announcing mail for.

    A browser counts as there for an account once it announced one of that
    account's messages, until a toast that waited for it went unanswered.
    Per account, because a browser signed in to one account says nothing
    about the mail of another.
    """

    def __init__(self, root: Path | None = None) -> None:
        # Omarchy keeps this under $HOME itself, not under $XDG_STATE_HOME.
        self.root = root or Path.home() / ".local/state/omarchy/notifications"
        self._stamp: tuple | None = None
        self._rows: list[dict] = []
        self._present: dict[str, bool] = {}
        # Promarchy's own latest toasts: when each went out, and per account
        # the messages, to notice a browser announcing the same ones later.
        self._own: list[float] = []
        self._shown: dict[str, list[dict]] = {}

    def rows(self) -> list[dict]:
        """Proton's web toasts and redacted placeholders, parsed once per change."""
        stamp = tuple(self._stat(name) for name in FILES)
        if stamp != self._stamp:
            self._stamp = stamp
            self._rows = [row for name in FILES for row in self._read(name)]
        return self._rows

    def unannounced(self, account: str, messages: list[dict]) -> list[dict]:
        """The messages no browser has announced yet."""
        rows, own = self.rows(), tuple(self._own)
        left = [m for m in messages if not announced(m, rows, own)]
        if len(left) < len(messages):
            self._present[account] = True
        return left

    def present(self, account: str) -> bool:
        """Whether a browser is announcing this account's mail."""
        shown = self._shown.get(account)
        if shown and len(self.unannounced(account, shown)) < len(shown):
            del self._shown[account]  # it announced what we did: it is there
        known = self._present.get(account)
        if known is None:
            # Nothing learned yet (we just started): a browser that announced
            # any Proton mail lately is probably still there.
            return any(not row["redacted"] for row in self.rows())
        return known

    def missed(self, account: str) -> None:
        """A toast waited for the browser and it never came: the tab is gone."""
        self._present[account] = False

    def sent(self, account: str, messages: list[dict], now: float) -> None:
        """Promarchy showed these itself at `now`."""
        self._own = [*self._own[-31:], now]
        self._shown[account] = [*self._shown.get(account, []), *messages][-SHOWN_KEEP:]

    def forget(self, account: str) -> None:
        self._present.pop(account, None)
        self._shown.pop(account, None)

    def _stat(self, name: str) -> tuple[int, int] | None:
        try:
            info = (self.root / name).stat()
        except OSError:
            return None
        return info.st_mtime_ns, info.st_size

    def _read(self, name: str) -> list[dict]:
        path = self.root / name
        try:
            if path.stat().st_size > MAX_BYTES:
                return []
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return []
        found = []
        for entry in data if isinstance(data, list) else []:
            if not isinstance(entry, dict):
                continue
            ts = entry.get("ts")
            if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                continue
            summary, body = str(entry.get("summary") or ""), str(entry.get("body") or "")
            if (summary, body) == REDACTED:
                found.append({"ts": float(ts), "redacted": True, "text": ""})
            elif str(entry.get("source") or "").strip().lower() in HOSTS:
                found.append({"ts": float(ts), "redacted": False, "text": fold(f"{summary} {body}")})
        return found


class NoEchoes:
    """Stands in where there is no record to read: nothing was ever announced."""

    def unannounced(self, account: str, messages: list[dict]) -> list[dict]:
        return messages

    def present(self, account: str) -> bool:
        return False

    def missed(self, account: str) -> None:
        pass

    def sent(self, account: str, messages: list[dict], now: float) -> None:
        pass

    def forget(self, account: str) -> None:
        pass
