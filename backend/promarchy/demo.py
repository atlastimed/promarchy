"""Sample state for screenshots and UI work: no account, no network.

Enabled with the widget setting ``demo``: ``true`` for a healthy inbox, or a
variant name to see another state (``omarchy bar set atlastimed.promarchy
demo offline``): offline, expired, partial, warning, signed-out, empty, long,
multi. Everything is example data; account actions stay off.
"""

from __future__ import annotations

SAMPLES = [
    # (minutes ago, sender, address, subject, unread, thread, attachments, starred, category)
    (3, "Ada Lovelace", "ada@example.com", "Notes on the analytical engine", True, 1, 1, True, "primary"),
    (
        18,
        "GitHub",
        "notifications@github.example",
        "[omarchy/omarchy] New release: 4.1.0",
        True,
        3,
        0,
        False,
        "updates",
    ),
    (
        52,
        "Linear",
        "notifications@linear.example",
        "PRO-142 Ship the bar widget",
        True,
        1,
        0,
        False,
        "updates",
    ),
    (
        190,
        "Grace Hopper",
        "grace@example.com",
        "Re: Compiler review on Thursday?",
        True,
        2,
        0,
        False,
        "primary",
    ),
    (
        420,
        "Proton",
        "no-reply@proton.example",
        "Your monthly security summary",
        False,
        1,
        0,
        False,
        "primary",
    ),
    (
        1500,
        "Stripe",
        "receipts@stripe.example",
        "Your receipt from Hetzner #2741",
        False,
        1,
        1,
        False,
        "transactions",
    ),
    (2900, "Alan Turing", "alan@example.com", "Weekend hike photos", False, 1, 6, False, "primary"),
    (
        7000,
        "Weekly Digest",
        "digest@newsletter.example",
        "The weekly digest #712",
        False,
        1,
        0,
        False,
        "newsletters",
    ),
]

COUNTS = {"1h": 3, "24h": 4, "7d": 27, "all": 112}
ZERO = {"1h": 0, "24h": 0, "7d": 0, "all": 0}
VARIANTS = ("ok", "offline", "expired", "partial", "warning", "signed-out", "empty", "long", "multi")
YOU = "you@example.com"
WORK = "dana.okonkwo-lindqvist@research-division.example.org"

LONG = [
    (
        2,
        "Professor Dr. Maximilian Theodor von Habsburg-Lothringen",
        "max.habsburg@university-archive.example",
        "Re: Re: Fwd: Revised chapter draft — comments on sections 4.2 to 7.9 before the committee",
        True,
        14,
        3,
        True,
        "primary",
    ),
    (40, "Ōtomo Yūki", "yuki@example.jp", "会議の議事録 (meeting notes)", True, 1, 0, False, "primary"),
]


def variant(value: object) -> str:
    """The demo state a setting asks for (any other true value: a healthy inbox)."""
    name = str(value).strip().lower()
    return name if name in VARIANTS else "ok"


def demo_state(now: int, limit: int, kind: str = "ok") -> dict:
    """Sample mail relative to `now`, the moment demo mode was switched on, so
    the snapshot stays identical between ticks."""
    samples = LONG + SAMPLES if kind == "long" else SAMPLES
    extra = {"warning": ""}
    status, message = "ok", ""
    accounts = [{"email": YOU, "status": "ok", "error": "", "counts": COUNTS}]
    if kind in ("partial", "long", "multi"):
        second = "auth-expired" if kind == "partial" else "ok"
        error = "Your Proton session expired. Sign in again." if kind == "partial" else ""
        accounts.append({"email": WORK, "status": second, "error": error, "counts": ZERO})
    if kind == "offline":
        status, message = "offline", "Cannot reach Proton (offline?)"
        accounts[0].update(status="offline", error=message)
    elif kind == "expired":
        status = "auth-expired"
        accounts[0].update(status="auth-expired", error="Your Proton session expired. Sign in again.")
    elif kind == "partial":
        status, message = "partial", f"{WORK}: Your Proton session expired. Sign in again."
    elif kind == "warning":
        extra["warning"] = (
            f"{YOU}: renewed sign-in not yet saved to the keyring (is it locked?); retrying."
            " Until then, restarting Promarchy would sign you out."
        )
    elif kind == "signed-out":
        status, accounts = "signed-out", []
    messages = []
    for index, (minutes, name, address, subject, unread, thread, files, starred, category) in enumerate(
        samples
    ):
        mid = f"demo{index}"
        account = WORK if kind in ("long", "multi") and index % 3 == 1 else YOU
        messages.append(
            {
                "id": mid,
                "conversationId": f"demo-conv{index}",
                "account": account,
                "senderName": name,
                "senderAddress": address,
                "subject": subject,
                "time": now - minutes * 60,
                "unread": unread,
                "starred": starred,
                "attachments": files,
                "category": category,
                "senderImage": False,
                "threadCount": thread,
                "kind": "conversation",
                "unreadIds": [mid] if unread else [],
                "url": "https://mail.proton.me/inbox",
            }
        )
    empty = kind in ("empty", "signed-out")
    return {
        "type": "state",
        "demo": True,  # the widget keeps account actions away from real accounts
        "status": status,
        "message": message,
        **extra,
        "accounts": accounts,
        "counts": dict(ZERO if empty else COUNTS),
        "capped": False,
        "messages": [] if empty else messages[:limit],
        "inboxUrl": "https://mail.proton.me/inbox",
        "multiAccount": len(accounts) > 1,
    }
