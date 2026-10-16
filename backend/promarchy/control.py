"""How `promarchyctl` reaches the session owner.

A session has exactly one owner at a time, the only process allowed to renew,
replace or end it: the bar's background process while it runs, otherwise a
`promarchyctl` command holding the same lock (so the bar cannot start
halfway). Commands reach the running bar through a Unix socket in the private
runtime directory; the bar also checks the caller's uid.

Protocol: one JSON request line, one JSON reply line.

* ``{"cmd": "logout", "account": null | "<email>", "local": bool}``: null is
  every account. Reply ``{"results": [...]}``.
* ``{"cmd": "adopt", "session": {...}}``: a fresh sign-in, to be saved by the
  owner, which also ends the sessions it replaces with its own working
  tokens. Reply ``{"saved": true, "displaced": [...], "cleanupFailures": [...]}``
  or ``{"saved": false, "error": "..."}``.
* Errors: ``{"error": "unsupported request" | "invalid request" | "busy" |
  "no answer"}``. "busy" means the request never ran; "no answer" (made up
  on the client side) means it may or may not have.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import threading
from pathlib import Path
from typing import Any

from .api import Session
from .paths import open_private, runtime_dir

SOCKET_NAME = "control.sock"
LOCK_NAME = "daemon.lock"
MAX_REQUEST = 16384
CONNECT_TIMEOUT = 5
REPLY_TIMEOUT = 600  # the owner always answers; this only bounds a hung one
NO_ANSWER = {"error": "no answer"}


class BadRequest(ValueError):
    pass


def socket_path() -> Path:
    return runtime_dir() / SOCKET_NAME


def parse(line: bytes) -> dict:
    """Validate a request completely before anything acts on it."""
    try:
        request = json.loads(line or b"{}")
    except ValueError:
        raise BadRequest("invalid request") from None
    if not isinstance(request, dict):
        raise BadRequest("invalid request")
    cmd = request.get("cmd")
    if cmd == "logout":
        account = request.get("account")
        local = request.get("local", False)
        if account is not None and not (isinstance(account, str) and account.strip()):
            raise BadRequest("invalid request")  # never let a bad selector mean "all"
        if not isinstance(local, bool):
            raise BadRequest("invalid request")
        return {"cmd": "logout", "account": account.strip().lower() if account else None, "local": local}
    if cmd == "adopt":
        try:
            session = Session.from_dict(request["session"])
        except (KeyError, TypeError, ValueError):
            raise BadRequest("invalid request") from None
        if not session.email:
            raise BadRequest("invalid request")
        return {"cmd": "adopt", "session": session}
    raise BadRequest("unsupported request")


class Ticket:
    """A request from the socket, waiting for the owner's main loop.

    It either runs to completion and is answered, or, if it waited in the
    queue for too long, it never runs at all: a caller told "busy" can rely
    on nothing having happened.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = "queued"
        self._done = threading.Event()
        self.result: dict = {"error": "busy"}

    def start(self) -> bool:
        with self._lock:
            if self._state != "queued":
                return False
            self._state = "started"
            return True

    def finish(self, result: dict) -> None:
        self.result = result
        self._done.set()

    def wait(self, timeout: float) -> dict:
        if self._done.wait(timeout):
            return self.result
        with self._lock:
            if self._state == "queued":
                self._state = "expired"
                return {"error": "busy"}
        self._done.wait()  # started: the owner always finishes it
        return self.result


def ask(request: dict[str, Any], timeout: float = REPLY_TIMEOUT) -> dict | None:
    """Send a request to the running bar.

    None: no bar is running. NO_ANSWER: the request may or may not have run
    (the connection broke or the reply was unreadable).
    """
    data = b""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(CONNECT_TIMEOUT)
            conn.connect(str(socket_path()))
            conn.settimeout(timeout)
            conn.sendall(json.dumps(request).encode() + b"\n")
            while b"\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
    except (FileNotFoundError, ConnectionRefusedError):
        return None
    except OSError:
        return dict(NO_ANSWER)
    try:
        reply = json.loads(data.split(b"\n", 1)[0])
    except ValueError:
        return dict(NO_ANSWER)
    return reply if isinstance(reply, dict) else dict(NO_ANSWER)


def take_lock() -> int | None:
    """Become the sessions' owner while the bar is not running."""
    fd = open_private(runtime_dir() / LOCK_NAME)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None
