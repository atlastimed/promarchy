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
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from .api import Session
from .paths import open_private, state_dir, write_private

KEYRING_SERVICE = "promarchy"


class StoreUnavailable(RuntimeError):
    pass


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

    def get(self, key: str) -> str | None:
        result = self._run("lookup", "service", KEYRING_SERVICE, "username", key)
        if result.returncode != 0:
            if result.stderr.strip():
                raise StoreUnavailable(f"Keyring lookup failed: {result.stderr.strip()[:200]}")
            return None  # no such item
        return result.stdout.rstrip("\n") or None

    def set(self, key: str, value: str) -> None:
        label = f"Promarchy: Proton Mail session ({key.removeprefix('session:')})"
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
        if result.returncode != 0:
            raise StoreUnavailable(f"Could not save to the keyring: {result.stderr.strip()[:200]}")
        self._run(
            "clear", "service", KEYRING_SERVICE, "username", key, "application", self.LEGACY_APPLICATION
        )

    def delete(self, key: str) -> None:
        result = self._run("clear", "service", KEYRING_SERVICE, "username", key)
        # Exit status 1 with no message means nothing matched: already gone.
        if result.returncode != 0 and result.stderr.strip():
            detail = result.stderr.strip()[:200]
            raise StoreUnavailable(f"Could not remove the session from the keyring: {detail}")


class SessionStore:
    def __init__(self, secrets: SecretBackend | None = None, root: Path | None = None) -> None:
        self._secrets = secrets
        self.root = root or state_dir()
        self._index = self.root / "accounts.json"
        self._lock = self.root / "accounts.lock"
        self._stamp = self.root / "accounts.stamp"

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

    def replace(self, session: Session, *, announce: bool = True) -> Session | None:
        """Store a (new) session and return the different one it replaced.

        Read and write happen under one lock, so two sign-ins racing for the
        same account each get back exactly the session they displaced, and
        the caller can revoke it instead of leaving it valid on Proton's side.
        """
        if not session.email:
            raise ValueError("session has no account email")
        with self._locked():
            previous = self.load(session.email)
            self.secrets.set(_key(session.email), json.dumps(session.to_dict()))
            accounts = self.accounts()
            if session.email not in accounts:
                accounts.append(session.email)
                self._write_index(accounts)
                announce = True
            if announce:
                self._bump()
        return previous if previous is not None and previous.uid != session.uid else None

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
            if stored is not None and stored.uid != session.uid:
                return False
            self.secrets.set(_key(session.email), json.dumps(session.to_dict()))
            return True

    def remove(self, email: str, expected_uid: str | None = None) -> bool:
        """Forget an account. With `expected_uid`, only if that exact session
        is still the stored one (a newer sign-in must not be thrown away).
        The keyring entry goes first: if that fails, the index is untouched
        and the error propagates, so nothing is half-removed."""
        with self._locked():
            if expected_uid is not None:
                stored = self.load(email)
                if stored is not None and stored.uid != expected_uid:
                    return False
            self.secrets.delete(_key(email))
            self._write_index([a for a in self.accounts() if a != email])
            self._bump()
        return True

    # ------------------------------------------------------------ internals

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        fd = open_private(self._lock)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
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
