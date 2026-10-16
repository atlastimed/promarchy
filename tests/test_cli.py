from __future__ import annotations

import json

from promarchy import cli
from promarchy.api import HumanVerificationRequired


def hv_error(methods, url=None):
    details = {"HumanVerificationToken": "tok123", "HumanVerificationMethods": methods}
    if url is not None:
        details["WebUrl"] = url
    return HumanVerificationRequired("verify", code=9001, status=422, details=details)


def test_captcha_opens_browser_and_replays_token(monkeypatch):
    opened = []
    monkeypatch.setattr(cli, "open_in_browser", opened.append)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    hv = cli._human_verification(
        None, hv_error(["captcha"], "https://verify.proton.me/?methods=captcha&token=tok123")
    )
    assert opened == ["https://verify.proton.me/?methods=captcha&token=tok123"]
    assert hv.headers() == {
        "x-pm-human-verification-token-type": "captcha",
        "x-pm-human-verification-token": "tok123",
    }


def test_captcha_url_from_elsewhere_is_not_opened(monkeypatch):
    opened = []
    monkeypatch.setattr(cli, "open_in_browser", opened.append)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    cli._human_verification(None, hv_error(["captcha"], "https://evil.example/phish"))
    assert opened == ["https://verify.proton.me/?methods=captcha&token=tok123"]


def test_captcha_can_be_cancelled(monkeypatch):
    monkeypatch.setattr(cli, "open_in_browser", lambda url: None)
    monkeypatch.setattr("builtins.input", lambda prompt="": "q")
    assert cli._human_verification(None, hv_error(["captcha"])) is None


def test_login_outcome_is_recorded(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "state_dir", lambda: tmp_path)
    cli.record_login(False, "Incorrect username or password.")
    data = json.loads((tmp_path / "last-login.json").read_text())
    assert data["ok"] is False
    assert data["detail"] == "Incorrect username or password."
    assert oct((tmp_path / "last-login.json").stat().st_mode & 0o777) == "0o600"


def test_status_window(monkeypatch, tmp_path, capsys):
    (tmp_path / "state.json").write_text(json.dumps({"status": "ok", "counts": {"24h": 4}}))
    monkeypatch.setattr(cli, "runtime_dir", lambda: tmp_path)
    assert cli.main(["status", "--window", "24h"]) == 0
    assert capsys.readouterr().out.strip() == "4"
