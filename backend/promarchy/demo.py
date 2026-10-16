"""Sample state for screenshots and UI work: no account, no network.

Enabled with the undocumented widget setting ``demo: true``
(``omarchy bar set atlastimed.promarchy demo true``).
"""

from __future__ import annotations

SAMPLES = [
    # (minutes ago, sender, address, subject, unread, thread, attachments, starred, category)
    (3, "Ada Lovelace", "ada@example.com", "Notes on the analytical engine", True, 1, 1, True, "primary"),
    (
        18,
        "GitHub",
        "notifications@github.com",
        "[omarchy/omarchy] New release: 4.1.0",
        True,
        3,
        0,
        False,
        "updates",
    ),
    (52, "Linear", "notifications@linear.app", "PRO-142 Ship the bar widget", True, 1, 0, False, "updates"),
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
    (420, "Proton", "no-reply@proton.me", "Your monthly security summary", False, 1, 0, False, "primary"),
    (
        1500,
        "Stripe",
        "receipts@stripe.com",
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
        "Hacker Newsletter",
        "kale@hackernewsletter.com",
        "Hacker Newsletter #712",
        False,
        1,
        0,
        False,
        "newsletters",
    ),
]

COUNTS = {"1h": 3, "24h": 4, "7d": 27, "all": 112}


def demo_state(now: int, limit: int) -> dict:
    messages = []
    for index, (minutes, name, address, subject, unread, thread, files, starred, category) in enumerate(
        SAMPLES
    ):
        mid = f"demo{index}"
        messages.append(
            {
                "id": mid,
                "conversationId": f"demo-conv{index}",
                "account": "you@proton.me",
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
                "unreadIds": [mid] if unread else [],
                "url": "https://mail.proton.me/inbox",
            }
        )
    return {
        "type": "state",
        "status": "ok",
        "message": "",
        "accounts": [{"email": "you@proton.me", "status": "ok", "error": "", "counts": COUNTS}],
        "counts": dict(COUNTS),
        "capped": False,
        "messages": messages[:limit],
        "inboxUrl": "https://mail.proton.me/inbox",
        "multiAccount": False,
    }
