import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

REAL_RUNTIME = os.environ.get("XDG_RUNTIME_DIR", "")


@pytest.fixture(autouse=True)
def isolated_xdg(tmp_path, monkeypatch):
    """Never let a test touch the real Promarchy state, keyring index, lock or
    control socket of a bar that may be running on this machine."""
    for var in ("XDG_RUNTIME_DIR", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        path = tmp_path / "xdg" / var.lower()
        path.mkdir(parents=True, mode=0o700)
        monkeypatch.setenv(var, str(path))
    yield
