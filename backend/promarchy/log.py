"""Diagnostics that never echo secrets.

Exception messages can embed request headers (an access token), server data
or mail metadata, and the shell keeps everything we write to stderr in its
log. Logged errors therefore carry only the exception type and the code
location, which is enough to find a bug without copying data into logs.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path


def describe(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    where = " <- ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(frames[-4:]))
    return f"{type(exc).__name__}" + (f" at {where}" if where else "")


def log_error(context: str, exc: BaseException | None = None) -> None:
    exc = exc if exc is not None else sys.exc_info()[1]
    detail = describe(exc) if exc is not None else "unknown error"
    print(f"promarchy: {context}: {detail}", file=sys.stderr, flush=True)
