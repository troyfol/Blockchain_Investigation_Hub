"""Harness smoketest (`make smoke`).

Proves the scaffold + schema are wired end-to-end: a fresh DB migrates the full schema, the
audit runner is green on an empty migrated case, and the FastAPI app answers /health. The rich
seeded-case audits live in test_seeded_case.py.
"""

from __future__ import annotations

import keyring
import pytest
from fastapi.testclient import TestClient
from keyring.backend import KeyringBackend

from backend.app.audits.runner import run_audits
from backend.app.db import apply_migrations, read_schema_version
from backend.app.main import app


class _MemoryKeyring(KeyringBackend):
    """In-memory keyring so the health smoke never reads the operator's REAL keys (a configured paid
    connector on the dev machine must not flip this test red — it asserts the DEFAULT state)."""

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


@pytest.fixture
def _default_env(tmp_path, monkeypatch):
    """Isolate the per-OS app-data dir + the keyring so /health reports the DEFAULT connector state
    regardless of what the operator has configured on this machine (mirrors test_settings._isolate; the
    same latent bug P6 fixed for test_paid_registry). Never touches the real settings.json / keyring."""
    monkeypatch.setenv("BIH_APP_DATA_DIR", str(tmp_path / "appdata"))
    prev = keyring.get_keyring()
    keyring.set_keyring(_MemoryKeyring())
    try:
        yield
    finally:
        keyring.set_keyring(prev)


@pytest.mark.smoke
def test_fresh_db_migrates_full_schema(tmp_path):
    db_path = tmp_path / "case.db"
    applied = apply_migrations(db_path)
    assert applied == 14  # 0001..0014 (0014 = P27/FN-19 audit_baseline anchor)
    # No case initialized yet -> no case_meta row -> schema_version reads 0.
    assert read_schema_version(db_path) == 0


@pytest.mark.smoke
def test_audit_runner_green_on_empty_migrated_db(tmp_path):
    db_path = tmp_path / "case.db"
    apply_migrations(db_path)
    results = run_audits(db_path=str(db_path))
    assert results, "expected real checks to be registered by Phase 1"
    assert all(r.passed for r in results)  # empty but valid schema -> every check passes


@pytest.mark.smoke
def test_health_endpoint(_default_env):
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["finality_thresholds"]["bitcoin"] == 6
    # Optional paid sources are reported and all OFF by default (selectable only when enabled + keyed).
    paid = {p["name"]: p for p in body["paid_connectors"]}
    assert set(paid) == {"bitquery", "arkham-api", "misttrack-api", "oklink"}
    assert all(not p["available"] for p in paid.values())
