"""Scalable focused VIEW (services/graph_view.py) + read-model valuation (services/graph.py).

Seeds a dense node: a focus address with a few SIGNIFICANT (valued) inbound transfers and many DUST
(unvalued) ones, then asserts the bounded view focuses + caps + collapses dust into an expandable
aggregate, surfaces USD value-at-time, and never writes a fact (the view is display-only — Inv #5).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.audits.runner import run_audits
from backend.app.db import get_connection
from backend.app.db import repository as repo
from backend.app.main import app, get_case_db_path
from backend.app.models import Address, Asset, SourceQuery, Transaction, Transfer, Valuation
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.graph import build_graph
from backend.app.services.graph_view import build_view
from backend.tests.integration._helpers import new_case


def _seed_dense(conn, *, n_sig=3, n_dust=20):
    """A focus address receiving n_sig valued transfers (>= $1) and n_dust unvalued (dust) ones."""
    sq = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="txlist",
                     params={"address": "focus", "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")
    ids: dict = {"sig": [], "dust": []}

    def write(c, sqid):
        asset = repo.upsert_asset(c, Asset(chain="ethereum", symbol="ETH", decimals=18), sqid)
        focus = repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + "f" * 40), sqid)
        tx = repo.upsert_transaction(c, Transaction(
            chain="ethereum", tx_hash="0x" + "a" * 64, block_ts="2026-01-01T00:00:00Z",
            confirmations=100, finality_status="final"), sqid)
        ids.update(focus=focus, asset=asset, tx=tx)
        for i in range(n_sig):
            cp = repo.upsert_address(c, Address(chain="ethereum", address_display=f"0x{i:02d}" + "1" * 38), sqid)
            tr = repo.upsert_transfer(c, Transfer(transaction_id=tx, chain="ethereum", from_address_id=cp,
                to_address_id=focus, asset_id=asset, amount=str(10 ** 18), transfer_type="native",
                position=i), sqid)
            ids["sig"].append((cp, tr))
        for i in range(n_dust):
            cp = repo.upsert_address(c, Address(chain="ethereum", address_display=f"0x{i:02d}" + "2" * 38), sqid)
            tr = repo.upsert_transfer(c, Transfer(transaction_id=tx, chain="ethereum", from_address_id=cp,
                to_address_id=focus, asset_id=asset, amount=str(10 ** 12), transfer_type="native",
                position=100 + i), sqid)
            ids["dust"].append((cp, tr))

    write_with_provenance(conn, sq, write)

    # Value ONLY the significant transfers ($2000 each); dust is left unvalued (an honest gap -> dust).
    sq2 = SourceQuery(connector="defillama", capability="get_price", endpoint="prices/historical",
                      params={}, requested_at="2026-01-01T00:00:00Z", status="ok")

    def write2(c, sqid):
        for _cp, tr in ids["sig"]:
            repo.insert_valuation(c, Valuation(
                subject_type="transfer", subject_id=tr, currency="USD", unit_price="2000", value="2000",
                price_timestamp="2026-01-01T00:00:00Z", confidence=0.9, source="defillama",
                retrieved_at="2026-01-01T00:00:00Z"), sqid)

    write_with_provenance(conn, sq2, write2)
    return ids


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="Dense")
    ids = _seed_dense(conn)
    yield conn, db, ids
    conn.close()


# --- read-model valuation -----------------------------------------------------------------

def test_read_model_surfaces_usd_value_and_node_summary(case):
    conn, _db, ids = case
    g = build_graph(conn)
    fnid = f"addr:{ids['focus']}"
    fnode = next(n for n in g["nodes"] if n["id"] == fnid)
    # value summary: USD only counts the valued (significant) transfers; native counts all ETH.
    assert fnode["val"]["in_usd"] == pytest.approx(3 * 2000.0)
    assert fnode["val"]["native_symbol"] == "ETH"
    assert fnode["val"]["in_native"] == pytest.approx(3 + 20 * 1e-6)

    valued = [e for e in g["edges"] if e.get("value_usd")]
    no_price = [e for e in g["edges"] if e.get("no_price")]
    assert len(valued) == 3 and all(e["value_usd"] == 2000.0 for e in valued)
    assert all(e["value_usd_label"].startswith("$") for e in valued)
    assert len(no_price) == 20            # the dust carries an honest no-price gap, never a $0


# --- focused / aggregated view ------------------------------------------------------------

def test_view_focuses_caps_and_aggregates_dust(case):
    conn, _db, ids = case
    fnid = f"addr:{ids['focus']}"
    v = build_view(conn, focus=fnid, group_dust=True, dust_floor_usd=1.0)
    kinds = {k: sum(1 for n in v["nodes"] if n["kind"] == k) for k in {n["kind"] for n in v["nodes"]}}
    # focus + 3 significant addresses kept; the 20 dust collapse to ONE aggregate node.
    assert kinds.get("aggregate") == 1
    assert kinds.get("address") == 4                      # focus + 3 significant
    agg = next(n for n in v["nodes"] if n["kind"] == "aggregate")
    assert agg["count"] == 20 and agg["agg_direction"] == "in"
    assert agg["no_price_count"] == 20 and agg["underlying"]   # provenance pointer to the real underlying
    assert v["meta"]["bounded"] is True and v["meta"]["aggregated"] == 1
    assert v["meta"]["total"] >= 24                       # full graph is bigger than the displayed view


def test_view_expand_reveals_the_real_underlying(case):
    conn, _db, ids = case
    fnid = f"addr:{ids['focus']}"
    v = build_view(conn, focus=fnid, group_dust=True)
    aggid = next(n["id"] for n in v["nodes"] if n["kind"] == "aggregate")
    expanded = build_view(conn, focus=fnid, group_dust=True, node_cap=400, expand=(aggid,))
    assert not any(n["kind"] == "aggregate" for n in expanded["nodes"])   # the bundle is gone
    assert sum(1 for n in expanded["nodes"] if n["kind"] == "address") == 24  # focus + 3 + 20 real


def test_view_node_cap_bounds_significant_too(case):
    conn, _db, ids = case
    fnid = f"addr:{ids['focus']}"
    v = build_view(conn, focus=fnid, node_cap=2, group_dust=True)   # focus + 1 significant fits
    assert sum(1 for n in v["nodes"] if n["kind"] == "address") <= 2
    assert v["meta"]["bounded"] is True


def test_view_value_floor_and_flagged_filters(case):
    conn, _db, ids = case
    fnid = f"addr:{ids['focus']}"
    # value floor above the significant value -> everything becomes dust (nothing clears $5000).
    v = build_view(conn, focus=fnid, value_floor_usd=5000.0)
    assert sum(1 for n in v["nodes"] if n["kind"] == "address") == 1   # only the focus
    # only_flagged with no flagged counterparties -> all collapse to dust as well.
    v2 = build_view(conn, focus=fnid, only_flagged=True)
    assert sum(1 for n in v2["nodes"] if n["kind"] == "address") == 1


def test_view_is_display_only_writes_no_facts(case):
    """The view must NEVER write a row — it is display-only over the real facts (Invariant #5)."""
    conn, db, ids = case
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    fnid = f"addr:{ids['focus']}"
    for kw in ({}, {"hops": 2}, {"group_dust": False}, {"node_cap": 3}, {"value_floor_usd": 10}):
        build_view(conn, focus=fnid, **kw)
    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    assert before == after                                 # not a single row added/changed
    assert all(r.passed for r in run_audits(db_path=str(db)))


# --- endpoints ----------------------------------------------------------------------------

def test_view_and_summary_endpoints(case):
    conn, db, ids = case
    app.dependency_overrides[get_case_db_path] = lambda: str(db)
    try:
        c = TestClient(app)
        fnid = f"addr:{ids['focus']}"
        v = c.get("/api/view", params={"focus": fnid, "group_dust": "true"}).json()
        assert v["meta"]["bounded"] is True
        assert any(n["kind"] == "aggregate" for n in v["nodes"])

        s = c.get(f"/api/node/{fnid}/summary").json()
        assert s["val"]["in_usd"] == pytest.approx(6000.0)
        assert s["counterparty_total"] == 23                 # 3 significant + 20 dust
        assert s["counterparties"][0]["usd"] == pytest.approx(2000.0)   # ranked by USD
        assert c.get("/api/node/addr:ghost/summary").status_code == 404
    finally:
        app.dependency_overrides.clear()
