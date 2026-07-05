"""P39 — the bundled first-run sample case ('Explore the sample case').

Source-mode coverage of the import logic + the two endpoints. The FROZEN-bundle path (does the sample
ride along inside the exe and open there?) is proven separately by ``scripts/frozen_smoke.py`` against
the built app. ``BIH_CASES_ROOT`` / ``BIH_APP_DATA_DIR`` are sandboxed to a tmp dir so the sample
extracts there, never into the repo ``cases/``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("BIH_CASES_ROOT", str(tmp_path / "cases_root"))
    monkeypatch.setenv("BIH_APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.delenv("BIH_CASE_DB", raising=False)
    from backend.app.services import cases

    cases.clear_active_case()
    yield
    cases.clear_active_case()


def test_sample_casefile_present_in_source(sandbox):
    """The sample resolves through resource_path() in source mode (repo examples/) — a fast path check
    with no extraction."""
    from backend.app.services import cases

    p = cases.sample_casefile_path()
    assert p is not None and p.exists() and p.suffix == ".casefile"


def test_sample_endpoints_offer_and_open(sandbox):
    """GET /api/cases/sample advertises availability; POST /api/cases/import-sample verifies CLEAN (the
    app's own bundle is never a tamper/audit failure), opens it, and the opened case reads back through
    /api/graph with real nodes. One extraction exercises the service fn + both routes + the graph read."""
    from backend.app.main import app

    with TestClient(app) as c:
        avail = c.get("/api/cases/sample")
        assert avail.status_code == 200 and avail.json()["available"] is True

        imp = c.post("/api/cases/import-sample")
        assert imp.status_code == 200, imp.text
        body = imp.json()
        assert body["opened"] is True, body
        assert body["verification"]["ok"] is True, body   # our own bundle verifies clean (no untrusted path)
        assert body["trusted"] is True
        assert body["active"] is not None

        g = c.get("/api/graph")
        assert g.status_code == 200
        assert len(g.json().get("nodes", [])) > 0          # the Tornado sample has real graph data
