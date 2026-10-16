"""Command line entry points: ``promarchyctl <command>``."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import webbrowser

from . import __version__
from .api import HumanVerificationRequired, ProtonClient, ProtonError, WrongCredentials
from .auth import (
    HumanVerification,
    MissingDependency,
    begin_login,
    logout,
    primary_address,
    request_verification_code,
    submit_totp,
)
from .paths import runtime_dir
from .store import SessionStore, StoreUnavailable

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def _style(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if sys.stdout.isatty() else text


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


# --------------------------------------------------------------------- login


def cmd_login(args: argparse.Namespace) -> int:
    print(_style("Promarchy · sign in to Proton Mail", BOLD))
    print(_style("Your password is used locally for Proton's SRP handshake and is never stored.\n", DIM))
    store = SessionStore()
    try:
        _ = store.secrets  # fail early if the keyring is unavailable
    except StoreUnavailable as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    username = args.username or _ask("Proton username or address: ")
    if not username:
        print("✗ No username given.", file=sys.stderr)
        return 1
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
                    return 1
        else:
            print("✗ Proton did not accept the verification.", file=sys.stderr)
            return 1
    except WrongCredentials:
        print("✗ Incorrect username or password.", file=sys.stderr)
        return 1
    except MissingDependency as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    except ProtonError as exc:
        print(f"✗ Sign-in failed: {exc}", file=sys.stderr)
        return 1
    finally:
        password = ""  # noqa: F841 - drop our reference as early as possible

    client.session = pending.session
    try:
        if pending.needs_fido2_only:
            print(
                "✗ This account only accepts a security key as its second factor.\n"
                "  Promarchy supports authenticator-app codes (TOTP). Add one in\n"
                "  Proton's account settings, then sign in again.",
                file=sys.stderr,
            )
            logout(client)
            return 1
        if pending.needs_totp and not _two_factor(client):
            logout(client)
            return 1
        email = (primary_address(client) or username).strip().lower()
    except WrongCredentials as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    except ProtonError as exc:
        print(f"✗ Sign-in failed: {exc}", file=sys.stderr)
        logout(client)
        return 1

    pending.session.email = email
    previous = None
    try:
        previous = store.load(email)
    except Exception:
        previous = None
    store.save(pending.session)
    if previous and previous.uid != pending.session.uid:
        logout(ProtonClient(previous))  # revoke the session we just replaced
    print(f"\n✓ Signed in as {_style(email, BOLD)}. The bar picks this up within a few seconds.")
    return 0


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
    methods = exc.methods
    print("\nProton wants to confirm you are human before this sign-in.")
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
        phone = _ask("Or send a code by SMS to which number (blank to skip)? ")
        if phone:
            try:
                request_verification_code(client, "sms", phone)
            except ProtonError as err:
                print(f"✗ Could not send the code: {err}", file=sys.stderr)
                return None
            code = _ask("Code from the SMS: ")
            if code:
                return HumanVerification("sms", f"{phone}:{code}")
    if "captcha" in methods and exc.token:
        url = f"https://verify.proton.me/?methods=captcha&token={exc.token}"
        print(
            "Proton only offered a CAPTCHA, which has to be solved in a browser.\n"
            f"  {url}\n"
            "Solving it there does not hand the result back to a terminal. Sign in once at\n"
            "https://mail.proton.me from this network, wait a little, and run this again."
        )
        if _ask("Open the CAPTCHA page now? [y/N] ").lower().startswith("y"):
            webbrowser.open(url)
        return None
    print("✗ Proton did not offer a verification method Promarchy can handle.", file=sys.stderr)
    return None


# -------------------------------------------------------------------- others


def cmd_logout(args: argparse.Namespace) -> int:
    store = SessionStore()
    accounts = store.accounts()
    targets = accounts if args.all else [args.email.lower()] if args.email else accounts[:1]
    if not targets:
        print("Not signed in.")
        return 0
    for email in targets:
        if email not in accounts:
            print(f"✗ {email} is not signed in.", file=sys.stderr)
            return 1
        try:
            session = store.load(email)
        except StoreUnavailable as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 1
        if session:
            logout(ProtonClient(session))
        store.remove(email)
        print(f"Signed out {email}.")
    return 0


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
    login.set_defaults(func=cmd_login)

    out = sub.add_parser("logout", help="sign out and revoke the session")
    out.add_argument("email", nargs="?")
    out.add_argument("--all", action="store_true", help="sign out every account")
    out.set_defaults(func=cmd_logout)

    sub.add_parser("accounts", help="list signed-in accounts").set_defaults(func=cmd_accounts)

    status = sub.add_parser("status", help="print the bar's current counts")
    status.add_argument("--json", action="store_true", help="print the full state snapshot")
    status.add_argument("--window", choices=("1h", "24h", "7d", "all"), help="print one count only")
    status.set_defaults(func=cmd_status)

    daemon = sub.add_parser("daemon", help="run the bar backend (started by the shell)")
    daemon.add_argument("--icon", default="", help=argparse.SUPPRESS)
    daemon.set_defaults(func=cmd_daemon)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print()
        return 130
