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
a toast is given up only for a row that names the message's own sender and
subject. Anything else (no such file, a format this does not know, a row
Omarchy redacted) means "not announced", so the worst case is the double toast
this exists to prevent.
"""

from __future__ import annotations

import html
import json
import re
import stat
from pathlib import Path

from .mailbox import SUBJECT_LIMIT, clean_text

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

# What Omarchy stores instead of a notification that carried a sign-in code:
# its sender and text are gone, so nothing says which message it was for.
REDACTED = ("Verification notification", "[redacted]")
_CODE_WORD = re.compile(
    r"\b(code|otp|one[\s-]?time|verification|verify|passcode|pin|2fa|two[\s-]factor|security key"
    r"|confirm(?:ation)?)\b",
    re.IGNORECASE,
)
_CODE_SHAPE = re.compile(
    r"(?<![\w-])(?:[A-Za-z]-)?(?:\d{2,4}[\s-]\d{2,5}|\d{4,8}|(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{5,8})"
    r"(?![\w-])"
)


def fold(text: object) -> str:
    """Text as both sides can be compared: a browser escapes markup, and
    Promarchy's own copy has lost its invisible characters, case and spacing
    aside."""
    return " ".join(clean_text(html.unescape(str(text or "")), 20000).casefold().split())


def carries_code(message: dict) -> bool:
    """Whether this looks like a sign-in code, whose toast Omarchy stores
    redacted. Deliberately broad: such mail is never made to wait."""
    text = f"{message.get('senderName') or ''} - {message.get('subject') or ''}"
    return bool(_CODE_WORD.search(text) and _CODE_SHAPE.search(text))


def announced(message: dict, rows: list[dict]) -> bool:
    """True when one of `rows` is a browser's toast for `message`.

    Proton's web app writes "From: <sender> - <subject>" in the reader's own
    language (German uses a dash of its own), so the match is the sender,
    a short separator and the subject, never the wording around them.
    """
    sender = fold(message.get("senderName"))
    if not sender:
        return False
    raw = str(message.get("subject") or "")
    subject = fold(raw)
    # "<sender> - <subject>": the sender from the start of a word (some
    # languages put no space after their "From:"), a dash of one or two
    # characters with spaces around it, then the subject, which ends the
    # toast: one for "Lunch? Moved to Saturday" is not one for "Lunch?". Only
    # a subject Promarchy holds just the start of is matched as a start.
    tail = r"\s" + re.escape(subject) if subject else ""
    if len(raw) < SUBJECT_LIMIT:
        tail += r"$"
    pattern = re.compile(r"(?<!\w)" + re.escape(sender) + r"\s\S{1,2}" + tail)
    after = int(message.get("time") or 0) - SKEW
    return any(not row["redacted"] and row["ts"] >= after and pattern.search(row["text"]) for row in rows)


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
        rows = self.rows()
        left = [m for m in messages if not announced(m, rows)]
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

    def obscured(self, messages: list[dict]) -> bool:
        """Whether a redacted toast, not one of ours, appeared since these
        messages arrived. It may be the browser's for one of them, and nothing
        can tell: waiting longer would only make a sign-in code late."""
        since = min((int(m.get("time") or 0) for m in messages), default=0) - OWN_MARGIN
        return any(
            row["redacted"]
            and row["ts"] >= since
            and not any(abs(row["ts"] - t) <= OWN_MARGIN for t in self._own)
            for row in self.rows()
        )

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
            info = path.stat()
            # Only a plain file of a sane size: anything else is not a record
            # (and opening a FIFO would wait for ever).
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
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

    def obscured(self, messages: list[dict]) -> bool:
        return False

    def missed(self, account: str) -> None:
        pass

    def sent(self, account: str, messages: list[dict], now: float) -> None:
        pass

    def forget(self, account: str) -> None:
        pass
