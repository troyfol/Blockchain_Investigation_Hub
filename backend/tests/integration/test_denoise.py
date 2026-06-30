"""P8.7 EVM de-noise: per-denomination min/fold (#1), unverified-token collapse (#2), and the
address-poisoning heuristic (#3). All display-only over real facts (Inv #5)."""

from __future__ import annotations

import pytest

from backend.app.db import repository as repo
from backend.app.models import Address, Asset, SourceQuery, Transaction, Transfer, Valuation
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.graph import build_graph
from backend.app.services.graph_view import build_view
from backend.tests.integration._helpers import new_case

FOCUS = "0x" + "f" * 40


def _seed(conn, rows):
    """rows: list of (from_addr, amount_wei, asset_key, usd_or_None). asset_key: 'ETH' native or a token
    symbol (contract token). Each row is one inbound transfer to FOCUS in its own tx."""
    sq = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="txlist",
                     params={"address": "focus", "bounds": "default"}, requested_at="2026-01-01T00:00:00Z",
                     status="ok")
    ids = {"focus": None, "tr": []}

    def write(c, sqid):
        eth = repo.upsert_asset(c, Asset(chain="ethereum", symbol="ETH", decimals=18), sqid)
        assets = {"ETH": eth}
        focus = repo.upsert_address(c, Address(chain="ethereum", address_display=FOCUS), sqid)
        ids["focus"] = focus
        for i, (frm, amt, akey, _usd) in enumerate(rows):
            if akey not in assets:
                assets[akey] = repo.upsert_asset(c, Asset(
                    chain="ethereum", symbol=akey, decimals=18,
                    contract_address="0x" + format(hash(akey) & 0xffffffff, "040x")), sqid)
            cp = repo.upsert_address(c, Address(chain="ethereum", address_display=frm), sqid)
            tx = repo.upsert_transaction(c, Transaction(
                chain="ethereum", tx_hash="0x" + format(i + 1, "064x"), block_height=100 + i,
                block_ts="2026-01-01T00:00:00Z", confirmations=100, finality_status="final"), sqid)
            tr = repo.upsert_transfer(c, Transfer(
                transaction_id=tx, chain="ethereum", from_address_id=cp, to_address_id=focus,
                asset_id=assets[akey], amount=str(amt), transfer_type="native", position=0), sqid)
            ids["tr"].append({"cp": cp, "tr": tr, "from": frm})

    write_with_provenance(conn, sq, write)

    priced = [(r, ids["tr"][i]["tr"]) for i, r in enumerate(rows) if r[3] is not None]
    if priced:
        sq2 = SourceQuery(connector="defillama", capability="get_price", endpoint="p",
                          params={}, requested_at="2026-01-01T00:00:00Z", status="ok")

        def write2(c, sqid):
            for (frm, amt, akey, usd), trid in priced:
                repo.insert_valuation(c, Valuation(
                    subject_type="transfer", subject_id=trid, currency="USD", unit_price=str(usd),
                    value=str(usd), price_timestamp="2026-01-01T00:00:00Z", confidence=0.9,
                    source="defillama", retrieved_at="2026-01-01T00:00:00Z"), sqid)

        write_with_provenance(conn, sq2, write2)
    return ids


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="Denoise")
    yield conn, db
    conn.close()


# --------------------------------------------------------------------------- #2 unverified collapse

def test_unverified_tokens_collapse_by_default_and_reveal_on_toggle(case):
    conn, _db = case
    a = "0x" + "a" * 40
    b = "0x" + "b" * 40
    spam = "0x" + "c" * 40
    ids = _seed(conn, [
        (a, 5 * 10**18, "ETH", "5000"),     # native ETH (always prominent)
        (b, 10**18, "USDC", "1000"),         # priced token (verified)
        (spam, 10**24, "VISA", None),        # huge UNPRICED spam token -> unverified
    ])
    fnid = f"addr:{ids['focus']}"

    default = build_view(conn, focus=fnid, group_dust=False, node_cap=400)
    addrs = {n["id"] for n in default["nodes"] if n["kind"] == "address"}
    assert f"addr:{ids['tr'][0]['cp']}" in addrs    # ETH stays
    assert f"addr:{ids['tr'][1]['cp']}" in addrs    # priced USDC stays
    assert f"addr:{ids['tr'][2]['cp']}" not in addrs  # spam collapsed
    assert any(n["kind"] == "unverified" for n in default["nodes"])
    assert default["meta"]["unverified_token_edges"] >= 1

    revealed = build_view(conn, focus=fnid, group_dust=False, node_cap=400, show_unverified=True)
    addrs2 = {n["id"] for n in revealed["nodes"] if n["kind"] == "address"}
    assert f"addr:{ids['tr'][2]['cp']}" in addrs2    # spam now visible
    assert not any(n["kind"] == "unverified" for n in revealed["nodes"])


# --------------------------------------------------------------------------- #1 per-denomination filters

def test_per_denomination_fold_touches_only_its_own_asset(case):
    conn, _db = case
    # two pools: cDAI (a small 5 + a big 5,000,000) and DAI (a small 1 + a big 100,000). Fold the cDAI
    # SMALL only; the DAI small must remain untouched.
    rows = [
        ("0x" + "1" * 40, 5 * 10**18, "CDAI", "5"),
        ("0x" + "2" * 40, 5_000_000 * 10**18, "CDAI", "5000000"),
        ("0x" + "3" * 40, 1 * 10**18, "DAI", "1"),
        ("0x" + "4" * 40, 100_000 * 10**18, "DAI", "100000"),
    ]
    ids = _seed(conn, rows)
    fnid = f"addr:{ids['focus']}"
    v = build_view(conn, focus=fnid, group_dust=False, node_cap=400,
                   denom_filters={"CDAI": {"fold": 1000.0}})  # fold cDAI < 1000 (native)
    addrs = {n["id"] for n in v["nodes"] if n["kind"] == "address"}
    assert f"addr:{ids['tr'][0]['cp']}" not in addrs   # cDAI small (5) folded
    assert f"addr:{ids['tr'][1]['cp']}" in addrs       # cDAI big (5M) stays
    assert f"addr:{ids['tr'][2]['cp']}" in addrs       # DAI small (1) UNTOUCHED by the cDAI fold
    assert f"addr:{ids['tr'][3]['cp']}" in addrs       # DAI big stays
    # the fold produced a user_dust bucket scoped to cDAI
    ud = [n for n in v["nodes"] if n["kind"] == "user_dust"]
    assert len(ud) == 1 and "CDAI" in ud[0]["label"]


def test_per_denomination_min_drops_only_its_own_asset(case):
    conn, _db = case
    rows = [
        ("0x" + "1" * 40, 5 * 10**18, "CDAI", None),
        ("0x" + "2" * 40, 5_000_000 * 10**18, "CDAI", None),
        ("0x" + "3" * 40, 1 * 10**18, "DAI", None),
    ]
    ids = _seed(conn, rows)
    fnid = f"addr:{ids['focus']}"
    v = build_view(conn, focus=fnid, group_dust=False, node_cap=400, show_unverified=True,
                   denom_filters={"CDAI": {"min": 1000.0}})  # drop cDAI < 1000
    addrs = {n["id"] for n in v["nodes"] if n["kind"] == "address"}
    assert f"addr:{ids['tr'][0]['cp']}" not in addrs   # cDAI 5 dropped (below the per-denom min)
    assert f"addr:{ids['tr'][1]['cp']}" in addrs       # cDAI 5M stays
    assert f"addr:{ids['tr'][2]['cp']}" in addrs       # DAI 1 untouched (different asset)


# --------------------------------------------------------------------------- #3 address poisoning

def test_address_poisoning_zero_value_lookalike_is_flagged(case):
    conn, _db = case
    real = "0xabcd" + "0" * 32 + "1234"          # a genuine counterparty (first4 abcd, last4 1234)
    look = "0xabcd" + "f" * 32 + "1234"          # a 0-value LOOK-ALIKE (same first4 + last4)
    ids = _seed(conn, [
        (real, 3 * 10**18, "ETH", "6000"),       # FOCUS genuinely transacts with `real` (non-zero)
        (look, 0, "ETH", None),                  # the look-alike sends FOCUS a ZERO-value transfer
    ])
    g = build_graph(conn)
    look_edge = next(e for e in g["edges"]
                     if e["kind"] == "transfer" and e["source"] == f"addr:{ids['tr'][1]['cp']}")
    assert look_edge.get("poison_suspect") is True
    assert look_edge.get("poison_lookalike", "").lower() == real.lower()
    look_node = next(n for n in g["nodes"] if n.get("address") == look.lower())
    assert look_node.get("poison_suspect") is True
    # the genuine counterparty is NOT flagged
    real_node = next(n for n in g["nodes"] if n.get("address") == real.lower())
    assert not real_node.get("poison_suspect")


def test_poison_transfers_fold_into_their_own_bucket(case):
    conn, _db = case
    real = "0xabcd" + "0" * 32 + "1234"
    look = "0xabcd" + "f" * 32 + "1234"
    ids = _seed(conn, [(real, 3 * 10**18, "ETH", "6000"), (look, 0, "ETH", None)])
    fnid = f"addr:{ids['focus']}"
    folded = build_view(conn, focus=fnid, group_dust=False, node_cap=400, fold_poison=True)
    assert any(n["kind"] == "poison" for n in folded["nodes"])
    assert folded["meta"]["poison_suspect_edges"] >= 1
    addrs = {n["id"] for n in folded["nodes"] if n["kind"] == "address"}
    assert f"addr:{ids['tr'][1]['cp']}" not in addrs   # the look-alike folded out of the way
