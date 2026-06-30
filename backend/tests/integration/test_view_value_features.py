"""P3.5 view value-model: sequence key (read-model), the user_dust value filter, and per-view
value-driven edge thickness (services/graph.py + graph_view.py).

All three are DISPLAY-ONLY over real facts (Inv #5) — a regression here also re-checks that the view
writes no row. A focus address receives a spread of priced inbound transfers (each its own tx, so each
has a distinct block_height), one unpriced transfer, and one mempool (NULL-height) transfer.
"""

from __future__ import annotations

import pytest

from backend.app.audits.runner import run_audits
from backend.app.db import repository as repo
from backend.app.models import Address, Asset, SourceQuery, Transaction, Transfer, Valuation
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.graph import build_graph, scale_edge_widths
from backend.app.services.graph_view import build_view
from backend.app.theme import dimension
from backend.tests.integration._helpers import new_case

# name -> (block_height, amount_wei, usd_or_None). Distinct heights => distinct sequence keys.
_SPECS = [
    ("big", 100, 5 * 10**18, "5000"),
    ("mid", 200, 10**18, "50"),
    ("small", 300, 10**17, "20"),
    ("noprice", 400, 2 * 10**18, None),     # unpriced — honest gap, never swept by the value filter
    ("mempool", None, 10**18, "30"),        # NULL block_height — sequence key MISSING (-> tray)
]


def _seed(conn):
    sq = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="txlist",
                     params={"address": "focus", "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")
    ids: dict = {"tr": {}}

    def write(c, sqid):
        asset = repo.upsert_asset(c, Asset(chain="ethereum", symbol="ETH", decimals=18), sqid)
        focus = repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + "f" * 40), sqid)
        ids.update(focus=focus, asset=asset)
        for idx, (name, height, amount, _usd) in enumerate(_SPECS, start=1):
            cp = repo.upsert_address(c, Address(
                chain="ethereum", address_display="0x" + format(idx, "040x")), sqid)
            tx = repo.upsert_transaction(c, Transaction(
                chain="ethereum", tx_hash="0x" + format(idx, "064x"), block_height=height,
                block_ts="2026-01-01T00:00:00Z", confirmations=(0 if height is None else 100),
                finality_status=("provisional" if height is None else "final")), sqid)
            tr = repo.upsert_transfer(c, Transfer(
                transaction_id=tx, chain="ethereum", from_address_id=cp, to_address_id=focus,
                asset_id=asset, amount=str(amount), transfer_type="native", position=0), sqid)
            ids["tr"][name] = {"cp": cp, "tr": tr, "tx": tx, "height": height}

    write_with_provenance(conn, sq, write)

    sq2 = SourceQuery(connector="defillama", capability="get_price", endpoint="prices/historical",
                      params={}, requested_at="2026-01-01T00:00:00Z", status="ok")

    def write2(c, sqid):
        for name, _h, _a, usd in _SPECS:
            if usd is None:
                continue
            repo.insert_valuation(c, Valuation(
                subject_type="transfer", subject_id=ids["tr"][name]["tr"], currency="USD",
                unit_price=usd, value=usd, price_timestamp="2026-01-01T00:00:00Z", confidence=0.9,
                source="defillama", retrieved_at="2026-01-01T00:00:00Z"), sqid)

    write_with_provenance(conn, sq2, write2)
    return ids


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="ValueFeatures")
    ids = _seed(conn)
    yield conn, db, ids
    conn.close()


def _edges_by_cp(g: dict, ids: dict) -> dict:
    """Map spec name -> its transfer edge (the edge whose source is that counterparty address)."""
    cp_node = {name: f"addr:{ids['tr'][name]['cp']}" for name, *_ in _SPECS}
    out = {}
    for name, src in cp_node.items():
        out[name] = next((e for e in g["edges"] if e["kind"] == "transfer" and e["source"] == src), None)
    return out


# --- foundation: the sequence key -----------------------------------------------------------

def test_read_model_surfaces_block_height_sequence_key(case):
    conn, _db, ids = case
    g = build_graph(conn)
    edges = _edges_by_cp(g, ids)
    # A confirmed transfer carries seq == its tx block_height (the "order by sequence" key).
    for name, height, _a, _u in _SPECS:
        e = edges[name]
        assert e is not None
        if height is None:
            assert e.get("seq_missing") is True and "seq" not in e  # mempool -> no orderable key
        else:
            assert e["seq"] == height and "seq_missing" not in e
    # tx routing nodes carry the same key (BTC "order by sequence" orders an address's txs).
    tx_nodes = [n for n in g["nodes"] if n["kind"] == "transaction"]  # (EVM has none, but the field exists)
    for n in tx_nodes:
        assert ("seq" in n) or (n.get("seq_missing") is True)


# --- feature 2: the user_dust value filter --------------------------------------------------

def test_value_filter_buckets_priced_subthreshold_and_keeps_unpriced_visible(case):
    conn, _db, ids = case
    fnid = f"addr:{ids['focus']}"
    # group_dust OFF so nothing auto-aggregates; the ONLY bucketing is the value filter at $100.
    v = build_view(conn, focus=fnid, group_dust=False, user_dust_usd=100.0, node_cap=400)

    user_dust = [n for n in v["nodes"] if n["kind"] == "user_dust"]
    assert len(user_dust) == 1
    ud = user_dust[0]
    # mid ($50) + small ($20) + mempool ($30) are priced-below-$100 -> bucketed; big ($5000) is not.
    assert ud["count"] == 3
    assert "below $100" in ud["label"]
    folded = set(ud["underlying"])
    assert {f"addr:{ids['tr'][n]['cp']}" for n in ("mid", "small", "mempool")} == folded

    addrs = {n["id"] for n in v["nodes"] if n["kind"] == "address"}
    assert f"addr:{ids['tr']['big']['cp']}" in addrs        # big stays (above threshold)
    assert f"addr:{ids['tr']['noprice']['cp']}" in addrs    # UNPRICED stays visible — never swept
    # the bucketed priced counterparties are gone from the individual node set
    for n in ("mid", "small", "mempool"):
        assert f"addr:{ids['tr'][n]['cp']}" not in addrs


def test_user_dust_never_merges_with_the_automatic_dust_bucket(case):
    conn, _db, ids = case
    fnid = f"addr:{ids['focus']}"
    # AUTO dust floor $25 -> small ($20) auto-dusts; user filter $40 -> mempool ($30) folds to user_dust;
    # big ($5000) + mid ($50) stay. The unpriced 'noprice' (2 ETH, well above the native dust floor) stays
    # VISIBLE — P8.6 "unpriced ≠ dust": it is NEITHER auto-dusted nor folded by the USD value filter.
    v = build_view(conn, focus=fnid, group_dust=True, dust_floor_usd=25.0, user_dust_usd=40.0,
                   node_cap=400)
    auto = [n for n in v["nodes"] if n["kind"] == "aggregate"]
    user = [n for n in v["nodes"] if n["kind"] == "user_dust"]
    assert len(auto) == 1 and len(user) == 1                       # two SEPARATE buckets
    assert auto[0]["id"].startswith("agg:") and user[0]["id"].startswith("udust:")
    assert f"addr:{ids['tr']['small']['cp']}" in set(auto[0]["underlying"])    # $20 -> auto dust
    assert f"addr:{ids['tr']['mempool']['cp']}" in set(user[0]["underlying"])  # $30 -> user filter
    # the large unpriced movement is in NEITHER bucket — it stays a visible address node
    addrs = {n["id"] for n in v["nodes"] if n["kind"] == "address"}
    assert f"addr:{ids['tr']['noprice']['cp']}" in addrs
    assert f"addr:{ids['tr']['noprice']['cp']}" not in set(auto[0]["underlying"])
    assert f"addr:{ids['tr']['noprice']['cp']}" not in set(user[0]["underlying"])


def test_user_dust_is_expandable_to_the_real_underlying(case):
    conn, _db, ids = case
    fnid = f"addr:{ids['focus']}"
    v = build_view(conn, focus=fnid, group_dust=False, user_dust_usd=100.0, node_cap=400)
    udid = next(n["id"] for n in v["nodes"] if n["kind"] == "user_dust")
    expanded = build_view(conn, focus=fnid, group_dust=False, user_dust_usd=100.0, node_cap=400,
                          expand=(udid,))
    assert not any(n["kind"] == "user_dust" for n in expanded["nodes"])   # the bundle is gone
    addrs = {n["id"] for n in expanded["nodes"] if n["kind"] == "address"}
    for n in ("mid", "small", "mempool"):
        assert f"addr:{ids['tr'][n]['cp']}" in addrs


# --- feature 3: per-view value-driven thickness ---------------------------------------------

def test_thickness_normalizes_against_the_visible_min_max(case):
    conn, _db, ids = case
    fnid = f"addr:{ids['focus']}"
    ew_min = dimension("edge.thickness.min", 1.8)
    ew_max = dimension("edge.thickness.max", 7.0)
    ew_unpriced = dimension("edge.thickness.unpriced", 1.5)

    # Everything visible (no aggregation). Visible priced USD spans $20..$5000.
    v = build_view(conn, focus=fnid, group_dust=False, node_cap=400)
    edges = _edges_by_cp(v, ids)
    assert edges["big"]["ew"] == pytest.approx(ew_max)     # largest visible -> max
    assert edges["small"]["ew"] == pytest.approx(ew_min)   # smallest visible -> min
    # P8.6 "unpriced ≠ dust": the unpriced edge is NO LONGER flattened to the thin baseline — it is
    # scaled by its NATIVE amount per asset. As the lone unpriced ETH edge it sits at the midpoint
    # (not the baseline, not pinned to the USD min/max), and stays flagged no_price + excluded from the
    # USD basis meta.
    assert edges["noprice"]["ew"] == pytest.approx((ew_min + ew_max) / 2)
    assert edges["noprice"]["ew"] != pytest.approx(ew_unpriced)
    assert edges["noprice"].get("no_price") is True
    assert v["meta"]["value_min_usd"] == 20.0 and v["meta"]["value_max_usd"] == 5000.0


def test_unpriced_large_movement_renders_large_not_dust():
    """The #2 fix in isolation: among UNPRICED edges of one asset, the bigger NATIVE amount renders
    THICKER (a 100 ETH no-price movement is never swept to the thin 'dust' baseline just because
    DeFiLlama lacked a price)."""
    ew_min = dimension("edge.thickness.min", 1.8)
    ew_max = dimension("edge.thickness.max", 7.0)
    edges = [
        {"kind": "transfer", "no_price": True, "asset_symbol": "ETH", "value_num": 100.0},  # huge
        {"kind": "transfer", "no_price": True, "asset_symbol": "ETH", "value_num": 0.001},   # tiny
    ]
    scale_edge_widths(edges)  # USD basis, but no priced edges -> native per asset
    assert edges[0]["ew"] == pytest.approx(ew_max)   # 100 ETH -> thick (NOT dust)
    assert edges[1]["ew"] == pytest.approx(ew_min)   # 0.001 ETH -> thin


def test_thickness_is_per_view__same_edge_rethickens_as_the_visible_set_changes(case):
    conn, _db, ids = case
    fnid = f"addr:{ids['focus']}"
    ew_min = dimension("edge.thickness.min", 1.8)

    # Full view: the visible USD basis spans $20..$5000, so 'mid' ($50) sits ABOVE the minimum.
    full = _edges_by_cp(build_view(conn, focus=fnid, group_dust=False, node_cap=400), ids)
    mid_full = full["mid"]["ew"]
    assert mid_full > ew_min

    # Bound the view so the only PRICED edges visible are big ($5000) + mid ($50) (the unpriced edge is
    # kept too — P8.6 never dusts a no-price movement by the cap — but it's excluded from the USD basis).
    # Now 'mid' IS the visible priced minimum, so the SAME edge thins to ew_min (per-view normalization).
    sub = build_view(conn, focus=fnid, group_dust=False, node_cap=4)
    mid_sub = _edges_by_cp(sub, ids)["mid"]["ew"]
    assert mid_sub == pytest.approx(ew_min)
    assert mid_sub < mid_full


def test_scale_edge_widths_excludes_unpriced_from_the_basis():
    # A direct unit check of the shared model: unpriced edges never enter min/max, get the baseline.
    ew_min = dimension("edge.thickness.min", 1.8)
    ew_unpriced = dimension("edge.thickness.unpriced", 1.5)
    edges = [
        {"kind": "transfer", "value_usd": 10.0},
        {"kind": "transfer", "value_usd": 1000.0},
        {"kind": "transfer", "no_price": True},   # excluded from the basis -> baseline
    ]
    basis = scale_edge_widths(edges)
    assert basis == {"basis": "usd", "min_usd": 10.0, "max_usd": 1000.0}
    assert edges[0]["ew"] == pytest.approx(ew_min)    # the $10 is the min of the basis
    assert edges[2]["ew"] == pytest.approx(ew_unpriced)


# --- the line that must never move: display-only ---------------------------------------------

def test_value_features_write_no_rows(case):
    conn, db, ids = case
    fnid = f"addr:{ids['focus']}"
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    for kw in ({"user_dust_usd": 100.0}, {"user_dust_usd": 100.0, "group_dust": False},
               {"value_floor_usd": 100.0}, {"group_dust": False}):
        build_view(conn, focus=fnid, **kw)
    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    assert before == after
    assert all(r.passed for r in run_audits(db_path=str(db)))
