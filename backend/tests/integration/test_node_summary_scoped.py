"""Deferred EFF-01 follow-on: `api_node_summary` / `build_graph(focus_incident=…)` must be node-SCOPED
(no whole-case build per click) AND produce the IDENTICAL summary to the full-graph result. This test
compares the focused build's summary against the same aggregation run over the FULL graph (the reference
oracle), across EVM-address / BTC-address / BTC-tx-node focuses and a flagged counterparty.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.db import repository as repo
from backend.app.main import app, get_case_db_path
from backend.app.models import (Address, Asset, RiskAssessment, SourceQuery, Transaction, Transfer,
                                TxInput, TxOutput, Valuation)
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.graph import build_graph
from backend.tests.integration._helpers import new_case


def _summary_from_graph(g: dict, node_id: str) -> dict:
    """The reference oracle — the ORIGINAL api_node_summary aggregation, run over any graph dict."""
    nodes = {n["id"]: n for n in g["nodes"]}
    if node_id not in nodes:
        return {"missing": True}
    agg: dict[str, dict] = {}
    for e in g["edges"]:
        if e.get("kind") == "trace":
            continue
        if e["source"] == node_id or e["target"] == node_id:
            other = e["target"] if e["source"] == node_id else e["source"]
            if other == node_id:
                continue
            direction = "out" if e["source"] == node_id else "in"
            o = nodes.get(other, {})
            d = agg.setdefault(other, {
                "id": other, "label": o.get("label"), "kind": o.get("kind"),
                "address": o.get("address"), "risk_level": o.get("risk_level"),
                "has_attribution": o.get("has_attribution"), "entity_label": o.get("entity_label"),
                "in_usd": 0.0, "out_usd": 0.0, "in_count": 0, "out_count": 0, "usd": 0.0, "count": 0})
            usd = e.get("value_usd") or 0.0
            d[f"{direction}_usd"] += usd
            d[f"{direction}_count"] += 1
            d["usd"] += usd
            d["count"] += 1
    ranked = sorted(agg.values(), key=lambda x: (-x["usd"], -x["count"]))
    for d in ranked:
        d["in_usd"] = round(d["in_usd"], 2) or None
        d["out_usd"] = round(d["out_usd"], 2) or None
        d["usd"] = round(d["usd"], 2) or None
    return {"node_id": node_id, "label": nodes[node_id].get("label"), "val": nodes[node_id].get("val"),
            "counterparties": ranked[:50], "counterparty_total": len(ranked),
            "flagged": [d for d in ranked if d["risk_level"] or d["has_attribution"]][:50]}


@pytest.fixture
def rich_case(tmp_path):
    conn, db = new_case(tmp_path, title="Rich")
    ids: dict = {}
    sq = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="txlist",
                     params={"address": "probe", "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")

    def w(c, sqid):
        eth = repo.upsert_asset(c, Asset(chain="ethereum", symbol="ETH", decimals=18), sqid)
        btc = repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)
        A = repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + "a1" * 20), sqid)
        B = repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + "b2" * 20), sqid)
        C = repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + "c3" * 20), sqid)
        # EVM: B -> A ($2000 valued), A -> C ($500 valued)
        t1 = repo.upsert_transaction(c, Transaction(chain="ethereum", tx_hash="0x" + "11" * 32,
             block_height=10, confirmations=100, finality_status="final"), sqid, authoritative=True)
        trBA = repo.upsert_transfer(c, Transfer(transaction_id=t1, chain="ethereum", from_address_id=B,
               to_address_id=A, asset_id=eth, amount=str(10**18), transfer_type="native", position=0), sqid)
        t2 = repo.upsert_transaction(c, Transaction(chain="ethereum", tx_hash="0x" + "22" * 32,
             block_height=11, confirmations=100, finality_status="final"), sqid, authoritative=True)
        trAC = repo.upsert_transfer(c, Transfer(transaction_id=t2, chain="ethereum", from_address_id=A,
               to_address_id=C, asset_id=eth, amount=str(10**18), transfer_type="native", position=0), sqid)
        # BTC: F pays D (valued), D spends into S paying E
        D = repo.upsert_address(c, Address(chain="bitcoin", address_display="1D"), sqid)
        E = repo.upsert_address(c, Address(chain="bitcoin", address_display="1E"), sqid)
        F = repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="f" * 64, block_height=800000,
            confirmations=50, finality_status="final"), sqid, authoritative=True)
        o1 = repo.upsert_tx_output(c, TxOutput(transaction_id=F, address_id=D, amount="100", output_index=0), sqid)
        S = repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="5" * 64, block_height=800001,
            confirmations=49, finality_status="final"), sqid, authoritative=True)
        repo.upsert_tx_input(c, TxInput(transaction_id=S, prev_output_id=o1, address_id=D, amount="100", input_index=0), sqid)
        repo.upsert_tx_output(c, TxOutput(transaction_id=S, address_id=E, amount="90", output_index=0), sqid)
        ids.update(A=A, B=B, C=C, D=D, F=F, S=S, trBA=trBA, trAC=trAC, o1=o1)

    write_with_provenance(conn, sq, w)

    # Value the EVM transfers + the BTC output; flag B as sanctioned (a flagged counterparty).
    sqv = SourceQuery(connector="defillama", capability="get_price", endpoint="prices/historical",
                      params={}, requested_at="2026-01-01T00:00:00Z", status="ok")

    def wv(c, sqid):
        repo.insert_valuation(c, Valuation(subject_type="transfer", subject_id=ids["trBA"], currency="USD",
            unit_price="2000", value="2000", price_timestamp="2026-01-01T00:00:00Z", source="defillama",
            retrieved_at="2026-01-01T00:00:00Z", confidence=0.9), sqid)
        repo.insert_valuation(c, Valuation(subject_type="transfer", subject_id=ids["trAC"], currency="USD",
            unit_price="500", value="500", price_timestamp="2026-01-01T00:00:00Z", source="defillama",
            retrieved_at="2026-01-01T00:00:00Z", confidence=0.9), sqid)
        repo.insert_valuation(c, Valuation(subject_type="tx_output", subject_id=ids["o1"], currency="USD",
            unit_price="1", value="10", price_timestamp="2026-01-01T00:00:00Z", source="defillama",
            retrieved_at="2026-01-01T00:00:00Z", confidence=0.9), sqid)

    write_with_provenance(conn, sqv, wv)

    sqr = SourceQuery(connector="ofac", capability="get_risk", endpoint="import",
                      params={"bounds": "default"}, requested_at="2026-01-01T00:00:00Z", status="ok")
    write_with_provenance(conn, sqr, lambda c, sqid: repo.insert_risk_assessment(c, RiskAssessment(
        address_id=ids["B"], category="sanctioned", source="ofac-sdn", score=None, score_scale=None,
        rationale="OFAC", retrieved_at="2026-01-01T00:00:00Z"), sqid))
    yield conn, db, ids
    conn.close()


def test_focused_summary_matches_full_graph(rich_case):
    conn, db, ids = rich_case
    full = build_graph(conn)  # the reference (whole-case) graph
    app.dependency_overrides[get_case_db_path] = lambda: str(db)
    try:
        client = TestClient(app)
        for node_id in (f"addr:{ids['A']}", f"addr:{ids['D']}", f"tx:{ids['F']}", f"tx:{ids['S']}",
                        f"addr:{ids['B']}"):
            expected = _summary_from_graph(full, node_id)     # full-graph oracle
            got = client.get(f"/api/node/{node_id}/summary").json()  # node-scoped endpoint
            assert got == expected, f"node-scoped summary diverged from full-graph for {node_id}"
    finally:
        app.dependency_overrides.clear()


def test_focused_build_only_touches_neighborhood(rich_case):
    conn, db, ids = rich_case
    # A's neighborhood (EVM): A + its two counterparties B, C (3 address nodes), no BTC nodes.
    g = build_graph(conn, focus_incident=f"addr:{ids['A']}")
    addr_ids = {n["id"] for n in g["nodes"] if n["kind"] == "address"}
    assert addr_ids == {f"addr:{ids['A']}", f"addr:{ids['B']}", f"addr:{ids['C']}"}
    assert not any(n["kind"] == "transaction" for n in g["nodes"])  # no BTC tx nodes pulled in
    conn.close()
