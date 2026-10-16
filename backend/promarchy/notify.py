"""Desktop notifications for newly arrived mail.

Notifications go through ``omarchy-notification-send`` so they look and behave
like every other Omarchy toast (theme, Do Not Disturb, history), and a click
opens the message. Everything is passed as discrete argv words (never through
a shell), and because that script keeps parsing options until the first
unknown word, any sender name or subject starting with "-" has its first dash
swapped for a look-alike hyphen so it can never become an option.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import state_dir, write_private

MAX_INDIVIDUAL = 3
GLYPH = "󰇮"


GRACE = 600  # seconds of already-announced IDs remembered behind the watermark


@dataclass(frozen=True)
class Mark:
    """How far announcements have got for one account.

    `time` alone is not enough: two messages can share a timestamp second, and
    Proton can deliver one slightly out of order. So we also remember the IDs
    already handled in the last few minutes and only skip those.
    """

    time: int
    seen: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"time": self.time, "seen": self.seen}

    @classmethod
    def from_json(cls, value: Any) -> Mark | None:
        if isinstance(value, (int, float)):  # 0.1.0 format: a bare timestamp
            return cls(int(value), {})
        if isinstance(value, dict) and isinstance(value.get("time"), (int, float)):
            seen = value.get("seen") if isinstance(value.get("seen"), dict) else {}
            return cls(int(value["time"]), {str(k): int(v) for k, v in seen.items()})
        return None


class Watermarks:
    """Per-account announcement marks, kept across restarts."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or state_dir() / "notified.json"
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        self._marks: dict[str, Mark] = {}
        if isinstance(data, dict):
            for account, value in data.items():
                mark = Mark.from_json(value)
                if mark is not None:
                    self._marks[str(account)] = mark

    def get(self, account: str) -> Mark | None:
        return self._marks.get(account)

    def set(self, account: str, mark: Mark) -> None:
        if self._marks.get(account) != mark:
            self._marks[account] = mark
            self._write()

    def forget(self, account: str) -> None:
        if self._marks.pop(account, None) is not None:
            self._write()

    def _write(self) -> None:
        write_private(self.path, json.dumps({k: v.to_json() for k, v in self._marks.items()}))


def select_new(
    unread: dict[str, int], fresh: list[dict], mark: Mark | None, now: int, catch_up: int = 3600
) -> tuple[list[dict], int, Mark]:
    """Decide what to announce.

    `unread` is every unread message ID in the index (with its time), so the
    count stays right even when more mail arrived than we hold details for;
    `fresh` carries sender and subject for the newest of them.

    Returns (displayable new messages newest first, how many are new, new mark).
    The first run for an account announces nothing (old mail is not news),
    and after a long sleep only the last hour is caught up on.
    """
    if mark is None:
        start = max([now, *unread.values()])
        return [], 0, Mark(start, {i: t for i, t in unread.items() if t >= start - GRACE})
    floor = max(mark.time - GRACE, now - catch_up)
    new = {i: t for i, t in unread.items() if t >= floor and i not in mark.seen}
    advanced = max([mark.time, *new.values()])
    seen = {i: t for i, t in {**mark.seen, **new}.items() if t >= advanced - GRACE}
    shown = sorted((m for m in fresh if m["id"] in new), key=lambda m: m["time"], reverse=True)
    return shown, len(new), Mark(advanced, seen)


class Notifier:
    def __init__(self, icon: str = "", opener: list[str] | None = None) -> None:
        self.icon = icon
        self.opener = opener or ["omarchy-launch-browser"]
        self._send = shutil.which("omarchy-notification-send")
        self._fallback = shutil.which("notify-send")

    def announce(
        self, messages: list[dict], urls: dict[str, str], inbox_url: str, total: int | None = None
    ) -> None:
        total = len(messages) if total is None else total
        if total <= 0:
            return
        if total > MAX_INDIVIDUAL or len(messages) < total:
            senders = []
            for m in messages:
                if m["senderName"] not in senders:
                    senders.append(m["senderName"])
            shown = ", ".join(senders[:3])
            more = " and others" if len(senders) > 3 or len(messages) < total else ""
            self._toast(f"{total} new messages", (shown + more) or "Proton Mail", inbox_url)
            return
        for m in reversed(messages):  # oldest first, so the newest ends on top
            self._toast(
                m["senderName"] or "New message", m["subject"] or "(no subject)", urls.get(m["id"], inbox_url)
            )

    def _toast(self, title: str, body: str, url: str) -> None:
        title, body = defang(title), defang(body)
        if self._send:
            argv = [self._send, "--app-name", "Proton Mail", "-u", "normal", "-g", GLYPH]
            if self.icon:
                argv += ["-i", self.icon]
            argv += [title, body, "--exec", *self.opener, url]
        elif self._fallback:
            argv = [self._fallback, "--app-name=Proton Mail", "--", title, body]
        else:
            return
        try:
            subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass


def defang(text: str) -> str:
    """Make sure untrusted text cannot be parsed as a command-line option."""
    return "\u2010" + text[1:] if text.startswith("-") else text
