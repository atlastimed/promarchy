"""Where sessions live.

Tokens go into the desktop keyring (GNOME Keyring's Secret Service on
Omarchy) via libsecret's ``secret-tool``. Only the list of signed-in addresses is kept in a plain file so the
daemon can enumerate accounts without touching secrets. Writers take a file
lock, and every change bumps a stamp file the daemon watches with a cheap
``stat()`` so a sign-in from the terminal shows up in the bar within seconds.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from .api import Session
from .log import log_error
from .paths import open_private, state_dir, write_private

KEYRING_SERVICE = "promarchy"


class StoreUnavailable(RuntimeError):
    pass


class SaveIndeterminate(StoreUnavailable):
    """A write was attempted and may or may not have landed (the helper
    timed out or crashed). Never treat this as "nothing changed"."""


class SecretBackend(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...


class SecretToolBackend:
    """Secret Service access through libsecret's ``secret-tool``.

    A short-lived subprocess per read or write (they happen at start-up and
    when tokens rotate, about once a day) keeps the long-running daemon free
    of a D-Bus and crypto stack it would otherwise carry around.
    """

    LEGACY_APPLICATION = "Python keyring library"  # items written by 0.1.0 pre-releases

    def __init__(self) -> None:
        tool = shutil.which("secret-tool")
        if not tool:
            raise StoreUnavailable("secret-tool is missing. Install it with:\n  omarchy pkg add libsecret")
        self.tool = tool

    def _run(self, *args: str, secret: str | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [self.tool, *args],
                input=secret,
                capture_output=True,
                text=True,
                timeout=60,  # an unlock prompt may be waiting for the user
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise StoreUnavailable(f"Could not talk to the keyring: {exc.__class__.__name__}") from exc

    @staticmethod
    def _no_match(result: subprocess.CompletedProcess) -> bool:
        # secret-tool's only quiet failure: exit status 1, nothing on stderr.
        # Anything else (a signal, another status, a message) is a real error.
        return result.returncode == 1 and not result.stderr.strip()

    @staticmethod
    def _failure(action: str, result: subprocess.CompletedProcess) -> StoreUnavailable:
        detail = result.stderr.strip()[:200] or f"secret-tool exited with status {result.returncode}"
        return StoreUnavailable(f"{action}: {detail}")

    def get(self, key: str) -> str | None:
        result = self._run("lookup", "service", KEYRING_SERVICE, "username", key)
        if result.returncode == 0:
            return result.stdout.rstrip("\n") or None
        if self._no_match(result):
            return None
        raise self._failure("Keyring lookup failed", result)

    def set(self, key: str, value: str) -> None:
        label = f"Promarchy: Proton Mail session ({key.removeprefix('session:')})"
        try:
            result = self._run(
                "store",
                "--label",
                label,
                "service",
                KEYRING_SERVICE,
                "username",
                key,
                "application",
                "promarchy",
                secret=value,
            )
        except StoreUnavailable as exc:  # timed out or crashed mid-write
            raise SaveIndeterminate(str(exc)) from exc
        if result.returncode != 0:  # the helper refused: nothing was stored
            raise self._failure("Could not save to the keyring", result)
        try:  # tidying up after older versions must not turn a save into a failure
            self._run(
                "clear", "service", KEYRING_SERVICE, "username", key, "application", self.LEGACY_APPLICATION
            )
        except StoreUnavailable as exc:
            log_error("clearing a legacy keyring item", exc)

    def delete(self, key: str) -> None:
        result = self._run("clear", "service", KEYRING_SERVICE, "username", key)
        if result.returncode != 0 and not self._no_match(result):  # no match: already gone
            raise self._failure("Could not remove the session from the keyring", result)


class SessionStore:
    def __init__(self, secrets: SecretBackend | None = None, root: Path | None = None) -> None:
        self._secrets = secrets
        self.root = root or state_dir()
        self._index = self.root / "accounts.json"
        self._lock = self.root / "accounts.lock"
        self._stamp = self.root / "accounts.stamp"
        self._held = threading.local()
        self.cleanup_failures: list[str] = []  # keyring items replace() could not delete
        self.index_failed = False  # replace() saved the session but not the account list

    @property
    def secrets(self) -> SecretBackend:
        # Resolved lazily so listing accounts never needs the keyring.
        if self._secrets is None:
            self._secrets = SecretToolBackend()
        return self._secrets

    # ------------------------------------------------------------- queries

    def accounts(self) -> list[str]:
        try:
            data = json.loads(self._index.read_text())
        except (OSError, ValueError):
            return []
        accounts = data.get("accounts") if isinstance(data, dict) else None
        return [str(a) for a in accounts or [] if isinstance(a, str) and a]

    def load(self, email: str) -> Session | None:
        raw = self.secrets.get(_key(email))
        if not raw:
            return None
        try:
            return Session.from_dict(json.loads(raw))
        except (ValueError, KeyError, TypeError):
            return None

    def generation(self) -> int:
        """Changes whenever an account is added, removed or re-authenticated."""
        try:
            return self._stamp.stat().st_mtime_ns
        except OSError:
            return 0

    # ------------------------------------------------------------- updates

    def save(self, session: Session, *, announce: bool = True) -> None:
        """Persist a session. Token rotations pass announce=False: the daemon
        made that change itself and must not treat it as a new sign-in."""
        self.replace(session, announce=announce)

    def replace(self, session: Session, *, announce: bool = True) -> list[Session]:
        """Store a (new) session and return every session it displaced.

        A Proton account is identified by its UserID, not by the address you
        typed: signing in again through another of its addresses replaces the
        old entry instead of adding the same mailbox twice. Read and write
        happen under one lock, so racing sign-ins each get back exactly what
        they displaced, and the caller revokes it on Proton's side.

        Outcomes: StoreUnavailable means nothing changed; SaveIndeterminate
        means the new session may or may not be stored; a return means it is
        stored, with any leftovers in `cleanup_failures` / `index_failed`.
        """
        # One spelling per address: the keyring key is case-insensitive, so
        # "Me@x" and "me@x" must never be treated as two accounts.
        session.email = session.email.strip().lower()
        if not session.email:
            raise ValueError("session has no account email")
        displaced: list[Session] = []
        self.cleanup_failures = []
        self.index_failed = False
        with self._locked():
            accounts = list(
                dict.fromkeys(session.email if a.lower() == session.email else a for a in self.accounts())
            )
            aliases = []
            if session.user_id:
                for other in [a for a in accounts if a != session.email]:
                    stored = self.load(other)
                    if stored is not None and stored.user_id == session.user_id:
                        aliases.append((other, stored))
            previous = self.load(session.email)
            # Save the new session before removing anything: if this fails,
            # every existing entry is still intact.
            try:
                self.secrets.set(_key(session.email), json.dumps(session.to_dict()))
            except StoreUnavailable:
                raise
            except Exception as exc:
                raise SaveIndeterminate("the keyring may or may not hold the new session") from exc
            # From here on the new session is saved: finish what we can and
            # report the rest, never raise as if nothing had happened.
            if previous is not None and previous.uid != session.uid:
                displaced.append(previous)
            for _, stored in aliases:
                if stored.uid != session.uid:  # the same session merely re-keyed: keep it alive
                    displaced.append(stored)
            try:
                if session.email not in accounts:
                    accounts.append(session.email)
                    announce = True
                for other, _ in aliases:
                    # The new sign-in is authoritative: the old address leaves
                    # the index even if its keyring item cannot be deleted
                    # (reported, not hidden).
                    try:
                        self.secrets.delete(_key(other))
                    except StoreUnavailable:
                        self.cleanup_failures.append(other)
                    accounts.remove(other)
                    announce = True
                if accounts != self.accounts():
                    self._write_index(accounts)
                if announce:
                    self._bump()
            except Exception as exc:
                log_error("updating the account list", exc)
                self.index_failed = True
        return displaced

    def update_tokens(self, session: Session) -> bool:
        """Persist rotated tokens, but only onto the same live session.

        A sign-out or a fresh sign-in from the terminal may have happened
        while the daemon was refreshing; writing then would resurrect the
        account or clobber the newer session. Returns True when written.
        """
        with self._locked():
            if session.email not in self.accounts():
                return False
            stored = self.load(session.email)
            if stored is None:
                # Unreadable (a locked keyring answers like a missing item):
                # never write blind over what may be a newer sign-in. The
                # caller keeps the tokens and tries again.
                raise StoreUnavailable("Could not read the saved session to update it")
            if stored.uid != session.uid:
                return False
            self.secrets.set(_key(session.email), json.dumps(session.to_dict()))
            return True

    def remove(self, email: str, expected_uid: str | None = None, *, require_absent: bool = False) -> bool:
        """Forget an account, only if it is still in the state the caller saw.

        `expected_uid`: that exact session must still be the stored one (a
        newer sign-in is never thrown away). `require_absent`: there must
        still be no stored session (the caller found none). The keyring entry
        goes first: if that fails, the index is untouched and the error
        propagates, so nothing is half-removed."""
        with self._locked():
            if expected_uid is not None or require_absent:
                stored = self.load(email)
                if stored is not None and (require_absent or stored.uid != expected_uid):
                    return False
            self.secrets.delete(_key(email))
            self._write_index([a for a in self.accounts() if a != email])
            self._bump()
        return True

    # ------------------------------------------------------------ internals

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        # Re-entrant for the thread holding it; exclusive across threads and
        # processes through flock on a fresh descriptor.
        depth = getattr(self._held, "depth", 0)
        if depth:
            self._held.depth = depth + 1
            try:
                yield
            finally:
                self._held.depth = depth
            return
        fd = open_private(self._lock)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._held.depth = 1
            yield
        finally:
            self._held.depth = 0
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _write_index(self, accounts: list[str]) -> None:
        write_private(self._index, json.dumps({"accounts": accounts}))

    def _bump(self) -> None:
        fd = open_private(self._stamp)
        try:
            os.utime(fd)
        finally:
            os.close(fd)


def _key(email: str) -> str:
    return f"session:{email.strip().lower()}"
