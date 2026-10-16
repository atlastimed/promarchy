"""What the session owner does, whichever process it is (see control.py).

Results are plain dicts so the running bar can send them over its socket:
``{"email", "outcome", "removed"}`` plus ``"removeFailed": True`` when the
keyring refused to forget the account, and ``"unsaved": True`` when fresh
tokens could not be saved.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable

from .api import ProtonClient, RateLimited, Session
from .auth import REJECTED, REVOKED, UNCONFIRMED, end_session
from .log import log_error
from .store import SessionStore, StoreUnavailable

NOT_SIGNED_IN = "not-signed-in"
UNREADABLE = "unreadable"
NO_SESSION = "no-session"


def forget(store: SessionStore, result: dict, **conditions: object) -> dict:
    """Remove the account from this machine, recording a keyring refusal
    instead of hiding it behind "a newer sign-in replaced it"."""
    try:
        result["removed"] = store.remove(result["email"], **conditions)
    except Exception as exc:
        log_error("removing an account", exc)
        result["removeFailed"] = True
    return result


def sign_out_stored(
    store: SessionStore,
    email: str,
    local: bool,
    client_factory: Callable[..., ProtonClient] = ProtonClient,
) -> dict:
    """End a session from its saved copy. Only the owner may call this."""
    result: dict = {"email": email, "outcome": UNREADABLE, "removed": False}
    try:
        if email not in store.accounts():
            return {**result, "outcome": NOT_SIGNED_IN}
        session = store.load(email)
    except StoreUnavailable as exc:
        log_error("reading a session to sign out", exc)
        return result
    if session is None:
        # An empty answer can also mean a locked keyring: only forget the
        # account when asked to, and only if no session appeared meanwhile.
        if not local:
            return result
        return forget(store, {**result, "outcome": NO_SESSION}, require_absent=True)

    unsaved: list[Session] = []

    def persist(updated: Session) -> None:
        # Keep going if the keyring refuses: finish ending the session first.
        try:
            store.update_tokens(updated)
        except Exception:
            unsaved.append(updated)

    try:
        outcome = end_session(client_factory(session, on_refresh=persist))
    except RateLimited as exc:
        outcome = UNCONFIRMED
        result["retryAfter"] = exc.retry_after
    if unsaved:  # one more attempt before reporting
        with contextlib.suppress(Exception):
            store.update_tokens(unsaved[-1])
            unsaved.clear()
    result.update(outcome=outcome, unsaved=bool(unsaved))
    if outcome in (REVOKED, REJECTED) or local:
        forget(store, result, expected_uid=session.uid)
    return result


def adopt(store: SessionStore, session: Session, end: Callable[[Session], str]) -> dict:
    """Save a fresh sign-in and end every session it replaces.

    `end` ends one displaced session with the owner's current tokens for it
    (the saved copy may be older than what the owner holds). Raises if the
    new session cannot be saved; nothing is replaced then.
    """
    displaced = store.replace(session)
    outcomes = []
    for old in displaced:
        try:
            outcome = end(old)
        except Exception as exc:
            log_error("ending a replaced session", exc)
            outcome = UNCONFIRMED
        outcomes.append({"email": old.email, "outcome": outcome})
    return {"saved": True, "displaced": outcomes, "cleanupFailures": list(store.cleanup_failures)}


def end_displaced(client: ProtonClient) -> str:
    try:
        return end_session(client)
    except RateLimited:
        return UNCONFIRMED
