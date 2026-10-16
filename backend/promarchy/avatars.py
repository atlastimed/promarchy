"""Sender logos, fetched through Proton's own logo proxy and cached on disk.

This is the same endpoint the Proton web app uses for its sender images, so
no third-party server learns who mails you. Only senders Proton flags with
``DisplaySenderImage`` are looked up, a handful per sync at most, and both
hits and misses are cached so steady state costs no requests at all.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from .api import ProtonClient, ProtonError
from .paths import cache_dir

HIT_TTL = 14 * 86400
MISS_TTL = 2 * 86400
FETCH_BUDGET = 6
MAX_BYTES = 256 * 1024
EXTENSIONS = {"image/png": ".png", "image/svg+xml": ".svg", "image/jpeg": ".jpg", "image/webp": ".webp"}


class Avatars:
    def __init__(self, root: Path | None = None, clock=time.time) -> None:
        self.root = root or cache_dir() / "logos"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.clock = clock
        # Proton's logos are drawn for light backgrounds; the widget puts them
        # on a light tile in every theme.
        self.mode = "light"

    def attach(self, rows: list[dict], client: ProtonClient) -> bool:
        """Set row["avatar"] where a logo is known. True if anything changed."""
        budget = FETCH_BUDGET
        changed = False
        for row in rows:
            address = row.get("senderAddress") or ""
            if not row.get("senderImage") or not address:
                continue
            found = self._cached(address)
            if found is None and budget > 0:
                budget -= 1
                found = self._fetch(address, client)
            path = str(found) if found else ""
            if row.get("avatar", "") != path:
                row["avatar"] = path
                changed = True
        return changed

    def _stem(self, address: str) -> str:
        digest = hashlib.sha256(f"{self.mode}:{address.strip().lower()}".encode()).hexdigest()
        return digest[:40]

    def _cached(self, address: str) -> Path | None | bool:
        """Path for a hit, False for a remembered miss, None when unknown."""
        stem = self._stem(address)
        now = self.clock()
        for ext in EXTENSIONS.values():
            path = self.root / f"{stem}{ext}"
            try:
                if now - path.stat().st_mtime < HIT_TTL:
                    return path
            except OSError:
                continue
        miss = self.root / f"{stem}.miss"
        try:
            if now - miss.stat().st_mtime < MISS_TTL:
                return False
        except OSError:
            pass
        return None

    def _fetch(self, address: str, client: ProtonClient) -> Path | bool:
        stem = self._stem(address)
        try:
            status, ctype, body = client.fetch_bytes(
                "/core/v4/images/logo",
                params={"Address": address, "Size": 64, "Mode": self.mode},
            )
        except ProtonError:
            return False
        ext = EXTENSIONS.get(ctype)
        if status != 200 or not ext or not body or len(body) > MAX_BYTES:
            (self.root / f"{stem}.miss").touch(mode=0o600)
            return False
        path = self.root / f"{stem}{ext}"
        tmp = path.with_suffix(ext + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
        os.replace(tmp, path)
        return path
