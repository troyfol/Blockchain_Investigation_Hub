"""P4 / FN-09 — the disagreements roster.

Surfaces every subject where sources DISAGREE (attribution label/category, risk category, or a movement's
valuation), each with the sources' claims side-by-side + the fields that differ. The whole point is
Invariant #4: the roster NEVER emits a winner, a consensus, or a merged/averaged value — adjudication
stays an explicit investigator finding. Read-only surfacing; no invariant is at risk.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from backend.app.db import repository as repo
from backend.app.main import app, get_case_db_path
from backend.app.models import (
    Address, Asset, Attribution, RiskAssessment, SourceQuery, Transaction, Transfer, Valuation,
)
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.disagreements import find_disagreements
from backend.tests.integration._helpers import new_case

SHARED = "0x52908400098527886e0f7030069857d2e4169ee7"  # canonical (lowercase)


def _seed_conflicts(conn) -> dict:
    """One address two sources disagree on (attribution LABEL + risk CATEGORY), plus one movement two
    sources price differently (100 vs 105.5)."""
    sq = SourceQuery(connector="multi", capability="get_attributions", endpoint="x",
                     params={"bounds": "default"}, requested_at="2026-02-01T00:00:00Z", status="ok")
    ids: dict = {}

    def w(c, sqid):
        asset = repo.upsert_asset(c, Asset(chain="ethereum", symbol="ETH", decimals=18), sqid)
        a = repo.upsert_address(c, Address(chain="ethereum", address_display=SHARED), sqid)
        b = repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + "cc" * 20), sqid)
        # two sources disagree on attribution LABEL (category agrees) ...
        repo.insert_attribution(c, Attribution(address_id=a, label="Binance Hot 14", category="exchange",
            source="arkham", confidence=0.9, retrieved_at="2026-02-01T00:00:00Z"), sqid)
        repo.insert_attribution(c, Attribution(address_id=a, label="Binance", category="exchange",
            source="misttrack", retrieved_at="2026-02-01T00:00:00Z"), sqid)
        # ... and disagree on risk CATEGORY ...
        repo.upsert_risk_assessment(c, RiskAssessment(address_id=a, score=None, score_scale=None,
            category="sanctioned", rationale="OFAC SDN", source="ofac-sdn",
            retrieved_at="2026-02-01T00:00:00Z"), sqid)
        repo.upsert_risk_assessment(c, RiskAssessment(address_id=a, score=60.0, score_scale="0-100",
            category="elevated", rationale="mixer exposure", source="chainalysis",
            retrieved_at="2026-02-01T00:00:00Z"), sqid)
        # ... and a movement priced differently by two sources.
        tx = repo.upsert_transaction(c, Transaction(chain="ethereum", tx_hash="0x" + "e" * 64,
            confirmations=100, finality_status="final"), sqid)
        mv = repo.upsert_transfer(c, Transfer(transaction_id=tx, chain="ethereum", from_address_id=a,
            to_address_id=b, asset_id=asset, amount="1000000000000000000", transfer_type="native",
            position=0), sqid)
        repo.insert_valuation(c, Valuation(subject_type="transfer", subject_id=mv, currency="USD",
            unit_price="100", value="100", price_timestamp="2026-02-01T00:00:00Z", source="defillama",
            retrieved_at="2026-02-01T00:00:00Z"), sqid)
        repo.insert_valuation(c, Valuation(subject_type="transfer", subject_id=mv, currency="USD",
            unit_price="105.5", value="105.5", price_timestamp="2026-02-01T00:00:00Z", source="arkham",
            retrieved_at="2026-02-01T00:00:00Z"), sqid)
        ids["addr"] = a
        ids["dst"] = b
        ids["mv"] = mv

    write_with_provenance(conn, sq, w)
    return ids


def test_lists_multi_source_conflicts(tmp_path):
    conn, db = new_case(tmp_path, title="Disagreements")
    ids = _seed_conflicts(conn)
    ds = find_disagreements(conn)

    assert {d["claim_type"] for d in ds} == {"attribution", "risk", "valuation"}

    attr = next(d for d in ds if d["claim_type"] == "attribution")
    assert attr["subject_id"] == ids["addr"] and attr["node_id"] == f"addr:{ids['addr']}"
    assert attr["fields"] == ["label"]                          # labels differ, categories agree
    assert set(attr["sources"]) == {"arkham", "misttrack"}
    assert {c["label"] for c in attr["claims"]} == {"Binance Hot 14", "Binance"}

    risk = next(d for d in ds if d["claim_type"] == "risk")
    assert risk["subject_id"] == ids["addr"] and risk["fields"] == ["category"]
    assert {c["category"] for c in risk["claims"]} == {"sanctioned", "elevated"}

    val = next(d for d in ds if d["claim_type"] == "valuation")
    assert val["subject_id"] == ids["mv"] and val["fields"] == ["value"]
    assert set(val["sources"]) == {"arkham", "defillama"}
    # navigation: land on the movement's DESTINATION (value recipient) — always present, EVM + UTXO alike.
    assert val["node_id"] == f"addr:{ids['dst']}"
    conn.close()


def test_never_emits_merged_value(tmp_path):
    conn, db = new_case(tmp_path, title="No merge")
    _seed_conflicts(conn)
    ds = find_disagreements(conn)
    val = next(d for d in ds if d["claim_type"] == "valuation")

    # BOTH source values present, side-by-side.
    assert {c["value"] for c in val["claims"]} == {"100", "105.5"}
    # The tool NEVER computes a winner / consensus / merged / averaged value.
    blob = json.dumps(ds)
    assert "102.75" not in blob                                # the arithmetic mean must never appear
    for forbidden in ("winner", "resolved", "consensus", "merged", "averaged", "combined"):
        assert forbidden not in blob
    # no scalar resolved "value" at the subject level — value lives ONLY inside each source's claim.
    assert "value" not in val
    conn.close()


def test_agreeing_sources_are_not_flagged(tmp_path):
    """Two sources that AGREE (same attribution label + category) are not a disagreement — the roster is
    for genuine conflicts only, not merely multi-source subjects."""
    conn, db = new_case(tmp_path, title="Agreement")
    sq = SourceQuery(connector="multi", capability="get_attributions", endpoint="x",
                     params={"bounds": "default"}, requested_at="2026-02-01T00:00:00Z", status="ok")

    def w(c, sqid):
        a = repo.upsert_address(c, Address(chain="ethereum", address_display=SHARED), sqid)
        repo.insert_attribution(c, Attribution(address_id=a, label="Binance", category="exchange",
            source="arkham", retrieved_at="2026-02-01T00:00:00Z"), sqid)
        repo.insert_attribution(c, Attribution(address_id=a, label="Binance", category="exchange",
            source="misttrack", retrieved_at="2026-02-01T00:00:00Z"), sqid)

    write_with_provenance(conn, sq, w)
    assert find_disagreements(conn) == []
    conn.close()


def test_endpoint_lists_disagreements(tmp_path):
    conn, db = new_case(tmp_path, title="Endpoint")
    _seed_conflicts(conn)
    conn.close()
    app.dependency_overrides[get_case_db_path] = lambda: str(db)
    try:
        body = TestClient(app).get("/api/disagreements").json()
    finally:
        app.dependency_overrides.clear()
    assert {d["claim_type"] for d in body["disagreements"]} == {"attribution", "risk", "valuation"}
