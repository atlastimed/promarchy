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
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TextIO

from .api import AuthExpired, NetworkError, ProtonClient, ProtonError, RateLimited, Session
from .avatars import Avatars
from .mailbox import WEB_BASE, Mailbox, parse_categories
from .notify import Notifier, Watermarks, select_new
from .paths import runtime_dir
from .store import SessionStore, StoreUnavailable

STAMP_CHECK_SECONDS = 5
LOCK_WAIT_SECONDS = 15
MAX_BACKOFF = 300


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
    theme: str = "dark"

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
            theme="light" if str(data.get("theme", "")).lower() == "light" else "dark",
        )


@dataclass
class Account:
    email: str
    client: ProtonClient
    mailbox: Mailbox
    status: str = "starting"  # starting | ok | offline | error | auth-expired
    error: str = ""
    failures: int = 0
    next_poll: float = 0.0
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
        client_factory: Callable[..., ProtonClient] = ProtonClient,
        state_path: str | None = None,
    ) -> None:
        self.store = store
        self.out = out or sys.stdout
        self.notifier = notifier or Notifier()
        self.watermarks = watermarks or Watermarks()
        self.avatars = avatars or Avatars()
        self.clock = clock
        self.client_factory = client_factory
        self.state_path = state_path
        self.config = Config()
        self.accounts: dict[str, Account] = {}
        self.commands: queue.Queue[dict] = queue.Queue()
        self.generation: int | None = None
        self.store_error = ""
        self.configured = False
        self.stopping = False
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
        try:
            self._sync_accounts()
            self.emit(force=True)
            # Give the shell a moment to send its settings before the first
            # sync, so we do not fetch with defaults and then again.
            self._drain(timeout=1.5)
            while not self.stopping:
                self.tick()
                if self.stopping:
                    break
                self._drain(timeout=self._sleep_for())
        finally:
            for account in self.accounts.values():
                account.client.close()
            os.close(lock_fd)
        return 0

    def tick(self) -> None:
        self._sync_accounts()
        now = self.clock()
        for account in list(self.accounts.values()):
            if account.next_poll <= now:
                self._poll(account)
        # An explicit refresh always answers, even when nothing changed, so
        # the widget can stop its spinner.
        self.emit(force=self._force_emit)
        self._force_emit = False

    def _sleep_for(self) -> float:
        now = self.clock()
        wake = now + STAMP_CHECK_SECONDS
        for account in self.accounts.values():
            wake = min(wake, account.next_poll)
            boundary = account.mailbox.next_boundary(int(now)) if account.mailbox.synced else None
            if boundary is not None:
                wake = min(wake, boundary)
        return max(0.05, wake - now)

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
            self.handle(command)
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
            self._force_emit = True
            for account in self.accounts.values():
                account.next_poll = 0
                account.extra["force"] = True
        elif name == "poll":
            for account in self.accounts.values():
                if account.status != "auth-expired":
                    account.next_poll = 0
        elif name == "markRead":
            self._mark_read(str(command.get("account") or ""), command.get("ids") or [])

    def _configure(self, config: Config) -> None:
        old = self.config
        self.config = config
        self.configured = True
        self.avatars.mode = config.theme
        for account in self.accounts.values():
            needs_sync = account.mailbox.configure(
                categories=config.categories, recent_limit=config.recent_limit, threads=config.threads
            )
            if needs_sync or (config.sender_images and not old.sender_images) or config.theme != old.theme:
                account.next_poll = 0
                account.extra["force"] = True
            elif config.poll_seconds < old.poll_seconds:
                account.next_poll = min(account.next_poll, self.clock() + config.poll_seconds)

    def _mark_read(self, email: str, ids: list[Any]) -> None:
        account = self.accounts.get(email)
        if account is None:
            return
        try:
            account.mailbox.mark_read([str(i) for i in ids if isinstance(i, str)])
            self._after_sync(account)
        except ProtonError as exc:
            self._record_failure(account, exc)
        self.emit()

    # -------------------------------------------------------------- accounts

    def _sync_accounts(self) -> None:
        generation = self.store.generation()
        if generation == self.generation:
            return
        self.generation = generation
        emails = self.store.accounts()
        for email in list(self.accounts):
            if email not in emails:
                self.accounts.pop(email).client.close()
                self.watermarks.forget(email)
        self.store_error = ""
        for email in emails:
            try:
                session = self.store.load(email)
            except StoreUnavailable as exc:
                self.store_error = str(exc)
                return
            except Exception as exc:  # keyring backends raise their own types
                self.store_error = f"Could not read the keyring: {exc.__class__.__name__}"
                return
            current = self.accounts.get(email)
            if session is None:
                if current:
                    self.accounts.pop(email).client.close()
                continue
            if current and current.client.session and current.client.session.uid == session.uid:
                continue
            if current:
                current.client.close()
            self.accounts[email] = self._make_account(email, session)

    def _make_account(self, email: str, session: Session) -> Account:
        def persist(updated: Session) -> None:
            try:
                self.store.save(updated, announce=False)
            except Exception as exc:  # never lose the in-memory session over this
                print(f"promarchy: could not save rotated tokens: {exc.__class__.__name__}", file=sys.stderr)

        client = self.client_factory(session, on_refresh=persist)
        mailbox = Mailbox(
            client,
            email,
            categories=self.config.categories,
            recent_limit=self.config.recent_limit,
            threads=self.config.threads,
            clock=self.clock,
        )
        return Account(email=email, client=client, mailbox=mailbox)

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
            traceback.print_exc(file=sys.stderr)
            self._record_failure(account, ProtonError(f"Internal error: {exc.__class__.__name__}"))
            return
        account.status = "ok"
        account.error = ""
        account.failures = 0
        account.next_poll = self.clock() + self.config.poll_seconds
        if changed:
            self._after_sync(account)

    def _record_failure(self, account: Account, exc: ProtonError) -> None:
        now = self.clock()
        account.error = str(exc)
        if isinstance(exc, AuthExpired):
            account.status = "auth-expired"
            account.next_poll = float("inf")  # until a new sign-in bumps the stamp
            return
        account.failures += 1
        if isinstance(exc, RateLimited):
            account.next_poll = now + exc.retry_after
            account.status = "ok" if account.mailbox.synced else "error"
            return
        account.status = "offline" if isinstance(exc, NetworkError) else "error"
        account.next_poll = now + min(MAX_BACKOFF, 5 * 2 ** min(account.failures, 6))

    def _after_sync(self, account: Account) -> None:
        mailbox = account.mailbox
        now = int(self.clock())
        new, mark = select_new(mailbox.fresh, self.watermarks.get(account.email), now)
        if self.config.notify and new:
            urls = {m["id"]: mailbox.web_url(m) for m in new}
            self.notifier.announce(new, urls, mailbox.web_url())
        self.watermarks.set(account.email, mark)
        if self.config.sender_images and mailbox.recent:
            self.emit()  # show the list now; logos follow
            try:
                self.avatars.attach(mailbox.recent, account.client)
            except ProtonError:
                pass

    # ----------------------------------------------------------------- state

    def snapshot(self) -> dict:
        now = int(self.clock())
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
        rows.sort(key=lambda r: r["time"], reverse=True)
        rows = rows[: self.config.recent_limit]
        status, message = self._overall_status(accounts, synced_any)
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
        if self.store_error:
            return "error", self.store_error
        if not accounts:
            return "signed-out", self._last_login_failure() or "Sign in to see your mail"
        statuses = {a["status"] for a in accounts}
        if statuses == {"auth-expired"}:
            return "auth-expired", "Your Proton session expired. Sign in again."
        if "ok" in statuses and synced_any:
            problems = [a for a in accounts if a["status"] != "ok"]
            return "ok", (f"{problems[0]['email']}: {problems[0]['error']}" if problems else "")
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
        path = self.state_path or str(runtime_dir() / "state.json")
        with contextlib.suppress(OSError):
            tmp = path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(payload + "\n")
            os.replace(tmp, path)

    # ------------------------------------------------------------------ lock

    def _acquire_lock(self) -> int | None:
        fd = os.open(runtime_dir() / "daemon.lock", os.O_RDWR | os.O_CREAT, 0o600)
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
