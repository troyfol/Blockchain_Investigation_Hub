"""Chainalysis free sanctions API — contract tests (Phase C; docs/connectors.md §6).

A second free sanctions source, stored SIDE-BY-SIDE with OFAC, never merged (Invariant #4). Responses
are mocked (no key at build to record a live cassette — the shape follows the documented API; the
RUN_LIVE drift test in test_live_drift.py is the refresh path). TODO: confirm field names live.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.app.audits.runner import run_audits
from backend.app.connectors.base import ConnectorError, RateLimiter
from backend.app.connectors.chainalysis import ChainalysisSanctionsConnector
from backend.app.connectors.imports.ofac import OfacSdnImporter
from backend.tests.integration._helpers import new_case

SANCTIONED = "0x8589427373D6D84E98730D7795D8f6f8731FDA16"  # also OFAC's DOE address (for side-by-side)
CANON = SANCTIONED.lower()
BASE = "https://public.chainalysis.com/api/v1"
SANCTIONED_BODY = {"identifications": [
    {"category": "sanctions", "name": "SDN: SOME ENTITY", "description": "OFAC SDN listed", "url": "https://x"}]}


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="Chainalysis")
    yield conn, db
    conn.close()


def _conn():
    return ChainalysisSanctionsConnector(api_key="test-key",
                                         rate_limiter=RateLimiter(0, enabled=False), sleep=lambda _s: None)


@respx.mock
def test_sanctioned_address_writes_categorical_risk(case):
    conn, db = case
    respx.get(f"{BASE}/address/{CANON}").mock(return_value=httpx.Response(200, json=SANCTIONED_BODY))
    c = _conn()
    res = c.get_risk(conn, "ethereum", SANCTIONED)
    c.close()

    assert res["risks"] == 1 and res["sanctioned"] is True
    row = conn.execute(
        """SELECT r.score, r.score_scale, r.category, r.rationale, r.source, a.address
           FROM risk_assessment r JOIN address a ON a.id=r.address_id""").fetchone()
    assert row["source"] == "chainalysis-sanctions" and row["category"] == "sanctions"
    assert row["score"] is None and row["score_scale"] is None  # categorical only
    assert row["address"] == CANON  # canonicalized (lowercased)
    assert "SDN: SOME ENTITY" in row["rationale"] and "OFAC SDN listed" in row["rationale"]
    assert all(r.passed for r in run_audits(db_path=str(db)))


@respx.mock
def test_clean_address_records_check_but_no_risk(case):
    conn, db = case
    respx.get(f"{BASE}/address/{CANON}").mock(return_value=httpx.Response(200, json={"identifications": []}))
    c = _conn()
    res = c.get_risk(conn, "ethereum", SANCTIONED)
    c.close()
    assert res["risks"] == 0 and res["sanctioned"] is False
    assert conn.execute("SELECT COUNT(*) FROM risk_assessment").fetchone()[0] == 0
    # The negative screening is still recorded as provenance (we checked; clean as of now).
    assert conn.execute(
        "SELECT COUNT(*) FROM source_query WHERE connector='chainalysis-sanctions'").fetchone()[0] == 1
    assert all(r.passed for r in run_audits(db_path=str(db)))


@respx.mock
def test_ofac_and_chainalysis_stored_side_by_side(case):
    """Inv #4: two sanctions sources on the SAME address are kept side-by-side, never merged."""
    conn, db = case
    from pathlib import Path
    FIX = Path(__file__).resolve().parent.parent / "fixtures" / "imports"
    OfacSdnImporter().get_risk(conn, FIX / "ofac_sdn.xml")  # writes ofac-sdn for the DOE (0x8589…) addr
    respx.get(f"{BASE}/address/{CANON}").mock(return_value=httpx.Response(200, json=SANCTIONED_BODY))
    c = _conn()
    c.get_risk(conn, "ethereum", SANCTIONED)
    c.close()

    rows = conn.execute(
        """SELECT r.source FROM risk_assessment r JOIN address a ON a.id=r.address_id
           WHERE a.address=?""", (CANON,)).fetchall()
    sources = sorted(r["source"] for r in rows)
    assert sources == ["chainalysis-sanctions", "ofac-sdn"]  # both present, distinct, not merged
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_missing_key_raises_clean_error(case):
    conn, db = case
    c = ChainalysisSanctionsConnector(api_key="")  # no key
    with pytest.raises(ConnectorError) as exc:
        c.get_risk(conn, "ethereum", SANCTIONED)
    c.close()
    assert "key not set" in str(exc.value).lower()


@respx.mock
def test_rescreen_is_idempotent(case):
    conn, db = case
    respx.get(f"{BASE}/address/{CANON}").mock(return_value=httpx.Response(200, json=SANCTIONED_BODY))
    c = _conn()
    c.get_risk(conn, "ethereum", SANCTIONED)
    c.get_risk(conn, "ethereum", SANCTIONED)  # re-screen the same address (Invariant #7)
    c.close()
    assert conn.execute(
        "SELECT COUNT(*) FROM risk_assessment WHERE source='chainalysis-sanctions'").fetchone()[0] == 1
    assert all(r.passed for r in run_audits(db_path=str(db)))


@respx.mock
def test_multiple_identifications_side_by_side_and_category_fallback(case):
    """N identifications -> N distinct risk rows (Inv #4, never collapsed); an identification with no
    `category` falls back to 'sanctioned' (the documented degrade-not-crash contract)."""
    conn, db = case
    body = {"identifications": [
        {"category": "sanctions", "name": "A", "description": "d1"},
        {"name": "B", "description": "d2"},               # no category -> fallback 'sanctioned'
        {"category": "pep", "name": "C", "description": "d3"}]}
    respx.get(f"{BASE}/address/{CANON}").mock(return_value=httpx.Response(200, json=body))
    c = _conn()
    res = c.get_risk(conn, "ethereum", SANCTIONED)
    c.close()
    assert res["risks"] == 3
    cats = {r["category"] for r in conn.execute(
        "SELECT category FROM risk_assessment WHERE source='chainalysis-sanctions'").fetchall()}
    assert cats == {"sanctions", "sanctioned", "pep"}  # 3 distinct; missing-category row -> 'sanctioned'
    assert all(r.passed for r in run_audits(db_path=str(db)))


@respx.mock
def test_non_dict_body_fails_clean(case):
    conn, db = case
    respx.get(f"{BASE}/address/{CANON}").mock(return_value=httpx.Response(200, json=[]))  # list, not dict
    c = _conn()
    with pytest.raises(ConnectorError) as exc:
        c.get_risk(conn, "ethereum", SANCTIONED)
    c.close()
    assert "unexpected body shape" in str(exc.value)
    assert conn.execute("SELECT COUNT(*) FROM source_query").fetchone()[0] == 0  # nothing written


@respx.mock
def test_upstream_error_writes_nothing(case):
    """A persistent upstream 5xx (retries exhausted) surfaces as a clean ConnectorError and leaves NO
    partial risk row or source_query (failure precedes the provenance write)."""
    conn, db = case
    respx.get(f"{BASE}/address/{CANON}").mock(return_value=httpx.Response(500))
    c = _conn()  # no-sleep, so retries don't block
    with pytest.raises(ConnectorError):
        c.get_risk(conn, "ethereum", SANCTIONED)
    c.close()
    for table in ("risk_assessment", "source_query"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
