"""Where sessions live.

Tokens go into the desktop keyring (GNOME Keyring's Secret Service on
Omarchy). Only the list of signed-in addresses is kept in a plain file so the
daemon can enumerate accounts without touching secrets. Writers take a file
lock, and every change bumps a stamp file the daemon watches with a cheap
``stat()`` so a sign-in from the terminal shows up in the bar within seconds.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from .api import Session
from .paths import state_dir

KEYRING_SERVICE = "promarchy"


class StoreUnavailable(RuntimeError):
    pass


class SecretBackend(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...


class KeyringBackend:
    def __init__(self) -> None:
        try:
            import keyring
            from keyring.backends import fail
        except ImportError as exc:
            raise StoreUnavailable(
                "python-keyring is not installed. Install it with:\n  omarchy pkg add python-keyring"
            ) from exc
        if isinstance(keyring.get_keyring(), fail.Keyring):
            raise StoreUnavailable("No desktop keyring is available (is gnome-keyring running?)")
        self._keyring = keyring

    def get(self, key: str) -> str | None:
        return self._keyring.get_password(KEYRING_SERVICE, key)

    def set(self, key: str, value: str) -> None:
        self._keyring.set_password(KEYRING_SERVICE, key, value)

    def delete(self, key: str) -> None:
        with contextlib.suppress(self._keyring.errors.PasswordDeleteError):
            self._keyring.delete_password(KEYRING_SERVICE, key)


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
            self._secrets = KeyringBackend()
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
        if not session.email:
            raise ValueError("session has no account email")
        with self._locked():
            self.secrets.set(_key(session.email), json.dumps(session.to_dict()))
            accounts = self.accounts()
            if session.email not in accounts:
                accounts.append(session.email)
                self._write_index(accounts)
                announce = True
            if announce:
                self._bump()

    def remove(self, email: str) -> None:
        with self._locked():
            self.secrets.delete(_key(email))
            self._write_index([a for a in self.accounts() if a != email])
            self._bump()

    # ------------------------------------------------------------ internals

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        fd = os.open(self._lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _write_index(self, accounts: list[str]) -> None:
        tmp = self._index.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"accounts": accounts}, fh)
        os.replace(tmp, self._index)

    def _bump(self) -> None:
        self._stamp.touch(mode=0o600)
        # touch() keeps the old mtime when the file already exists on some
        # filesystems with coarse timestamps; force a fresh one.
        os.utime(self._stamp)


def _key(email: str) -> str:
    return f"session:{email.strip().lower()}"
