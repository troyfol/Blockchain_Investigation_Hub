"""Settings API tests (P5): connectors · keys->keyring · cases folder · offline.

Hardest constraint = the CREDENTIAL BOUNDARY: a key value must NEVER appear in any settings response.
Tests install an IN-MEMORY keyring backend so they never read/write the real OS Credential Manager.
"""

from __future__ import annotations

from pathlib import Path

import keyring
import pytest
from fastapi.testclient import TestClient
from keyring.backend import KeyringBackend

from backend.app.db import apply_migrations, get_connection
from backend.app.db import repository as repo
from backend.app.main import app, get_case_db_path


class MemoryKeyring(KeyringBackend):
    """A throwaway in-memory keyring so tests never touch the real OS keyring (and report available)."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self._store.get((service, username))

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def delete_password(self, service, username):
        self._store.pop((service, username), None)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("BIH_APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("BIH_CASES_ROOT", str(tmp_path / "cases_root"))
    monkeypatch.delenv("BIH_ALLOW_PLAINTEXT_KEYS", raising=False)
    prev = keyring.get_keyring()
    keyring.set_keyring(MemoryKeyring())  # in-memory: never touch the real Credential Manager
    from backend.app.services import cases

    cases.clear_active_case()
    yield
    cases.clear_active_case()
    keyring.set_keyring(prev)


@pytest.fixture
def client():
    return TestClient(app)


def _seed_case(tmp_path) -> Path:
    from backend.tests.integration.test_seeded_case import seed_evm_transfer

    db = tmp_path / "settings_case" / "case.db"
    apply_migrations(db)
    conn = get_connection(db)
    repo.init_case(conn, title="Settings Case")
    seed_evm_transfer(conn, final=True)
    conn.close()
    return db


# --------------------------------------------------------------------------- credential boundary

def test_get_settings_masks_keys_and_lists_pillars(client):
    from backend.app import secrets

    secrets.set_secret("bitquery_token", "SUPER-SECRET-VALUE-123")
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    assert "SUPER-SECRET-VALUE-123" not in resp.text  # the value is NEVER serialized

    body = resp.json()
    free = {c["name"] for c in body["connectors"]["free"]}
    assert {"etherscan", "esplora", "defillama", "graphsense", "ofac", "chainalysis-free"} <= free
    assert all(c["always_on"] for c in body["connectors"]["free"])

    bq = next(p for p in body["connectors"]["paid"] if p["name"] == "bitquery")
    assert bq["key_present"] is True            # presence only...
    assert "key" not in bq and "keyring" not in bq  # ...never the value or even the slot id


def test_no_key_value_is_ever_serialized(client):
    secret = "tok_LIVE_should_never_appear_ABC987"
    post = client.post("/api/settings/keys/arkham-api", json={"key": secret})
    assert post.status_code == 200 and secret not in post.text
    assert secret not in client.get("/api/settings").text
    patched = client.patch("/api/settings", json={"connector": {"name": "arkham-api", "enabled": True}})
    assert secret not in patched.text


# --------------------------------------------------------------------------- paid key -> available

def test_setting_a_paid_key_flips_available_in_the_registry(client):
    from backend.app.config import get_settings
    from backend.app.connectors.registry import paid_status

    # enable bitquery (config default is disabled) -> needs-key (enabled, no key, not available)
    r = client.patch("/api/settings", json={"connector": {"name": "bitquery", "enabled": True}})
    bq = next(p for p in r.json()["connectors"]["paid"] if p["name"] == "bitquery")
    assert bq["enabled"] is True and bq["key_present"] is False
    assert bq["available"] is False and bq["status"] == "needs-key"

    # write the key -> available (and the registry that drives the orchestrator agrees)
    assert client.post("/api/settings/keys/bitquery", json={"key": "abc123"}).json() == {
        "ok": True, "connector": "bitquery", "key_present": True}
    bq2 = next(p for p in client.get("/api/settings").json()["connectors"]["paid"] if p["name"] == "bitquery")
    assert bq2["available"] is True and bq2["status"] == "available"
    reg = next(p for p in paid_status(get_settings()) if p["name"] == "bitquery")
    assert reg["available"] is True


def test_disabled_with_key_reads_as_disabled(client):
    client.post("/api/settings/keys/oklink", json={"key": "k"})  # key present but never enabled
    ok = next(p for p in client.get("/api/settings").json()["connectors"]["paid"] if p["name"] == "oklink")
    assert ok["key_present"] is True and ok["enabled"] is False
    assert ok["available"] is False and ok["status"] == "disabled"


def test_delete_key_clears_presence(client):
    client.post("/api/settings/keys/oklink", json={"key": "k"})
    paid = lambda: {p["name"]: p for p in client.get("/api/settings").json()["connectors"]["paid"]}
    assert paid()["oklink"]["key_present"] is True
    assert client.delete("/api/settings/keys/oklink").json()["key_present"] is False
    assert paid()["oklink"]["key_present"] is False


def test_key_endpoints_reject_unknown_and_empty(client):
    assert client.post("/api/settings/keys/nope", json={"key": "x"}).status_code == 400
    assert client.post("/api/settings/keys/bitquery", json={"key": "   "}).status_code == 400
    assert client.delete("/api/settings/keys/nope").status_code == 400
    assert client.patch("/api/settings",
                        json={"connector": {"name": "nope", "enabled": True}}).status_code == 400


# --------------------------------------------------------------------------- offline-first

def test_offline_refuses_fetch_but_cached_view_still_renders(client, tmp_path):
    from backend.app.connectors.base import BaseHttpConnector, OfflineError
    from backend.app.services import settings_store

    db = _seed_case(tmp_path)
    app.dependency_overrides[get_case_db_path] = lambda: str(db)
    try:
        assert client.patch("/api/settings", json={"offline": True}).json()["offline"] is True

        # a connector refuses to touch the network (no host is ever contacted)
        conn = BaseHttpConnector(base_url="https://example.invalid")
        with pytest.raises(OfflineError):
            conn.request("/anything")

        # ...but the cached view still renders (reads case.db, no network)
        v = client.get("/api/view")
        assert v.status_code == 200 and len(v.json()["nodes"]) > 0

        # ...and an expand surfaces a CLEAN offline error (not a 500)
        ex = client.post("/api/graph/expand", json={"chain": "ethereum", "address": "0x" + "a" * 40})
        assert ex.status_code == 200 and "offline" in ex.json()["error"].lower()
    finally:
        app.dependency_overrides.clear()
        settings_store.set_offline(False)


def test_offline_default_is_off_and_toggles(client):
    assert client.get("/api/settings").json()["offline"] is False
    assert client.patch("/api/settings", json={"offline": True}).json()["offline"] is True
    assert client.get("/api/settings").json()["offline"] is True
    assert client.patch("/api/settings", json={"offline": False}).json()["offline"] is False


# --------------------------------------------------------------------------- cases folder

def test_cases_folder_change_persists_and_is_used(client, tmp_path):
    from backend.app.services import cases, settings_store

    new_root = tmp_path / "custom_cases"
    r = client.patch("/api/settings", json={"cases_folder": str(new_root)})
    assert Path(r.json()["cases_folder"]) == new_root
    assert Path(client.get("/api/settings").json()["cases_folder"]) == new_root  # persisted
    assert settings_store.cases_root() == new_root

    res = cases.new_case("Folder Case")                       # a new case lands under the new folder
    assert Path(res["path"]).parent.parent == new_root


# --------------------------------------------------------------------------- keyring status

def test_keyring_status_and_plaintext_flag(client, monkeypatch):
    body = client.get("/api/settings").json()
    assert body["keyring"]["available"] is True     # in-memory backend installed
    assert body["keyring"]["plaintext_active"] is False
    assert "backend" in body["keyring"]

    monkeypatch.setenv("BIH_ALLOW_PLAINTEXT_KEYS", "1")
    assert client.get("/api/settings").json()["keyring"]["plaintext_active"] is True
