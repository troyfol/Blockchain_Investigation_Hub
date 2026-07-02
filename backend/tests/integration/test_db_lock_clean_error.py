"""Batch 5 (RES-02): a write that loses the WAL ``busy_timeout`` race raises
``sqlite3.OperationalError: database is locked``. No handler caught it, so it reached Starlette's default
500 logger. It must now surface as a clean, retryable 503 (never a raw 500 + traceback)."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.services import cases, investigator


@pytest.fixture
def active_case(tmp_path, monkeypatch):
    monkeypatch.setenv("BIH_APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("BIH_CASES_ROOT", str(tmp_path / "cases_root"))
    monkeypatch.delenv("BIH_CASE_DB", raising=False)
    cases.clear_active_case()
    cases.new_case("Lock Test")  # creates + activates a migrated case
    yield
    cases.clear_active_case()


def test_database_locked_returns_503_not_500(active_case, monkeypatch):
    # Simulate the lost busy_timeout race: the write raises `database is locked` inside the endpoint.
    def _locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(investigator, "add_annotation", _locked)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/target/address/some-id/annotations", json={"content": "note"})
    assert r.status_code == 503, "a locked-db write must be a clean 503, not a 500 (RES-02)"
    assert r.headers.get("Retry-After")
    body = r.json()
    assert "busy" in body["detail"].lower()


def test_other_operational_error_is_generic_500(active_case, monkeypatch):
    def _boom(*a, **k):
        raise sqlite3.OperationalError("no such table: nope")

    monkeypatch.setattr(investigator, "add_annotation", _boom)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/target/address/some-id/annotations", json={"content": "note"})
    assert r.status_code == 500
    assert "no such table" not in r.text  # sanitized — no raw sqlite message leaked
