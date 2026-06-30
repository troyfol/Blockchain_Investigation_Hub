"""P8.6 native-amount value model: USD<->native basis, unpriced-≠-dust, denomination grouping, the
expand cap, and chain-aware/tolerant ingest bounds. All display-only over real facts (Inv #5)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from backend.app.audits.runner import run_audits
from backend.app.config import get_settings
from backend.app.connectors.base import RateLimiter
from backend.app.connectors.esplora import EsploraConnector
from backend.app.db import repository as repo
from backend.app.models import Address, Asset, SourceQuery, Transaction, Transfer, Valuation
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.graph_view import EXPAND_REVEAL_CAP, build_view
from backend.tests.integration._helpers import new_case

ETH = 10 ** 18
FOCUS = "0x" + "f" * 40


def _seed(conn, *, n_pool=0, pool_wei=100 * ETH, priced_pool=False, n_dust=0, dust_wei=10 ** 14,
          n_priced=0, priced_wei=ETH, priced_usd="2000", second_asset=False):
    """Seed a focus address with configurable inbound transfers. Pools share ONE native amount (mixer-
    like); dust is tiny+unpriced; priced are valued. Returns ids."""
    sq = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="txlist",
                     params={"address": "focus", "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")
    ids: dict = {"pool": [], "dust": [], "priced": [], "second": []}

    def write(c, sqid):
        eth = repo.upsert_asset(c, Asset(chain="ethereum", symbol="ETH", decimals=18), sqid)
        usdc = repo.upsert_asset(c, Asset(chain="ethereum", symbol="USDC", decimals=6,
                                          contract_address="0x" + "c" * 40), sqid) if second_asset else None
        focus = repo.upsert_address(c, Address(chain="ethereum", address_display=FOCUS), sqid)
        ids.update(focus=focus, eth=eth)
        i = 0

        def add(bucket, amount, asset, *, conf=100, fin="final"):
            nonlocal i
            i += 1
            cp = repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + format(i, "040x")), sqid)
            tx = repo.upsert_transaction(c, Transaction(
                chain="ethereum", tx_hash="0x" + format(i, "064x"), block_height=100 + i,
                block_ts="2026-01-01T00:00:00Z", confirmations=conf, finality_status=fin), sqid)
            tr = repo.upsert_transfer(c, Transfer(
                transaction_id=tx, chain="ethereum", from_address_id=cp, to_address_id=focus,
                asset_id=asset, amount=str(amount), transfer_type="native", position=0), sqid)
            ids[bucket].append({"cp": cp, "tr": tr})

        for _ in range(n_pool):
            add("pool", pool_wei, eth)
        for _ in range(n_dust):
            add("dust", dust_wei, eth)
        for _ in range(n_priced):
            add("priced", priced_wei, eth)
        if second_asset:
            add("second", 5_000_000, usdc)  # 5 USDC (6 decimals)

    write_with_provenance(conn, sq, write)

    if priced_pool or n_priced:
        sq2 = SourceQuery(connector="defillama", capability="get_price", endpoint="prices/historical",
                          params={}, requested_at="2026-01-01T00:00:00Z", status="ok")

        def write2(c, sqid):
            targets = (ids["pool"] if priced_pool else []) + ids["priced"]
            for t in targets:
                repo.insert_valuation(c, Valuation(
                    subject_type="transfer", subject_id=t["tr"], currency="USD", unit_price=priced_usd,
                    value=priced_usd, price_timestamp="2026-01-01T00:00:00Z", confidence=0.9,
                    source="defillama", retrieved_at="2026-01-01T00:00:00Z"), sqid)

        write_with_provenance(conn, sq2, write2)
    return ids


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="NativeModel")
    yield conn, db
    conn.close()


# --------------------------------------------------------------------------- #2 unpriced ≠ dust

def test_large_unpriced_pool_stays_visible_not_dust(case):
    conn, _db = case
    ids = _seed(conn, n_pool=4, pool_wei=100 * ETH, n_dust=3, dust_wei=10 ** 13)  # 0.00001 ETH dust
    v = build_view(conn, focus=f"addr:{ids['focus']}", group_dust=True, node_cap=400)
    addrs = {n["id"] for n in v["nodes"] if n["kind"] == "address"}
    # the 100 ETH UNPRICED pool members are visible (never auto-dusted just for lacking a USD price)
    for p in ids["pool"]:
        assert f"addr:{p['cp']}" in addrs
    # the tiny unpriced dust DOES still aggregate (small native amount)
    assert any(n["kind"] == "aggregate" for n in v["nodes"])
    big = next(e for e in v["edges"] if e.get("source") == f"addr:{ids['pool'][0]['cp']}")
    assert big["ew"] > 3.0  # rendered large, not the thin dust baseline


# --------------------------------------------------------------------------- #3 USD <-> native basis

def test_native_basis_switches_labels_and_widths(case):
    conn, _db = case
    ids = _seed(conn, n_priced=2, priced_wei=ETH, n_pool=0)
    fnid = f"addr:{ids['focus']}"

    usd = build_view(conn, focus=fnid, group_dust=False, node_cap=400)
    nat = build_view(conn, focus=fnid, group_dust=False, node_cap=400, value_basis="native")
    assert usd["meta"]["value_basis"] == "usd" and nat["meta"]["value_basis"] == "native"

    priced_src = f"addr:{ids['priced'][0]['cp']}"
    usd_e = next(e for e in usd["edges"] if e.get("source") == priced_src)
    nat_e = next(e for e in nat["edges"] if e.get("source") == priced_src)
    # USD mode keeps the USD label (wins on the canvas); native mode strips it so the native label shows.
    assert usd_e.get("value_usd_label", "").startswith("$")
    assert "value_usd_label" not in nat_e
    assert nat_e["value_label"].endswith("ETH")


def test_native_basis_scales_per_asset(case):
    conn, _db = case
    # Two assets: a big ETH transfer + a small USDC transfer. In native mode each asset normalizes over
    # its OWN min/max (never one fake combined native scale), so a single edge per asset -> both midpoint.
    ids = _seed(conn, n_pool=2, pool_wei=100 * ETH, second_asset=True)
    fnid = f"addr:{ids['focus']}"
    nat = build_view(conn, focus=fnid, group_dust=False, node_cap=400, value_basis="native")
    syms = {e.get("asset_symbol") for e in nat["edges"] if e["kind"] == "transfer"}
    assert {"ETH", "USDC"} <= syms  # both assets present, each scaled within itself


# --------------------------------------------------------------------------- #7 denomination grouping

def test_denomination_grouping_forms_over_equal_pools(case):
    conn, _db = case
    ids = _seed(conn, n_pool=5, pool_wei=100 * ETH)  # five counterparties each sending EXACTLY 100 ETH
    fnid = f"addr:{ids['focus']}"
    off = build_view(conn, focus=fnid, group_dust=False, node_cap=400)
    on = build_view(conn, focus=fnid, group_dust=False, node_cap=400, group_denominations=True)

    assert not any(n.get("group_type") == "denomination" for n in off["nodes"])  # off by default
    groups = [n for n in on["nodes"] if n.get("group_type") == "denomination"]
    assert len(groups) == 1
    assert groups[0]["pool_size"] == 5 and "ETH" in groups[0]["label"]
    assert on["meta"]["denomination_groups"] == 1
    # the pool members are now children of the denomination cluster
    members = [n for n in on["nodes"] if n.get("parent") == groups[0]["id"]]
    assert len(members) == 5


# --------------------------------------------------------------------------- #5 expand cap + residual

def test_expand_caps_visible_and_rolls_remainder_into_more(case):
    conn, _db = case
    n = EXPAND_REVEAL_CAP + 12
    ids = _seed(conn, n_dust=n, dust_wei=10 ** 13)  # many tiny unpriced -> one auto-dust aggregate
    fnid = f"addr:{ids['focus']}"
    v = build_view(conn, focus=fnid, group_dust=True, node_cap=1000)
    aggid = next(x["id"] for x in v["nodes"] if x["kind"] == "aggregate")

    exp = build_view(conn, focus=fnid, group_dust=True, node_cap=1000, expand=(aggid,))
    revealed = sum(1 for x in exp["nodes"] if x["kind"] == "address") - 1  # minus the focus
    assert revealed == EXPAND_REVEAL_CAP                       # capped, not exploded
    more = [x for x in exp["nodes"] if x.get("is_more")]
    assert len(more) == 1 and "more" in more[0]["label"]       # residual ":more" bundle

    # clicking "show more" reveals the rest
    rest = build_view(conn, focus=fnid, group_dust=True, node_cap=1000, expand=(aggid, more[0]["id"]))
    assert sum(1 for x in rest["nodes"] if x["kind"] == "address") - 1 == n


# --------------------------------------------------------------------------- #1 tolerant BTC ingest

@respx.mock
def test_btc_ingest_tolerates_an_evm_only_bound(tmp_path):
    """An EVM-only bound (top_n_counterparties) sent to Esplora is SKIPPED + the query marked partial,
    not raised — so a BTC ingest can't be hard-failed by the chain-agnostic depth control."""
    conn, db = new_case(tmp_path, title="BTC Tolerant")
    cassettes = __import__("pathlib").Path(__file__).resolve().parent.parent / "cassettes" / "esplora"

    def router(request):
        p = request.url.path
        if p.endswith("/blocks/tip/height"):
            return httpx.Response(200, text="800010")
        if "/address/" in p and p.endswith("/txs"):
            return httpx.Response(200, json=json.loads((cassettes / "address_txs.json").read_text()))
        if "/address/" in p and "/txs/chain/" in p:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=json.loads((cassettes / "address_stats.json").read_text()))

    respx.route(host="blockstream.info").mock(side_effect=router)
    c = EsploraConnector(settings=get_settings(), rate_limiter=RateLimiter(0, enabled=False),
                         sleep=lambda _s: None)
    try:
        # does NOT raise despite the unsupported bound
        c.get_transactions(conn, "bitcoin", "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq",
                           bounds={"top_n_counterparties": 10, "max_pages": 2})
    finally:
        c.close()
    assert conn.execute("SELECT COUNT(*) FROM transaction_").fetchone()[0] > 0  # ingested cleanly
    rows = conn.execute("SELECT status, params FROM source_query").fetchall()
    assert rows and all(r["status"] == "partial" for r in rows)
    assert all(json.loads(r["params"]).get("skipped_bounds") == ["top_n_counterparties"] for r in rows)
    assert all(r.passed for r in run_audits(db_path=str(db)))
