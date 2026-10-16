"""The long-running process behind the bar widget.

Protocol (one JSON object per line):

* stdout -> ``{"type": "state", ...}`` snapshots, emitted only when something
  the bar shows has changed. The same snapshot is mirrored to
  ``$XDG_RUNTIME_DIR/promarchy/state.json`` for scripts (``promarchyctl status``).
* stdin  <- commands from the QML service: ``configure``, ``refresh``,
  ``markRead``. EOF on stdin means the shell went away, so we exit.

Between polls the process sleeps in a blocking queue read, so it costs
nothing while idle.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import queue
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from .api import AuthExpired, NetworkError, ProtonClient, ProtonError, RateLimited, Session
from .auth import UNCONFIRMED, end_session
from .avatars import Avatars
from .demo import demo_state
from .log import log_error
from .mailbox import WEB_BASE, Mailbox, parse_categories
from .notify import Notifier, Watermarks, select_new
from .paths import open_private, runtime_dir, write_private
from .store import SessionStore, StoreUnavailable

STAMP_CHECK_SECONDS = 5
LOCK_WAIT_SECONDS = 15
MAX_BACKOFF = 300
STORE_RETRY_MAX = 120
AUTH_RECHECK_SECONDS = 120
SHUTDOWN_GRACE = 10  # seconds a SIGTERM waits for the current step
DEMO_INTERVAL = 30.0
NOTICE_SECONDS = 120


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _flag(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


@dataclass
class Config:
    poll_seconds: int = 30
    recent_limit: int = 8
    categories: tuple[str, ...] = ()
    threads: bool = True
    notify: bool = True
    sender_images: bool = True
    demo: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> Config:
        base = cls()
        return cls(
            poll_seconds=_clamp(data.get("pollSeconds"), 10, 900, base.poll_seconds),
            recent_limit=_clamp(data.get("recentLimit"), 0, 25, base.recent_limit),
            categories=parse_categories(data.get("categories", "all")),
            threads=_flag(data.get("groupThreads"), base.threads),
            notify=_flag(data.get("notify"), base.notify),
            sender_images=_flag(data.get("senderImages"), base.sender_images),
            demo=_flag(data.get("demo"), False),
        )


@dataclass
class Account:
    email: str
    client: ProtonClient
    mailbox: Mailbox
    status: str = "starting"  # starting | ok | offline | error | auth-expired
    error: str = ""
    failures: int = 0
    next_poll: float = 0.0  # on the monotonic clock
    # Proton's Retry-After is a hard deadline (even a manual refresh waits);
    # error backoff only holds off automatic polls such as opening the menu.
    rate_limited_until: float = 0.0
    backoff_until: float = 0.0
    extra: dict = field(default_factory=dict)


class Daemon:
    def __init__(
        self,
        store: SessionStore,
        *,
        out: TextIO | None = None,
        notifier: Notifier | None = None,
        watermarks: Watermarks | None = None,
        avatars: Avatars | None = None,
        clock: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
        client_factory: Callable[..., ProtonClient] = ProtonClient,
        state_path: str | None = None,
    ) -> None:
        self.store = store
        self.out = out or sys.stdout
        self.notifier = notifier or Notifier()
        self.watermarks = watermarks or Watermarks()
        self.avatars = avatars or Avatars()
        # Wall clock for mail timestamps and window ages; monotonic clock for
        # scheduling, so a clock correction cannot stall polling.
        self.clock = clock
        self.mono = mono
        self.client_factory = client_factory
        self.state_path = state_path
        self.config = Config()
        self.accounts: dict[str, Account] = {}
        # SimpleQueue.put is safe to call from a signal handler; Queue.put is not.
        self.commands: queue.SimpleQueue[dict] = queue.SimpleQueue()
        self.generation: int | None = None
        self.store_error = ""
        self.store_failures = 0
        self.store_retry_at = 0.0
        self.missing: set[str] = set()  # in the index, but no session in the keyring
        self.unsaved: dict[str, Session] = {}  # rotated tokens not yet persisted
        self.unsaved_failures = 0
        self.unsaved_retry_at = 0.0
        self.configured = False
        self.stopping = False
        self.notice = ""
        self.notice_until = 0.0
        self.demo_anchor: int | None = None
        self._last_payload = ""
        self._force_emit = False

    # ------------------------------------------------------------ lifecycle

    def run(self, stdin: TextIO | None = None) -> int:
        lock_fd = self._acquire_lock()
        if lock_fd is None:
            self.emit(
                force=True,
                override={"status": "error", "message": "Another Promarchy daemon is already running"},
            )
            return 3
        threading.Thread(target=self._read_commands, args=(stdin or sys.stdin,), daemon=True).start()
        # The shell stops us with SIGTERM; finish the current step (a token
        # refresh must be persisted) and leave through the normal path.
        signal.signal(signal.SIGTERM, self._on_sigterm)
        try:
            self._sync_accounts()
            self.emit(force=True)
            try:
                self.avatars.prune()
            except OSError:
                log_error("pruning the logo cache")
            # Give the shell a moment to send its settings before the first
            # sync, so we do not fetch with defaults and then again.
            self._drain(timeout=1.5)
            while not self.stopping:
                self.tick()
                if self.stopping:
                    break
                self._drain(timeout=self._sleep_for())
        finally:
            self._persist_unsaved(force=True)
            for account in self.accounts.values():
                account.client.close()
            os.close(lock_fd)
        return 0

    def _on_sigterm(self, *_: object) -> None:
        self.commands.put({"cmd": "quit"})
        # A step can be slow (a keyring unlock prompt, network timeouts); do
        # not hold up a replacement daemon waiting for our lock.
        watchdog = threading.Timer(SHUTDOWN_GRACE, os._exit, args=(0,))
        watchdog.daemon = True
        watchdog.start()

    def tick(self) -> None:
        if self.config.demo:
            if self.demo_anchor is None:
                self.demo_anchor = int(self.clock())
            self.emit(force=self._force_emit)
            self._force_emit = False
            return
        self._persist_unsaved()
        self._sync_accounts()
        now = self.mono()
        for account in list(self.accounts.values()):
            check_at = account.extra.get("auth_check_at")
            if account.status == "auth-expired" and check_at is not None and now >= check_at:
                account.extra["auth_check_at"] = now + AUTH_RECHECK_SECONDS
                if self._adopt_stored_tokens(account):
                    account.next_poll = now
            # One gate for every scheduled request: Proton's Retry-After wins
            # over whatever set next_poll (a command, a settings change).
            if account.next_poll <= now and now >= account.rate_limited_until:
                try:
                    self._poll(account)
                except Exception as exc:  # a bug must not kill the bar's data source
                    log_error("polling", exc)
                    self._record_failure(account, ProtonError(f"Internal error: {exc.__class__.__name__}"))
        # An explicit refresh always answers, even when nothing changed, so
        # the widget can stop its spinner.
        self.emit(force=self._force_emit)
        self._force_emit = False

    def _sleep_for(self) -> float:
        if self.config.demo:
            return DEMO_INTERVAL
        mono, wall = self.mono(), self.clock()
        delay = float(STAMP_CHECK_SECONDS)
        for account in self.accounts.values():
            delay = min(delay, account.next_poll - mono)
            boundary = account.mailbox.next_boundary(int(wall)) if account.mailbox.synced else None
            if boundary is not None:
                delay = min(delay, boundary - wall)
        return max(0.05, delay)

    # ------------------------------------------------------------- commands

    def _read_commands(self, stream: TextIO) -> None:
        try:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    command = json.loads(line)
                except ValueError:
                    continue
                if isinstance(command, dict):
                    self.commands.put(command)
        finally:
            self.commands.put({"cmd": "quit"})

    def _drain(self, timeout: float) -> None:
        try:
            command = self.commands.get(timeout=timeout)
        except queue.Empty:
            return
        while True:
            try:
                self.handle(command)
            except Exception as exc:  # a bad command or a bug must not end the session
                log_error(f"handling {str(command.get('cmd'))[:20]!r}", exc)
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                return

    def handle(self, command: dict) -> None:
        name = command.get("cmd")
        if name == "quit":
            self.stopping = True
        elif name == "configure":
            self._configure(Config.from_dict(command.get("settings") or {}))
        elif name == "refresh":
            # An explicit refresh may cut an error backoff short, never
            # Proton's own Retry-After.
            self._force_emit = True
            for account in self.accounts.values():
                account.next_poll = account.rate_limited_until
                account.extra["force"] = True
        elif name == "poll":
            for account in self.accounts.values():
                if account.status != "auth-expired":
                    account.next_poll = max(account.rate_limited_until, account.backoff_until)
        elif name == "logout":
            self._logout(command.get("account"))
        elif name == "markRead":
            kind = "conversation" if command.get("kind") == "conversation" else "message"
            self._mark_read(str(command.get("account") or ""), command.get("ids") or [], kind)

    def _configure(self, config: Config) -> None:
        old = self.config
        self.config = config
        self.configured = True
        for account in self.accounts.values():
            needs_sync = account.mailbox.configure(
                categories=config.categories, recent_limit=config.recent_limit, threads=config.threads
            )
            if needs_sync or (config.sender_images and not old.sender_images):
                account.next_poll = account.rate_limited_until
                account.extra["force"] = True
            elif config.poll_seconds < old.poll_seconds:
                account.next_poll = max(
                    account.rate_limited_until, min(account.next_poll, self.mono() + config.poll_seconds)
                )

    def _logout(self, target: Any) -> None:
        """Revoke and forget accounts (all, or one by email). An account is
        only removed once Proton confirmed the revocation, so a sign-out
        while offline is reported instead of silently leaving a live session."""
        emails = [str(target)] if isinstance(target, str) and target else list(self.accounts)
        failed = []
        for email in emails:
            account = self.accounts.get(email)
            if account is None or account.client.session is None:
                continue
            if self.mono() < account.rate_limited_until:
                failed.append(email)
                continue
            uid = account.client.session.uid
            try:
                outcome = end_session(account.client, lambda email=email: self.store.load_locked(email))
            except RateLimited as exc:
                self._record_failure(account, exc)  # keep Proton's Retry-After
                failed.append(email)
                continue
            if outcome == UNCONFIRMED:
                failed.append(email)
                continue
            try:
                self.store.remove(email, expected_uid=uid)
            except Exception as exc:
                log_error("removing an account", exc)
                failed.append(email)
        if failed:
            self._set_notice(
                f"Could not sign out {', '.join(failed)}: Proton could not be reached or the keyring "
                "refused. Still signed in; try again."
            )
        self._sync_accounts()
        self.emit(force=True)

    def _set_notice(self, text: str) -> None:
        self.notice = text
        self.notice_until = self.mono() + NOTICE_SECONDS

    def _mark_read(self, email: str, ids: list[Any], kind: str = "message") -> None:
        account = self.accounts.get(email)
        if account is None:
            return
        wait = account.rate_limited_until - self.mono()
        if wait > 0:
            self._set_notice(
                f"Proton asked Promarchy to wait; try marking as read again in {int(wait) + 1} s."
            )
            self.emit()
            return
        try:
            account.mailbox.mark_read([str(i) for i in ids if isinstance(i, str)], kind)
            self._after_sync(account)
        except ProtonError as exc:
            self._record_failure(account, exc)
        except Exception as exc:
            log_error("marking read", exc)
            self._record_failure(account, ProtonError(f"Internal error: {exc.__class__.__name__}"))
        self.emit()

    # -------------------------------------------------------------- accounts

    def _sync_accounts(self) -> None:
        generation = self.store.generation()
        if generation == self.generation:
            return
        if (self.store_error or self.missing) and self.mono() < self.store_retry_at:
            return
        emails = self.store.accounts()
        for email in list(self.accounts):
            if email not in emails:
                self.accounts.pop(email).client.close()
                self.unsaved.pop(email, None)
                try:
                    self.watermarks.forget(email)
                except OSError as exc:
                    log_error("forgetting a watermark", exc)
        missing: set[str] = set()
        for email in emails:
            try:
                session = self.store.load(email)
            except Exception as exc:  # keyring locked, not up yet, D-Bus hiccup
                self._store_failed(exc)
                return
            current = self.accounts.get(email)
            if session is None:
                # Keep a running account: a dismissed unlock prompt looks the
                # same as a missing item, and we still hold working tokens.
                if current is None:
                    missing.add(email)
                continue
            if current and current.client.session and current.client.session.uid == session.uid:
                continue
            if session.user_id and any(
                other != email and acc.client.session and acc.client.session.user_id == session.user_id
                for other, acc in self.accounts.items()
            ):
                # The same Proton account under another address (an old entry
                # a newer sign-in has not displaced yet): never count it twice.
                log_error("skipping a duplicate entry for an account already loaded", RuntimeError())
                continue
            if current:
                current.client.close()
            self.unsaved.pop(email, None)
            self.accounts[email] = self._make_account(email, session)
        self.store_error = ""
        self.missing = missing
        if missing:
            # An empty answer may be a locked keyring or a dismissed unlock
            # prompt rather than a deleted item, so look again later.
            self._schedule_store_retry()
            return
        self.generation = generation
        self.store_failures = 0

    def _store_failed(self, exc: Exception) -> None:
        """Remember the failure and try again later instead of giving up."""
        self._schedule_store_retry()
        if isinstance(exc, StoreUnavailable):
            self.store_error = str(exc)
        else:
            self.store_error = f"Could not read the keyring: {exc.__class__.__name__}"

    def _schedule_store_retry(self) -> None:
        self.generation = None
        self.store_failures += 1
        self.store_retry_at = self.mono() + min(STORE_RETRY_MAX, 5 * 2 ** min(self.store_failures, 5))

    def _make_account(self, email: str, session: Session) -> Account:
        def persist(updated: Session) -> None:
            # Refresh tokens are single-use: until this lands in the keyring,
            # a restart would sign the account out. Retried with backoff.
            self.unsaved[email] = updated
            self._persist_unsaved(force=True)

        client = self.client_factory(
            session, on_refresh=persist, refresh_guard=lambda: self.store.refresh_guard(email)
        )
        mailbox = Mailbox(
            client,
            email,
            categories=self.config.categories,
            recent_limit=self.config.recent_limit,
            threads=self.config.threads,
            clock=self.clock,
        )
        return Account(email=email, client=client, mailbox=mailbox)

    def _persist_unsaved(self, force: bool = False) -> None:
        if not self.unsaved or (not force and self.mono() < self.unsaved_retry_at):
            return
        failed = False
        for email, session in list(self.unsaved.items()):
            try:
                self.store.update_tokens(session)
            except Exception as exc:
                log_error("saving rotated tokens", exc)
                failed = True
                continue
            # Saved, or deliberately skipped because the account was signed
            # out or replaced by a newer sign-in meanwhile.
            self.unsaved.pop(email, None)
        if failed:
            self.unsaved_failures += 1
            self.unsaved_retry_at = self.mono() + min(STORE_RETRY_MAX, 5 * 2 ** min(self.unsaved_failures, 5))
        else:
            self.unsaved_failures = 0

    # ------------------------------------------------------------------ poll

    def _poll(self, account: Account) -> None:
        force = account.extra.pop("force", False)
        try:
            if force and account.mailbox.synced:
                account.mailbox.resync()
                changed = True
            else:
                changed = account.mailbox.poll()
        except ProtonError as exc:
            self._record_failure(account, exc)
            return
        except Exception as exc:  # a bug must not kill the bar's data source
            log_error("syncing", exc)
            self._record_failure(account, ProtonError(f"Internal error: {exc.__class__.__name__}"))
            return
        account.status = "ok"
        account.error = ""
        account.failures = 0
        account.backoff_until = 0.0
        account.next_poll = self.mono() + self.config.poll_seconds
        if changed:
            self._after_sync(account)

    def _record_failure(self, account: Account, exc: ProtonError) -> None:
        now = self.mono()
        account.error = str(exc)
        if isinstance(exc, AuthExpired):
            if self._adopt_stored_tokens(account):
                account.error = ""
                account.next_poll = now  # retry at once with the live tokens
                return
            account.status = "auth-expired"
            account.next_poll = float("inf")  # until a sign-in, or tokens appear (see tick)
            account.extra["auth_check_at"] = now + AUTH_RECHECK_SECONDS
            return
        account.failures += 1
        if isinstance(exc, RateLimited):
            account.rate_limited_until = now + exc.retry_after
            account.next_poll = account.rate_limited_until
            account.status = "ok" if account.mailbox.synced else "error"
            return
        account.status = "offline" if isinstance(exc, NetworkError) else "error"
        account.backoff_until = now + min(MAX_BACKOFF, 5 * 2 ** min(account.failures, 6))
        account.next_poll = account.backoff_until

    def _adopt_stored_tokens(self, account: Account) -> bool:
        """Recover when another process renewed this session.

        Refresh tokens are single-use: if the CLI (or anything else using the
        stored session) renewed it, ours was consumed and Proton rejects it,
        although the keyring holds a working pair for the very same session.
        Take that pair instead of declaring the session expired.
        """
        current = account.client.session
        try:
            stored = self.store.load(account.email)
        except Exception as exc:
            log_error("re-reading a session", exc)
            return False
        if stored is None or current is None or stored.uid != current.uid:
            return False
        if stored.refresh_token == current.refresh_token:
            return False
        account.client.session = stored
        self.unsaved.pop(account.email, None)
        return True

    def _after_sync(self, account: Account) -> None:
        mailbox = account.mailbox
        now = int(self.clock())
        shown, total, mark = select_new(
            mailbox.unread_times, mailbox.fresh, self.watermarks.get(account.email), now
        )
        if self.config.notify and total:
            urls = {m["id"]: mailbox.web_url(m) for m in shown}
            self.notifier.announce(shown, urls, mailbox.web_url(), total=total)
        try:
            self.watermarks.set(account.email, mark)
        except OSError as exc:
            log_error("saving the notification watermark", exc)
        if self.config.sender_images and mailbox.recent:
            self.emit()  # show the list now; logos follow
            try:
                self.avatars.attach(mailbox.recent, account.client)
            except ProtonError:
                pass
            except Exception as exc:  # logos are decoration; never let them stop mail
                log_error("fetching sender logos", exc)

    # ----------------------------------------------------------------- state

    def snapshot(self) -> dict:
        now = int(self.clock())
        if self.config.demo:
            return demo_state(self.demo_anchor or now, self.config.recent_limit)
        totals = {"1h": 0, "24h": 0, "7d": 0, "all": 0}
        rows: list[dict] = []
        accounts = []
        capped = False
        synced_any = False
        for email, account in self.accounts.items():
            mailbox = account.mailbox
            counts = mailbox.counts(now) if mailbox.synced else {k: 0 for k in totals}
            if mailbox.synced:
                synced_any = True
                capped = capped or mailbox.capped()
                for key in totals:
                    totals[key] += counts[key]
                rows.extend(mailbox.recent)
            accounts.append(
                {
                    "email": email,
                    "status": account.status,
                    "error": account.error,
                    "counts": counts,
                    "inboxUrl": mailbox.web_url(),
                }
            )
        for email in sorted(self.missing):
            accounts.append(
                {
                    "email": email,
                    "status": "auth-expired",
                    "error": "No saved session (keyring locked?). Sign in again if this persists.",
                    "counts": {k: 0 for k in totals},
                    "inboxUrl": f"{WEB_BASE}/inbox",
                }
            )
        rows.sort(key=lambda r: r["time"], reverse=True)
        rows = rows[: self.config.recent_limit]
        status, message = self._overall_status(accounts, synced_any)
        if self.notice and self.mono() < self.notice_until:
            message = f"{self.notice} {message}".strip()
        return {
            "type": "state",
            "status": status,
            "message": message,
            "accounts": accounts,
            "counts": totals,
            "capped": capped,
            "messages": rows,
            "inboxUrl": accounts[0]["inboxUrl"] if accounts else f"{WEB_BASE}/inbox",
            "multiAccount": len(accounts) > 1,
        }

    def _overall_status(self, accounts: list[dict], synced_any: bool) -> tuple[str, str]:
        loaded = [a for a in accounts if a["email"] not in self.missing]
        if self.store_error and not loaded:
            return "error", self.store_error
        if not accounts:
            return "signed-out", self._last_login_failure() or "Sign in to see your mail"
        problems = [a for a in accounts if a["status"] not in ("ok", "starting")]
        statuses = {a["status"] for a in accounts}
        if "ok" in statuses and synced_any:
            notes = [f"{a['email']}: {a['error'] or a['status']}" for a in problems]
            if self.store_error:
                notes.append(self.store_error)
            # Some accounts work and some do not: say so, so partial totals
            # are never mistaken for the whole picture.
            return ("partial" if notes else "ok"), "; ".join(notes)
        if statuses == {"auth-expired"}:
            first = accounts[0]
            if first["email"] in self.missing:
                return "auth-expired", (
                    f"Could not find the saved session for {first['email']}. "
                    "If your keyring is locked, unlock it; otherwise sign in again."
                )
            return "auth-expired", "Your Proton session expired. Sign in again."
        if "offline" in statuses:
            return "offline", next(a["error"] for a in accounts if a["status"] == "offline")
        if "error" in statuses:
            return "error", next(a["error"] for a in accounts if a["status"] == "error")
        return "starting", "Connecting to Proton…"

    def _last_login_failure(self) -> str:
        """A recent failed sign-in, so the dropdown can say what went wrong."""
        try:
            data = json.loads((self.store.root / "last-login.json").read_text())
        except (OSError, ValueError):
            return ""
        if not isinstance(data, dict) or data.get("ok") or time.time() - float(data.get("time") or 0) > 1800:
            return ""
        return f"Last sign-in failed: {str(data.get('detail') or 'unknown error')[:200]}"

    def emit(self, force: bool = False, override: dict | None = None) -> None:
        state = self.snapshot()
        if override:
            state.update(override)
        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        if payload == self._last_payload and not force:
            return
        self._last_payload = payload
        try:
            self.out.write(payload + "\n")
            self.out.flush()
        except (BrokenPipeError, ValueError):
            self.stopping = True
            return
        self._mirror(payload)

    def _mirror(self, payload: str) -> None:
        path = Path(self.state_path) if self.state_path else runtime_dir() / "state.json"
        with contextlib.suppress(OSError):
            write_private(path, payload + "\n")

    # ------------------------------------------------------------------ lock

    def _acquire_lock(self) -> int | None:
        fd = open_private(runtime_dir() / "daemon.lock")
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                # A previous instance may still be shutting down after a reload.
                if time.monotonic() > deadline:
                    os.close(fd)
                    return None
                time.sleep(0.25)
