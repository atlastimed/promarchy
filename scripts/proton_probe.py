#!/usr/bin/env python3
"""Inspect Proton's live API through Promarchy's own session, without exposing it.

For maintainers and reviewers. Prints response *shapes* (field names and
types), numeric counts, and Promarchy's parsed view next to the raw fields it
is derived from, so parsers can be checked against the real service.

What it never does:
  * print or store tokens, cookies, IDs, senders, subjects or addresses
    (IDs become short aliases like c1, m2; text becomes "<str:N>");
  * renew the session: refresh tokens are single-use, and renewing here would
    consume the running bar's. If the access token has expired, it stops and
    asks you to let the bar renew it (one poll), then run it again.

Usage (from the repository root):
  python3 -I scripts/proton_probe.py [--account EMAIL] [--roundtrip]

  --roundtrip  one reversible account action: take the newest unread inbox
               conversation that has exactly one inbox message, mark it read
               the way the dropdown does, wait for the running bar's snapshot
               to reflect it, mark it unread again, and confirm the restore.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from promarchy.api import AuthExpired, ProtonClient, ProtonError  # noqa: E402
from promarchy.avatars import raster_extension  # noqa: E402
from promarchy.mailbox import CATEGORY_LABELS, INBOX, label_params, summarize, summarize_conversation  # noqa: E402
from promarchy.paths import runtime_dir  # noqa: E402
from promarchy.store import SessionStore  # noqa: E402

TEXT_FIELDS = {"senderName", "senderAddress", "subject", "account", "url", "avatar"}


class Aliases:
    """Stable short names for opaque IDs, so output can refer to items."""

    def __init__(self) -> None:
        self._names: dict[str, str] = {}

    def __call__(self, value: str, prefix: str) -> str:
        if value not in self._names:
            self._names[value] = f"{prefix}{sum(1 for n in self._names.values() if n.startswith(prefix)) + 1}"
        return self._names[value]


def shape(value, depth: int = 0):
    """Field names and types only; strings are never echoed."""
    if isinstance(value, dict):
        if depth > 3:
            return "{…}"
        return {key: shape(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [shape(value[0], depth + 1), f"len={len(value)}"] if value else []
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "str"
    return type(value).__name__


def redact_row(row: dict, alias: Aliases, now: int) -> dict:
    out = {}
    for key, value in row.items():
        if key in TEXT_FIELDS:
            out[key] = f"<str:{len(str(value))}>" if value else ""
        elif key in ("id", "conversationId"):
            out[key] = alias(
                str(value), "c" if row.get("kind") == "conversation" or key == "conversationId" else "m"
            )
        elif key == "unreadIds":
            out[key] = [alias(str(v), "c" if row.get("kind") == "conversation" else "m") for v in value]
        elif key == "time":
            out["ageSeconds"] = now - int(value or 0)
        else:
            out[key] = value
    return out


class NoRenewClient(ProtonClient):
    """A client that can never renew the session, whatever path asks it to."""

    def refresh(self) -> None:
        raise AuthExpired("the probe never renews the session")


class Probe:
    def __init__(self, client: ProtonClient) -> None:
        self.client = client
        self.requests = 0

    def get(self, path: str, params=None) -> dict:
        self.requests += 1
        return self.client.call("GET", path, params=params, _retry_auth=False)

    def put(self, path: str, body: dict) -> dict:
        self.requests += 1
        return self.client.call("PUT", path, body=body, _retry_auth=False)


def inbox_context(conversation: dict) -> dict:
    for entry in conversation.get("Labels") or []:
        if isinstance(entry, dict) and str(entry.get("ID")) == INBOX:
            return entry
    return {}


def check_conversations(probe: Probe, alias: Aliases, now: int, params, title: str) -> dict:
    resp = probe.get("/mail/v4/conversations", params=params)
    conversations = [c for c in resp.get("Conversations") or [] if isinstance(c, dict)]
    rows, mismatches = [], 0
    for conv in conversations:
        parsed = summarize_conversation(conv, "account")
        ctx = inbox_context(conv)
        raw = {
            "inbox.ContextNumMessages": ctx.get("ContextNumMessages"),
            "inbox.ContextNumUnread": ctx.get("ContextNumUnread"),
            "inbox.ContextTime.age": now - int(ctx.get("ContextTime") or 0) if ctx else None,
            "top.ContextNumMessages": conv.get("ContextNumMessages"),
            "NumMessages": conv.get("NumMessages"),
            "CategoryID": "set" if conv.get("CategoryID") else "",
            "hasLabelIDs": isinstance(conv.get("LabelIDs"), list),
        }
        expected = (
            ctx.get("ContextNumMessages", conv.get("ContextNumMessages")),
            (ctx.get("ContextNumUnread", conv.get("ContextNumUnread")) or 0) > 0,
        )
        if ctx and (parsed["threadCount"], parsed["unread"]) != expected:
            mismatches += 1
        rows.append({"parsed": redact_row(parsed, alias, now), "raw": raw})
    return {
        "title": title,
        "total": resp.get("Total"),
        "returned": len(conversations),
        "parserMismatches": mismatches,
        "spanningFolders": sum(
            1 for r in rows if r["raw"]["NumMessages"] != r["raw"]["inbox.ContextNumMessages"]
        ),
        "shapeOfFirst": shape(conversations[0]) if conversations else None,
        "rows": rows[:5],
    }


def unread_since(probe: Probe, seconds: int, now: int) -> int:
    params = label_params(()) + [
        ("Unread", "1"),
        ("Begin", str(now - seconds)),
        ("Page", "0"),
        ("PageSize", "1"),
    ]
    return int(probe.get("/mail/v4/messages", params=params).get("Total") or 0)


def snapshot() -> dict:
    try:
        return json.loads((runtime_dir() / "state.json").read_text())
    except (OSError, ValueError):
        return {}


def report(probe: Probe, alias: Aliases) -> dict:
    now = int(time.time())
    out: dict = {}
    latest = probe.get("/core/v4/events/latest")
    event = probe.get(f"/core/v4/events/{latest['EventID']}")
    out["events"] = {"latest": shape(latest), "next": shape(event)}

    messages = probe.get("/mail/v4/messages", params=label_params(()) + [("Page", "0"), ("PageSize", "5")])
    first = (messages.get("Messages") or [{}])[0]
    out["messages"] = {
        "shapeOfFirst": shape(first),
        "parsed": [redact_row(summarize(m, "account"), alias, now) for m in messages.get("Messages") or []],
    }

    page = label_params(()) + [("Page", "0"), ("PageSize", "25"), ("Sort", "Time"), ("Desc", "1")]
    out["conversations.inbox"] = check_conversations(probe, alias, now, page, "Inbox")
    primary = label_params(("primary",)) + [
        ("Page", "0"),
        ("PageSize", "10"),
        ("Sort", "Time"),
        ("Desc", "1"),
    ]
    out["conversations.primary"] = check_conversations(probe, alias, now, primary, "Inbox, Primary category")

    counts = probe.get("/mail/v4/messages/count", params={"OnlyInInboxForCategories": 1})
    watched = {INBOX: "inbox", **{label: name for name, label in CATEGORY_LABELS.items()}}
    out["counts"] = {
        watched[str(c.get("LabelID"))]: {"unread": c.get("Unread"), "total": c.get("Total")}
        for c in counts.get("Counts") or []
        if str(c.get("LabelID")) in watched
    }
    settings = probe.get("/mail/v4/settings").get("MailSettings") or {}
    out["viewMode"] = settings.get("ViewMode")

    logo_sender = next(
        (m.get("SenderAddress") for m in messages.get("Messages") or [] if m.get("DisplaySenderImage")), None
    )
    if logo_sender:
        probe.requests += 1
        status, ctype, body = probe.client.fetch_bytes(
            "/core/v4/images/logo",
            params={"Address": logo_sender, "Size": 64, "Mode": "light", "Format": "png"},
            max_bytes=512 * 1024,
        )
        out["logo"] = {
            "status": status,
            "contentType": ctype,
            "bytes": len(body),
            "raster": raster_extension(body),
        }

    independent = {
        name: unread_since(probe, span, now) for name, span in (("1h", 3600), ("24h", 86400), ("7d", 604800))
    }
    state = snapshot()
    out["barVersusApi"] = {
        "bar": {k: (state.get("counts") or {}).get(k) for k in ("1h", "24h", "7d", "all")},
        "api": {**independent, "all": (out["counts"].get("inbox") or {}).get("unread")},
        "barStatus": state.get("status"),
        "note": "small gaps can be mail arriving between reads; the bar applies its categories setting",
    }
    return out


def wait_for(predicate, timeout: float = 75.0) -> float | None:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if predicate():
            return round(time.monotonic() - start, 1)
        time.sleep(1.5)
    return None


def is_single_unread(conversation: dict) -> bool:
    """Exactly one message in the whole conversation, unread, in the Inbox.

    Marking a conversation read touches all its messages, while marking it
    unread again only restores one (Proton's own behaviour), so anything else
    could not be restored exactly.
    """
    ctx = inbox_context(conversation)
    return (
        conversation.get("NumMessages") == 1
        and conversation.get("NumUnread") == 1
        and ctx.get("ContextNumMessages") == 1
        and ctx.get("ContextNumUnread") == 1
    )


def restore(probe: Probe, cid: str, result: dict) -> None:
    probe.put("/mail/v4/conversations/unread", {"IDs": [cid], "LabelID": INBOX})


def verify(probe: Probe, cid: str, result: dict) -> None:
    after = probe.get(f"/mail/v4/conversations/{cid}").get("Conversation") or {}
    result["unreadAfterRestore"] = after.get("NumUnread")
    result["restored"] = is_single_unread(after)


def roundtrip(probe: Probe, alias: Aliases) -> dict:
    page = label_params(()) + [("Page", "0"), ("PageSize", "50"), ("Sort", "Time"), ("Desc", "1")]
    conversations = probe.get("/mail/v4/conversations", params=page).get("Conversations") or []
    target = next((c for c in conversations if is_single_unread(c)), None)
    if target is None:
        return {"skipped": "no unread single-message conversation in the newest 50"}
    cid = str(target["ID"])
    # Re-check just before writing: the list may be seconds old.
    if not is_single_unread(probe.get(f"/mail/v4/conversations/{cid}").get("Conversation") or {}):
        return {"skipped": "the chosen conversation changed before the test; nothing was modified"}
    before = (snapshot().get("counts") or {}).get("all")
    result: dict = {"conversation": alias(cid, "c"), "barAllBefore": before, "restored": False}
    # Once the read request is sent it may have landed even if its reply is
    # lost, so from here on the restore always runs and is always reported,
    # whatever happens meanwhile (Ctrl+C included).
    try:
        probe.put("/mail/v4/conversations/read", {"IDs": [cid]})
        result["barReflectedReadAfterSeconds"] = wait_for(
            lambda: (snapshot().get("counts") or {}).get("all") == (before or 0) - 1
        )
    except ProtonError as exc:
        result["readError"] = f"{type(exc).__name__} (HTTP {exc.status})"
    except (Exception, KeyboardInterrupt) as exc:
        result["interrupted"] = type(exc).__name__
    for step, action in (("restoreError", restore), ("verifyError", verify)):
        try:
            action(probe, cid, result)
        except ProtonError as exc:
            result[step] = f"{type(exc).__name__} (HTTP {exc.status})"
        except (Exception, KeyboardInterrupt) as exc:
            result[step] = type(exc).__name__
    if not result["restored"]:
        result["action"] = (
            "Restore not confirmed: in Proton Mail, find your newest unread conversation from before this run"
            f" ({result['conversation']} here) and mark it unread again if it is now read."
        )
    elif "interrupted" not in result:
        try:
            result["barRestoredAfterSeconds"] = wait_for(
                lambda: (snapshot().get("counts") or {}).get("all") == before
            )
        except KeyboardInterrupt:
            result["interrupted"] = "KeyboardInterrupt"  # already restored and verified
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--account", help="signed-in address (default: the first)")
    parser.add_argument("--roundtrip", action="store_true", help="one reversible mark-read/unread check")
    args = parser.parse_args(argv)

    store = SessionStore()
    accounts = store.accounts()
    email = (args.account or (accounts[0] if accounts else "")).lower()
    session = store.load(email) if email in accounts else None
    if session is None:
        print("No signed-in Promarchy account found.", file=sys.stderr)
        return 1
    probe = Probe(NoRenewClient(session))
    alias = Aliases()
    status = 0
    try:
        out = report(probe, alias)
        if args.roundtrip:
            out["roundtrip"] = roundtrip(probe, alias)
            if "skipped" not in out["roundtrip"] and not out["roundtrip"]["restored"]:
                status = 3
            elif "interrupted" in out["roundtrip"]:
                status = 130
    except AuthExpired:
        print(
            "The access token has expired. This probe never renews the session (that would consume\n"
            "the running bar's refresh token). Wait for the bar's next poll, then run it again.",
            file=sys.stderr,
        )
        return 2
    except ProtonError as exc:
        print(
            f"Proton request failed: {type(exc).__name__} (code {exc.code}, HTTP {exc.status})",
            file=sys.stderr,
        )
        return 1
    out["requests"] = probe.requests
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    print()
    if status == 3:
        print("Round trip: the restore could not be confirmed; see roundtrip.action.", file=sys.stderr)
    elif status == 130:
        print("Round trip interrupted; the conversation was restored and verified.", file=sys.stderr)
    return status


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
