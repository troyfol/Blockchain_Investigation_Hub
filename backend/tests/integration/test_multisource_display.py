"""Multi-source display (phase_07): two sources side-by-side, never collapsed; screenshot exhibit."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from backend.app.audits.runner import run_audits
from backend.app.db import repository as repo
from backend.app.models import Address, Attribution, RiskAssessment, SourceQuery
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.claims_display import address_claims
from backend.app.services.exhibits import attach_screenshot
from backend.tests.integration._helpers import new_case

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "imports"
SHARED = "0x52908400098527886e0f7030069857d2e4169ee7"  # canonical (lowercase) shared address


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="Multi-source")
    yield conn, db
    conn.close()


def _seed_arkham_api_attribution(conn):
    """Arkham *attribution* comes from the Arkham API (Path B), NOT the UI transfer export — represent
    it directly so the display test still proves two sources side-by-side (see the reconciliation note)."""
    sq = SourceQuery(connector="arkham-api", capability="get_attributions", endpoint="entity",
                     params={"address": SHARED, "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")

    def w(c, sqid):
        aid = repo.upsert_address(c, Address(chain="ethereum", address_display=SHARED), sqid)
        repo.insert_attribution(c, Attribution(
            address_id=aid, label="Binance Hot 14", category="exchange", source="arkham",
            confidence=0.95, retrieved_at="2026-01-01T00:00:00Z"), sqid)

    write_with_provenance(conn, sq, w)


def _seed_misttrack_claims(conn):
    """MisTrack risk + attribution come from its API (`connectors/misttrack.py`); seed them directly so
    the side-by-side display test stays independent of any live key (the CSV importer was retired —
    docs/findings/misttrack_reconciliation.md)."""
    sq = SourceQuery(connector="misttrack-api", capability="get_risk", endpoint="risk_score",
                     params={"address": SHARED, "coin": "ETH", "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")

    def w(c, sqid):
        aid = repo.upsert_address(c, Address(chain="ethereum", address_display=SHARED), sqid)
        repo.upsert_risk_assessment(c, RiskAssessment(
            address_id=aid, score=85.0, score_scale="3-100", category="mixer",
            rationale="High — mixer exposure", source="misttrack",
            retrieved_at="2026-01-01T00:00:00Z"), sqid)
        repo.upsert_attribution(c, Attribution(
            address_id=aid, label="Binance", category="exchange", source="misttrack",
            retrieved_at="2026-01-01T00:00:00Z"), sqid)

    write_with_provenance(conn, sq, w)


@pytest.mark.smoke
def test_two_sources_side_by_side_never_collapsed(case):
    conn, db = case
    _seed_arkham_api_attribution(conn)                       # arkham (API / Path B)
    _seed_misttrack_claims(conn)                             # misttrack (API)

    addr = conn.execute("SELECT id FROM address WHERE address=?", (SHARED,)).fetchone()["id"]
    d = address_claims(conn, addr)
    # Both sources present, side-by-side; risk from misttrack.
    assert set(d["attributions_by_source"]) == {"arkham", "misttrack"}
    assert set(d["risks_by_source"]) == {"misttrack"}
    # NO averaged/combined score anywhere — the never-collapse principle.
    assert "combined" not in d and "averaged" not in d
    synthetic = {"combined", "averaged", "synthetic", "aggregate", "merged"}
    used = {r[0] for r in conn.execute(
        "SELECT source FROM attribution UNION SELECT source FROM risk_assessment").fetchall()}
    assert not (used & synthetic)
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_screenshot_stored_as_hashed_exhibit(case):
    conn, db = case
    eid = attach_screenshot(conn, file_path=FIX / "screenshot.png", source="arkham",
                            description="Arkham risk panel (visual only)")
    ex = conn.execute("SELECT * FROM exhibit WHERE id=?", (eid,)).fetchone()
    assert ex["exhibit_type"] == "screenshot" and ex["content_hash"]
    stored = db.parent / ex["file_ref"]
    assert stored.exists()
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == ex["content_hash"]
    assert all(r.passed for r in run_audits(db_path=str(db)))
