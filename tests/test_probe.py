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


class ProbeAPI:
    """One conversation, with Proton's read/unread semantics."""

    def __init__(self, messages, fail_read_after_commit=False):
        self.messages = messages  # list of {"labels": set, "unread": bool}
        self.fail_read_after_commit = fail_read_after_commit
        self.puts = []

    def conversation(self):
        inbox = [m for m in self.messages if "0" in m["labels"]]
        return {
            "ID": "CID",
            "NumMessages": len(self.messages),
            "NumUnread": sum(m["unread"] for m in self.messages),
            "Labels": [
                {
                    "ID": "0",
                    "ContextNumMessages": len(inbox),
                    "ContextNumUnread": sum(m["unread"] for m in inbox),
                }
            ],
        }

    def get(self, path, params=None):
        if path == "/mail/v4/conversations":
            return {"Conversations": [self.conversation()]}
        return {"Conversation": self.conversation()}

    def put(self, path, body):
        self.puts.append(path)
        if path.endswith("/read"):
            for m in self.messages:
                m["unread"] = False
            if self.fail_read_after_commit:
                from promarchy.api import NetworkError

                raise NetworkError("reply lost")
        else:  # Proton marks the newest message in the label unread
            [m for m in self.messages if body["LabelID"] in m["labels"]][-1]["unread"] = True
        return {"Code": 1000}


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(probe, "snapshot", lambda: {"counts": {"all": 1}})
    monkeypatch.setattr(probe, "wait_for", lambda predicate, timeout=0: 0.0)


def test_roundtrip_refuses_conversations_it_could_not_restore(quick):
    api = ProbeAPI([{"labels": {"0"}, "unread": True}, {"labels": {"6"}, "unread": True}])
    result = probe.roundtrip(api, probe.Aliases())
    assert "skipped" in result and api.puts == []


def test_roundtrip_restores_even_when_the_read_reply_is_lost(quick):
    api = ProbeAPI([{"labels": {"0"}, "unread": True}], fail_read_after_commit=True)
    result = probe.roundtrip(api, probe.Aliases())
    assert api.puts == ["/mail/v4/conversations/read", "/mail/v4/conversations/unread"]
    assert api.messages[0]["unread"] is True
    assert result["restored"] is True and "readError" in result


def test_roundtrip_reports_a_failed_restore(quick):
    from promarchy.api import NetworkError

    class Broken(ProbeAPI):
        def put(self, path, body):
            if path.endswith("/unread"):
                self.puts.append(path)
                raise NetworkError("offline")
            return super().put(path, body)

    api = Broken([{"labels": {"0"}, "unread": True}], fail_read_after_commit=True)
    result = probe.roundtrip(api, probe.Aliases())
    assert result["restored"] is False and "restoreError" in result and "action" in result


def test_roundtrip_reports_an_exact_restore(quick):
    api = ProbeAPI([{"labels": {"0"}, "unread": True}])
    result = probe.roundtrip(api, probe.Aliases())
    assert result["restored"] is True and api.messages[0]["unread"] is True


def test_roundtrip_restores_after_ctrl_c(monkeypatch):
    monkeypatch.setattr(probe, "snapshot", lambda: {"counts": {"all": 1}})

    def interrupted(predicate, timeout=0):
        raise KeyboardInterrupt

    monkeypatch.setattr(probe, "wait_for", interrupted)
    api = ProbeAPI([{"labels": {"0"}, "unread": True}])
    result = probe.roundtrip(api, probe.Aliases())
    assert api.puts == ["/mail/v4/conversations/read", "/mail/v4/conversations/unread"]
    assert api.messages[0]["unread"] is True
    assert result["restored"] is True and result["interrupted"] == "KeyboardInterrupt"
