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
import os
import shutil
import subprocess
from pathlib import Path

from .paths import state_dir

MAX_INDIVIDUAL = 3
GLYPH = "󰇮"


class Watermarks:
    """Per-account "newest mail already announced" times, kept across restarts."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or state_dir() / "notified.json"
        try:
            data = json.loads(self.path.read_text())
            self._marks = {str(k): int(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self._marks = {}

    def get(self, account: str) -> int | None:
        return self._marks.get(account)

    def set(self, account: str, value: int) -> None:
        if self._marks.get(account) != value:
            self._marks[account] = value
            self._write()

    def forget(self, account: str) -> None:
        if self._marks.pop(account, None) is not None:
            self._write()

    def _write(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(self._marks, fh)
        os.replace(tmp, self.path)


def select_new(
    fresh: list[dict], watermark: int | None, now: int, catch_up: int = 3600
) -> tuple[list[dict], int]:
    """Pick messages to announce and return the advanced watermark.

    The first run for an account announces nothing (old mail is not news).
    After a restart we only catch up on the last hour, so a laptop that was
    asleep overnight does not unload a backlog of toasts.
    """
    if watermark is None:
        newest = max((m["time"] for m in fresh), default=now)
        return [], max(newest, now)
    floor = max(watermark, now - catch_up)
    new = [m for m in fresh if m["time"] > floor and m["unread"]]
    new.sort(key=lambda m: m["time"], reverse=True)
    advanced = max([watermark] + [m["time"] for m in new])
    return new, advanced


class Notifier:
    def __init__(self, icon: str = "", opener: list[str] | None = None) -> None:
        self.icon = icon
        self.opener = opener or ["omarchy-launch-browser"]
        self._send = shutil.which("omarchy-notification-send")
        self._fallback = shutil.which("notify-send")

    def announce(self, messages: list[dict], urls: dict[str, str], inbox_url: str) -> None:
        if not messages:
            return
        if len(messages) > MAX_INDIVIDUAL:
            senders = []
            for m in messages:
                if m["senderName"] not in senders:
                    senders.append(m["senderName"])
            more = f" and {len(senders) - 3} more" if len(senders) > 3 else ""
            self._toast(
                f"{len(messages)} new messages",
                ", ".join(senders[:3]) + more,
                inbox_url,
            )
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
