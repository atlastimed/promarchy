"""Command line entry points: ``promarchyctl <command>``."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import getpass
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

from . import __version__
from .api import HumanVerificationRequired, ProtonClient, ProtonError, RateLimited, Session, WrongCredentials
from .auth import (
    REJECTED,
    REVOKED,
    UNCONFIRMED,
    HumanVerification,
    MissingDependency,
    begin_login,
    end_session,
    logout,
    primary_address,
    request_verification_code,
    submit_totp,
)
from .paths import open_private, runtime_dir, state_dir, write_private
from .store import SessionStore, StoreUnavailable

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
VERIFY_PREFIX = "https://verify.proton.me/"


def _style(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if sys.stdout.isatty() else text


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


# --------------------------------------------------------------------- login


class LoginFailed(Exception):
    pass


def cmd_login(args: argparse.Namespace) -> int:
    print(_style("Promarchy · sign in to Proton Mail", BOLD))
    print(
        _style(
            "Your password is only used on this machine for Proton's SRP handshake and is not stored.\n", DIM
        )
    )
    try:
        email = _login(args.username)
    except LoginFailed as exc:
        print(f"\n✗ {exc}", file=sys.stderr)
        record_login(False, str(exc))
        code = 1
    except KeyboardInterrupt:
        print()
        record_login(False, "Sign-in cancelled")
        return 130
    else:
        print(f"\n✓ Signed in as {_style(email, BOLD)}. The bar picks this up within a few seconds.")
        record_login(True, email)
        code = 0
    if args.wait:
        _ask("\nPress Enter to close… ")
    return code


def _login(username: str | None) -> str:
    store = SessionStore()
    try:
        _ = store.secrets  # fail early if the keyring is unavailable
    except StoreUnavailable as exc:
        raise LoginFailed(str(exc)) from exc

    username = username or _ask("Proton username or address: ")
    if not username:
        raise LoginFailed("No username given.")
    password = getpass.getpass("Password: ")

    client = ProtonClient()
    hv: HumanVerification | None = None
    try:
        for _ in range(3):
            try:
                pending = begin_login(client, username, password, hv)
                break
            except HumanVerificationRequired as exc:
                hv = _human_verification(client, exc)
                if hv is None:
                    raise LoginFailed("Proton's human verification was not completed.") from exc
        else:
            raise LoginFailed("Proton did not accept the verification. Try again in a minute.")
    except WrongCredentials as exc:
        raise LoginFailed("Incorrect username or password.") from exc
    except MissingDependency as exc:
        raise LoginFailed(str(exc)) from exc
    except ProtonError as exc:
        raise LoginFailed(f"Sign-in failed: {exc}") from exc
    finally:
        password = ""  # noqa: F841 - drop our reference as early as possible

    client.session = pending.session
    committed = False
    try:
        if pending.needs_fido2_only:
            raise LoginFailed(
                "This account only accepts a security key as its second factor.\n"
                "  Promarchy supports authenticator-app codes (TOTP). Add one in\n"
                "  Proton's account settings, then sign in again."
            )
        if pending.needs_totp and not _two_factor(client):
            raise LoginFailed("Two-factor authentication was not completed.")
        email = primary_address(client, username)
        pending.session.email = email
        try:
            displaced = store.replace(pending.session)
        except Exception as exc:
            raise LoginFailed(f"Signed in, but could not save the session to your keyring: {exc}") from exc
        committed = True
    except WrongCredentials as exc:
        raise LoginFailed(str(exc)) from exc
    except ProtonError as exc:
        raise LoginFailed(f"Sign-in failed: {exc}") from exc
    finally:
        # Anything short of a saved session (an error, a wrong 2FA code,
        # Ctrl+C) must not leave a live session behind on Proton's side.
        if not committed:
            logout(client)
    for leftover in store.cleanup_failures:
        print(
            f"Note: the keyring entry for {leftover} (the same Proton account) could not be deleted.\n"
            "Promarchy no longer uses it; you can remove it in your keyring manager (Passwords and Keys).",
            file=sys.stderr,
        )
    for old_session in displaced:
        if old_session.email != email:
            print(f"{old_session.email} is the same Proton account; it now appears as {email}.")
        if not logout(ProtonClient(old_session)):
            print(
                "Note: a session this sign-in replaced could not be revoked (Proton unreachable).\n"
                "You can end it under Proton's Settings → Security → Session management.",
                file=sys.stderr,
            )
    return email


def _two_factor(client: ProtonClient) -> bool:
    for attempt in range(3):
        code = _ask("Two-factor code: ")
        if not code:
            return False
        if submit_totp(client, code):
            return True
        if attempt < 2:
            print("That code was not accepted. Try the next one.")
    print("✗ Too many wrong codes.", file=sys.stderr)
    return False


def _human_verification(client: ProtonClient, exc: HumanVerificationRequired) -> HumanVerification | None:
    """Complete Proton's human verification the way Proton Bridge does.

    For a CAPTCHA, Proton verifies the token server-side once it is solved in
    a browser, so we open the page, wait for the user, and replay the same
    token on the next sign-in attempt.
    """
    methods = exc.methods
    token = exc.token
    print("\nProton asks for a quick human check before signing in from a new app.")
    if "captcha" in methods and token:
        url = str(exc.details.get("WebUrl") or "")
        if not url.startswith(VERIFY_PREFIX):
            url = f"{VERIFY_PREFIX}?methods=captcha&token={quote(token)}"
        # The link carries a short-lived verification token: it goes straight
        # to the browser and is never printed or logged.
        print("Opening the CAPTCHA on verify.proton.me in your browser. Solve it there, then come back.")
        open_in_browser(url)
        answer = _ask("Press Enter once it says you're verified (q to cancel): ")
        if answer.lower().startswith("q"):
            return None
        return HumanVerification(",".join(methods), token)
    if "email" in methods:
        address = _ask("Send a verification code to which email address? ")
        if address:
            try:
                request_verification_code(client, "email", address)
            except ProtonError as err:
                print(f"✗ Could not send the code: {err}", file=sys.stderr)
                return None
            code = _ask("Code from that email: ")
            if code:
                return HumanVerification("email", f"{address}:{code}")
    if "sms" in methods:
        phone = _ask("Send a code by SMS to which number (blank to skip)? ")
        if phone:
            try:
                request_verification_code(client, "sms", phone)
            except ProtonError as err:
                print(f"✗ Could not send the code: {err}", file=sys.stderr)
                return None
            code = _ask("Code from the SMS: ")
            if code:
                return HumanVerification("sms", f"{phone}:{code}")
    return None


def open_in_browser(url: str) -> None:
    launcher = shutil.which("omarchy-launch-browser")
    if launcher:
        subprocess.Popen(
            [launcher, url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    else:
        webbrowser.open(url)


def record_login(ok: bool, detail: str) -> None:
    """Leave the outcome where the daemon can show it in the dropdown."""
    path = state_dir() / "last-login.json"
    with contextlib.suppress(OSError):
        write_private(path, json.dumps({"time": int(time.time()), "ok": ok, "detail": detail[:300]}))


# -------------------------------------------------------------------- others


def cmd_logout(args: argparse.Namespace) -> int:
    """End sessions on Proton's side and forget them here, truthfully.

    A session has exactly one owner at a time, the only process allowed to
    renew or end it: the bar's background process while it runs, otherwise
    this command (holding the same lock, so the bar cannot start meanwhile).
    """
    store = SessionStore()
    accounts = store.accounts()
    if args.email and args.email.lower() not in accounts:
        print(f"✗ {args.email.lower()} is not signed in.", file=sys.stderr)
        return 1
    targets = accounts if args.all else [args.email.lower()] if args.email else accounts[:1]
    if not targets:
        print("Not signed in.")
        return 0
    target = None if args.all else targets[0]
    reply = ask_daemon({"cmd": "logout", "account": target, "local": args.local})
    if reply is not None:
        results = reply.get("results")
        if not isinstance(results, list):
            print("✗ The bar's background process could not sign out.", file=sys.stderr)
            return 1
        return max((_report(r) for r in results), default=0)
    lock = take_daemon_lock()
    if lock is None:
        print(
            "✗ The bar's background process is running but did not answer. Use Sign out in the\n"
            "  Promarchy dropdown, or try again in a moment.",
            file=sys.stderr,
        )
        return 1
    try:
        status = 0
        for email in targets:
            try:
                status = max(status, _report(_logout_locally(store, email, args.local)))
            except StoreUnavailable as exc:
                print(f"✗ {email}: {exc}", file=sys.stderr)
                status = 1
        return status
    finally:
        os.close(lock)


def ask_daemon(request: dict) -> dict | None:
    """Send a request to the running bar; None when it is not running."""
    path = runtime_dir() / "control.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(5)
            conn.connect(str(path))
            conn.settimeout(180)
            conn.sendall(json.dumps(request).encode() + b"\n")
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
    except (FileNotFoundError, ConnectionRefusedError):
        return None
    except OSError:
        return {}
    try:
        reply = json.loads(data.split(b"\n", 1)[0])
    except ValueError:
        return {}
    return reply if isinstance(reply, dict) else {}


def take_daemon_lock() -> int | None:
    """Become the sessions' owner while the bar is not running."""
    fd = open_private(runtime_dir() / "daemon.lock")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None


def _logout_locally(store: SessionStore, email: str, local: bool) -> dict:
    session = store.load(email)
    if session is None:
        # An empty answer can also mean a locked keyring: only forget the
        # account when asked to, and only if no session appeared meanwhile.
        if not local:
            return {"email": email, "outcome": "unreadable", "removed": False}
        removed = store.remove(email, require_absent=True)
        return {"email": email, "outcome": "no-session", "removed": removed}

    unsaved: list[Session] = []

    def persist(updated: Session) -> None:
        # Keep going if the keyring refuses: finish ending the session first.
        try:
            store.update_tokens(updated)
        except Exception:
            unsaved.append(updated)

    try:
        outcome = end_session(ProtonClient(session, on_refresh=persist))
    except RateLimited as exc:
        outcome = UNCONFIRMED
        print(f"Proton asked to wait {exc.retry_after} s before more requests.", file=sys.stderr)
    if unsaved:  # one more attempt before reporting
        with contextlib.suppress(Exception):
            store.update_tokens(unsaved[-1])
            unsaved.clear()
    removed = False
    if outcome in (REVOKED, REJECTED) or local:
        removed = store.remove(email, expected_uid=session.uid)
    return {"email": email, "outcome": outcome, "removed": removed, "unsaved": bool(unsaved)}


def _report(result: dict) -> int:
    """Print one account's sign-out result; return its exit status."""
    email, outcome, removed = result.get("email"), result.get("outcome"), result.get("removed")
    if outcome == REVOKED and removed:
        print(f"Signed out {email}.")
        return 0
    if outcome == REVOKED:
        print(f"Ended the session for {email}; it had already been replaced by a newer sign-in here.")
        return 0
    if outcome == REJECTED:
        print(
            f"{email}: Proton no longer accepts its saved credentials, so Promarchy cannot confirm the\n"
            "  session ended (it may have ended already, or its credentials were lost). "
            + ("Removed it here.\n" if removed else "\n")
            + "  To be sure, check Proton's Settings → Security → Session management.",
            file=sys.stderr,
        )
        return 1
    if outcome == "unreadable":
        print(
            f"✗ {email}: its saved session could not be read (is your keyring locked?).\n"
            "  Nothing was changed. Unlock the keyring and try again,\n"
            "  or add --local to remove it anyway.",
            file=sys.stderr,
        )
        return 1
    if outcome == "no-session":
        print(
            f"Removed {email} (it had no saved session)." if removed else f"Kept {email}: it signed in again."
        )
        return 0
    if outcome == "not-signed-in":
        print(f"{email} is not signed in.")
        return 0
    # UNCONFIRMED
    if removed:
        print(f"Forgot {email} on this machine. Its session could not be ended on Proton's side.")
        return 1
    print(
        f"✗ {email}: could not confirm with Proton that the session ended, so it is still signed in.\n"
        "  Try again when online, or add --local to forget it here anyway (then end it under\n"
        "  Proton's Settings → Security → Session management).",
        file=sys.stderr,
    )
    if result.get("unsaved"):
        print(
            "  Proton also issued fresh credentials that could not be saved to your keyring, so this\n"
            "  session can no longer be renewed here: sign in again, and end the old session under\n"
            "  Proton's Settings → Security → Session management.",
            file=sys.stderr,
        )
    return 1


def cmd_accounts(_: argparse.Namespace) -> int:
    accounts = SessionStore().accounts()
    if not accounts:
        print("Not signed in. Run: promarchyctl login")
    for email in accounts:
        print(email)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    path = runtime_dir() / "state.json"
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        print("The Promarchy daemon is not running (is the bar widget enabled?).", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(state, indent=2, ensure_ascii=False))
        return 0
    counts = state.get("counts") or {}
    if args.window:
        print(counts.get(args.window, 0))
        return 0
    print(f"status: {state.get('status')}" + (f" ({state['message']})" if state.get("message") else ""))
    for account in state.get("accounts") or []:
        print(f"account: {account.get('email')} [{account.get('status')}]")
    print("unread: " + "  ".join(f"{key} {counts.get(key, 0)}" for key in ("1h", "24h", "7d", "all")))
    return 0


def cmd_notify_test(_: argparse.Namespace) -> int:
    """Show what a new-mail notification looks like, using the newest message."""
    from .notify import Notifier

    icon = Path(__file__).resolve().parents[2] / "assets" / "promarchy.svg"
    try:
        state = json.loads((runtime_dir() / "state.json").read_text())
        message = (state.get("messages") or [])[0]
    except (OSError, ValueError, IndexError):
        message = {"id": "", "senderName": "Promarchy", "subject": "Notifications are working", "url": ""}
    url = message.get("url") or "https://mail.proton.me/inbox"
    Notifier(icon=str(icon) if icon.exists() else "").announce([message], {message["id"]: url}, url)
    print(f"Sent a sample notification for: {message.get('senderName')}")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    from .daemon import Daemon
    from .notify import Notifier

    sys.stdout.reconfigure(line_buffering=True)
    return Daemon(SessionStore(), notifier=Notifier(icon=args.icon)).run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="promarchyctl", description="Proton Mail for the Omarchy bar")
    parser.add_argument("--version", action="version", version=f"promarchy {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="sign in to a Proton account")
    login.add_argument("username", nargs="?", help="Proton username or address")
    login.add_argument("--wait", action="store_true", help="wait for Enter before exiting (used by the bar)")
    login.set_defaults(func=cmd_login)

    out = sub.add_parser("logout", help="end the session on Proton's side and forget it here")
    out.add_argument("email", nargs="?")
    out.add_argument("--all", action="store_true", help="sign out every account")
    out.add_argument(
        "--local", action="store_true", help="forget the session here even if Proton cannot be reached"
    )
    out.set_defaults(func=cmd_logout)

    sub.add_parser("accounts", help="list signed-in accounts").set_defaults(func=cmd_accounts)

    status = sub.add_parser("status", help="print the bar's current counts")
    status.add_argument("--json", action="store_true", help="print the full state snapshot")
    status.add_argument("--window", choices=("1h", "24h", "7d", "all"), help="print one count only")
    status.set_defaults(func=cmd_status)

    sub.add_parser("notify-test", help="show a sample new-mail notification").set_defaults(
        func=cmd_notify_test
    )

    daemon = sub.add_parser("daemon", help="run the bar backend (started by the shell)")
    daemon.add_argument("--icon", default="", help=argparse.SUPPRESS)
    daemon.set_defaults(func=cmd_daemon)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print()
        return 130
