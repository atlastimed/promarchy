"""Per-user directories, created private (0700) on first use."""

from __future__ import annotations

import os
from pathlib import Path


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var, "")
    # The XDG spec says relative values are invalid and must be ignored.
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / fallback


def _ensure(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def state_dir() -> Path:
    """Durable, non-secret state: account index and notification watermarks."""
    return _ensure(_xdg("XDG_STATE_HOME", ".local/state") / "promarchy")


def cache_dir() -> Path:
    """Disposable data: sender logos and the GnuPG home used for SRP checks."""
    return _ensure(_xdg("XDG_CACHE_HOME", ".cache") / "promarchy")


def runtime_dir() -> Path:
    """Per-boot files: the daemon lock and the latest state snapshot."""
    base = os.environ.get("XDG_RUNTIME_DIR", "")
    if base and os.path.isabs(base) and os.path.isdir(base):
        return _ensure(Path(base) / "promarchy")
    return _ensure(cache_dir() / "run")
