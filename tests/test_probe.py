"""The reviewer probe must never leak text or renew the session."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from promarchy.api import AuthExpired, Session

SPEC = importlib.util.spec_from_file_location(
    "proton_probe", Path(__file__).resolve().parents[1] / "scripts" / "proton_probe.py"
)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)

SECRET = "Secret Subject From alice@example.com"


def test_shapes_never_echo_strings():
    data = {"Subject": SECRET, "Senders": [{"Address": SECRET}], "Labels": [{"ID": "0", "ContextTime": 5}]}
    text = json.dumps(probe.shape(data))
    assert SECRET not in text and "alice" not in text


def test_redacted_rows_keep_numbers_and_hide_text_and_ids():
    alias = probe.Aliases()
    row = {
        "id": "REALID==",
        "conversationId": "REALID==",
        "kind": "conversation",
        "senderName": SECRET,
        "subject": SECRET,
        "senderAddress": "alice@example.com",
        "time": 90,
        "threadCount": 3,
        "unreadIds": ["REALID=="],
        "unread": True,
    }
    out = probe.redact_row(row, alias, now=100)
    text = json.dumps(out)
    assert SECRET not in text and "REALID" not in text and "alice" not in text
    assert out["threadCount"] == 3 and out["ageSeconds"] == 10 and out["id"] == "c1"


def test_probe_client_can_never_renew():
    client = probe.NoRenewClient(Session(uid="u", access_token="a", refresh_token="r", email="me@proton.me"))
    with pytest.raises(AuthExpired):
        client.refresh()
