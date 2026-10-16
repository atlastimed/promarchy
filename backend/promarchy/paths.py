"""Per-user directories and private file writes.

Directories are verified on every use: they must be real directories (not
symlinks) owned by us, and are tightened to 0700 if something loosened them.
Files are written through a randomly named temporary file created with
O_EXCL in the same directory, then renamed into place, so a planted symlink
or a pre-existing file with loose permissions can never be written through.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from pathlib import Path


class UnsafePath(RuntimeError):
    pass


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var, "")
    # The XDG spec says relative values are invalid and must be ignored.
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / fallback


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafePath(f"{path} is not a directory")
    if info.st_uid != os.getuid():
        raise UnsafePath(f"{path} is owned by another user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(path, 0o700)
    return path


def write_private(path: Path, data: str | bytes) -> None:
    """Atomically replace `path` with `data`, readable only by us."""
    payload = data.encode() if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def open_private(path: Path) -> int:
    """Open (creating if needed) a lock or stamp file without following symlinks."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with contextlib.suppress(OSError):
        os.fchmod(fd, 0o600)
    return fd


def state_dir() -> Path:
    """Durable, non-secret state: account index and notification watermarks."""
    return ensure_private_dir(_xdg("XDG_STATE_HOME", ".local/state") / "promarchy")


def cache_dir() -> Path:
    """Disposable data: sender logos and the GnuPG home used for SRP checks."""
    return ensure_private_dir(_xdg("XDG_CACHE_HOME", ".cache") / "promarchy")


def runtime_dir() -> Path:
    """Per-boot files: the daemon lock and the latest state snapshot."""
    base = os.environ.get("XDG_RUNTIME_DIR", "")
    if base and os.path.isabs(base) and os.path.isdir(base):
        return ensure_private_dir(Path(base) / "promarchy")
    return ensure_private_dir(cache_dir() / "run")
