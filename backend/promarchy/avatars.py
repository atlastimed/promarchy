"""Sender logos, fetched through Proton's own logo proxy and cached on disk.

This is the same endpoint the Proton web app uses for its sender images, so
no third-party server learns who mails you (Proton itself does see which
address is looked up). Only senders Proton flags with ``DisplaySenderImage``
are fetched, a handful per sync at most, and hits and misses are both cached
so steady state costs no requests. A rate limit ends the pass and pauses
logo lookups until Proton's retry time.

Only raster images are accepted. SVG can reference other files, and no
byte-level filter is a sound boundary for it (an encoding change defeats
pattern matching), so we ask Proton to rasterise (``Format=png``), check the
file signature of whatever comes back, and never store or display SVG.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from .api import ProtonClient, ProtonError, RateLimited
from .paths import cache_dir, ensure_private_dir, write_private

HIT_TTL = 14 * 86400
MISS_TTL = 2 * 86400
FETCH_BUDGET = 6
MAX_BYTES = 512 * 1024
EXTENSIONS = (".png", ".jpg", ".webp")


def raster_extension(body: bytes) -> str | None:
    """Identify PNG, JPEG or WebP by signature; anything else is refused."""
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if body.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return ".webp"
    return None


class Avatars:
    def __init__(self, root: Path | None = None, clock=time.time) -> None:
        self.root = ensure_private_dir(root or cache_dir() / "logos")
        self.clock = clock
        # Proton's logos are drawn for light backgrounds; the widget puts them
        # on a light tile in every theme.
        self.mode = "light"
        self.paused_until = 0.0

    def attach(self, rows: list[dict], client: ProtonClient) -> bool:
        """Set row["avatar"] where a logo is known. True if anything changed."""
        budget = FETCH_BUDGET if self.clock() >= self.paused_until else 0
        changed = False
        for row in rows:
            address = row.get("senderAddress") or ""
            if not row.get("senderImage") or not address:
                continue
            found = self._cached(address)
            if found is None and budget > 0:
                budget -= 1
                try:
                    found = self._fetch(address, client)
                except RateLimited as exc:
                    self.paused_until = self.clock() + exc.retry_after
                    budget = 0
                    found = None
            path = str(found) if found else ""
            if row.get("avatar", "") != path:
                row["avatar"] = path
                changed = True
        return changed

    def prune(self) -> int:
        """Delete expired hits and misses, and anything that is not a raster
        logo or miss marker (such as SVGs cached by earlier versions)."""
        now = self.clock()
        removed = 0
        for path in self.root.iterdir():
            try:
                if path.suffix not in (*EXTENSIONS, ".miss"):
                    if path.is_file() or path.is_symlink():
                        path.unlink()
                        removed += 1
                    continue
                ttl = MISS_TTL if path.suffix == ".miss" else HIT_TTL
                if path.is_file() and not path.is_symlink() and now - path.stat().st_mtime >= ttl:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def _stem(self, address: str) -> str:
        digest = hashlib.sha256(f"{self.mode}:{address.strip().lower()}".encode()).hexdigest()
        return digest[:40]

    def _cached(self, address: str) -> Path | None | bool:
        """Path for a hit, False for a remembered miss, None when unknown."""
        stem = self._stem(address)
        now = self.clock()
        for ext in EXTENSIONS:
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
                params={"Address": address, "Size": 64, "Mode": self.mode, "Format": "png"},
                max_bytes=MAX_BYTES,
            )
        except RateLimited:
            raise
        except ProtonError:
            return False  # transient: try again on a later sync
        ext = raster_extension(body) if status == 200 and ctype.startswith("image/") else None
        if ext is None:
            write_private(self.root / f"{stem}.miss", b"")
            return False
        path = self.root / f"{stem}{ext}"
        write_private(path, body)
        return path
