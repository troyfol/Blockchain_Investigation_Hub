"""P6 / FN-03 — per-source valuation summary (side-by-side, never collapsed).

When >1 source priced a movement, the summary returns EVERY source's valuation side-by-side (Invariant
#4) — never one merged/averaged number. This powers the SidePanel's per-source value stack on a contested
movement (replacing the old single number + "see node detail" hint). Read-only; no invariant at risk.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.app.db import repository as repo
from backend.app.main import app, get_case_db_path
from backend.app.models import Address, Asset, SourceQuery, Transaction, Transfer, Valuation
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.valuation_display import movement_valuations
from backend.tests.integration._helpers import new_case


def _seed_movement(conn, valuations):
    """Seed one native ETH transfer + the given (source, value, confidence) valuations on it. Returns the
    movement (transfer) id."""
    sq = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="txlist",
                     params={"address": "seed"}, requested_at="2026-01-01T00:00:00Z", status="ok")
    ids: dict = {}

    def w(c, sqid):
        asset = repo.upsert_asset(c, Asset(chain="ethereum", symbol="ETH", decimals=18), sqid)
        a = repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + "ab" * 20), sqid)
        b = repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + "cd" * 20), sqid)
        tx = repo.upsert_transaction(c, Transaction(chain="ethereum", tx_hash="0x" + "e" * 64,
            block_ts="2026-01-01T00:00:00Z", confirmations=100, finality_status="final"), sqid)
        mv = repo.upsert_transfer(c, Transfer(transaction_id=tx, chain="ethereum", from_address_id=a,
            to_address_id=b, asset_id=asset, amount="1000000000000000000", transfer_type="native",
            position=0), sqid)
        for source, value, confidence in valuations:
            repo.insert_valuation(c, Valuation(subject_type="transfer", subject_id=mv, currency="USD",
                unit_price=value, value=value, price_timestamp="2026-01-01T00:00:00Z", source=source,
                confidence=confidence, retrieved_at="2026-02-01T00:00:00Z"), sqid)
        ids["mv"] = mv

    write_with_provenance(conn, sq, w)
    return ids["mv"]


def test_summary_returns_all_valuations_per_source(tmp_path):
    conn, db = new_case(tmp_path, title="Valuation side-by-side")
    mv = _seed_movement(conn, [("defillama", "2000", 0.99), ("arkham", "2050", 0.8)])

    d = movement_valuations(conn, mv)
    assert d["subject_id"] == mv
    assert d["contested"] is True
    assert set(d["valuations_by_source"]) == {"defillama", "arkham"}
    assert d["valuations_by_source"]["defillama"][0]["value"] == "2000"
    assert d["valuations_by_source"]["arkham"][0]["value"] == "2050"
    # NEVER a merged/averaged value — exactly the two source values, side-by-side (Invariant #4).
    all_values = sorted(v["value"] for lst in d["valuations_by_source"].values() for v in lst)
    assert all_values == ["2000", "2050"]  # 2025 (the mean) is never synthesized
    for forbidden in ("value", "combined", "averaged", "merged", "winner", "consensus"):
        assert forbidden not in d  # no collapsed/averaged/winner value at the summary level
    conn.close()


def test_single_source_movement_not_contested(tmp_path):
    """A movement priced by ONE source is not contested — the UI keeps its single-value display."""
    conn, db = new_case(tmp_path, title="Single source")
    mv = _seed_movement(conn, [("defillama", "2000", 0.99)])
    d = movement_valuations(conn, mv)
    assert d["contested"] is False
    assert set(d["valuations_by_source"]) == {"defillama"}
    conn.close()


def test_unvalued_movement_is_empty(tmp_path):
    conn, db = new_case(tmp_path, title="Unvalued")
    mv = _seed_movement(conn, [])  # no valuations
    d = movement_valuations(conn, mv)
    assert d["valuations_by_source"] == {} and d["contested"] is False
    conn.close()


def test_endpoint_returns_valuations(tmp_path):
    conn, db = new_case(tmp_path, title="Endpoint")
    mv = _seed_movement(conn, [("defillama", "2000", 0.99), ("arkham", "2050", 0.8)])
    conn.close()
    app.dependency_overrides[get_case_db_path] = lambda: str(db)
    try:
        body = TestClient(app).get(f"/api/movement/{mv}/valuations").json()
    finally:
        app.dependency_overrides.clear()
    assert body["contested"] is True
    assert set(body["valuations_by_source"]) == {"defillama", "arkham"}
