"""P1 / FN-01 — the source-query provenance drill-through endpoint.

The product's promise is that every displayed fact/claim traces to the exact query that produced it
(Invariant #3). `GET /api/source_query/{id}` returns that record (connector, capability, endpoint,
params/bounds, retrieval time, raw-response hash), and the claims payload carries each claim's
`source_query_id` so the UI can drill through. Read-only — no invariant is at risk.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.db import apply_migrations, get_connection
from backend.app.db import repository as repo
from backend.app.main import app, get_case_db_path
from backend.app.models import (
    Address, Attribution, Entity, EntityMembership, RiskAssessment, SourceQuery,
)
from backend.app.provenance.atomic import write_with_provenance

ADDR = "0x52908400098527886e0f7030069857d2e4169ee7"  # canonical (lowercase)


def _seed(tmp_path):
    db = tmp_path / "case.db"
    apply_migrations(db)
    conn = get_connection(db)
    repo.init_case(conn, title="Provenance")
    sq = SourceQuery(connector="graphsense", capability="get_attributions", endpoint="tagpack",
                     params={"address": ADDR, "bounds": "default"},
                     requested_at="2026-02-03T00:00:00Z", status="ok",
                     result_summary="1 attribution")

    def w(c, sqid):
        aid = repo.upsert_address(c, Address(chain="ethereum", address_display=ADDR), sqid)
        repo.insert_attribution(c, Attribution(
            address_id=aid, label="Tornado Cash", category="mixing_service", source="graphsense",
            confidence=0.6, retrieved_at="2026-02-03T00:00:00Z"), sqid)
        repo.upsert_risk_assessment(c, RiskAssessment(
            address_id=aid, score=None, score_scale=None, category="sanctioned",
            rationale="OFAC SDN", source="ofac-sdn", retrieved_at="2026-02-03T00:00:00Z"), sqid)
        ent_id = repo.insert_entity(c, Entity(
            name="Tornado Cash", entity_type="mixing_service", origin="source"))
        repo.insert_entity_membership(c, EntityMembership(
            entity_id=ent_id, address_id=aid, source="graphsense", method="shared-label"), sqid)

    sqid, _ = write_with_provenance(conn, sq, w, raw_response=b'{"ok":true}')
    addr_id = conn.execute("SELECT id FROM address WHERE address=?", (ADDR,)).fetchone()["id"]
    conn.close()
    return db, sqid, addr_id


@pytest.fixture
def client(tmp_path):
    db, sqid, addr_id = _seed(tmp_path)
    app.dependency_overrides[get_case_db_path] = lambda: str(db)
    yield TestClient(app), sqid, addr_id
    app.dependency_overrides.clear()


def test_returns_full_provenance(client):
    c, sqid, _ = client
    r = c.get(f"/api/source_query/{sqid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == sqid
    assert body["connector"] == "graphsense"
    assert body["capability"] == "get_attributions"
    assert body["endpoint"] == "tagpack"
    assert body["params"] == {"address": ADDR, "bounds": "default"}   # JSON round-trips to a dict
    assert body["requested_at"] == "2026-02-03T00:00:00Z"
    assert body["status"] == "ok"
    assert body["result_summary"] == "1 attribution"
    # the raw response was hashed on write — the drill-through exposes it for tamper-checking
    assert body["raw_response_hash"] and len(body["raw_response_hash"]) == 64


def test_unknown_source_query_returns_404(client):
    c, _, _ = client
    assert c.get("/api/source_query/does-not-exist").status_code == 404


def test_claims_payload_carries_source_query_id(client):
    """FN-01 acceptance: every claim in the payload carries the id of the query that produced it, so the
    UI can drill through to its provenance. (Holds today via `SELECT *`; this guards it.)"""
    c, sqid, addr_id = client
    d = c.get(f"/api/address/{addr_id}/claims").json()
    claims = [a for lst in d["attributions_by_source"].values() for a in lst] + \
             [r for lst in d["risks_by_source"].values() for r in lst] + \
             list(d["entities"])
    assert d["entities"], "expected a seeded entity membership"
    assert claims, "expected seeded claims"
    for claim in claims:
        assert claim["source_query_id"] == sqid
