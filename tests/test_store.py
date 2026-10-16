from __future__ import annotations

from fakes import MemorySecrets

from promarchy.api import Session
from promarchy.store import SessionStore


def make(tmp_path):
    secrets = MemorySecrets()
    return SessionStore(secrets=secrets, root=tmp_path), secrets


def test_save_load_remove(tmp_path):
    store, secrets = make(tmp_path)
    assert store.accounts() == []
    store.save(
        Session(uid="u", access_token="ACCESS-TOKEN", refresh_token="REFRESH-TOKEN", email="me@proton.me")
    )
    assert store.accounts() == ["me@proton.me"]
    assert store.load("me@proton.me").refresh_token == "REFRESH-TOKEN"
    assert list(secrets.data) == ["session:me@proton.me"]
    # Tokens never land in the plain index file.
    index = (tmp_path / "accounts.json").read_text()
    assert "TOKEN" not in index
    store.remove("me@proton.me")
    assert store.accounts() == []
    assert secrets.data == {}


def test_generation_bumps_on_sign_in_not_on_rotation(tmp_path):
    store, _ = make(tmp_path)
    g0 = store.generation()
    session = Session(uid="u", access_token="a", refresh_token="r", email="me@proton.me")
    store.save(session)
    g1 = store.generation()
    assert g1 != g0
    session.refresh_token = "r2"
    store.save(session, announce=False)
    assert store.generation() == g1
    assert store.load("me@proton.me").refresh_token == "r2"


def test_index_is_private(tmp_path):
    store, _ = make(tmp_path)
    store.save(Session(uid="u", access_token="a", refresh_token="r", email="me@proton.me"))
    assert oct((tmp_path / "accounts.json").stat().st_mode & 0o777) == "0o600"


def test_corrupt_secret_is_ignored(tmp_path):
    store, secrets = make(tmp_path)
    secrets.set("session:me@proton.me", "{not json")
    assert store.load("me@proton.me") is None
