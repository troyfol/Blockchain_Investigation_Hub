"""P3 / UX-03 — provenance-on-hover: the graph payload carries the source cues the canvas tooltip needs.

FN-01 (P1) made every claim's `source_query` reachable in the SidePanel; UX-03 brings the *source* onto
the canvas itself. This asserts the read-model exposes, per node, the distinct source(s) asserting its
risk / attribution (so a hover reads "risk: ofac-sdn"), and per fact edge (transfer / tx_input /
tx_output) the connector that acquired it + its `source_query_id` (so a hover names the source and a
click can drill through to P1's endpoint). A parallel-edge rollup keeps the DISTINCT set of sources
side-by-side — never collapsed to one (Invariant #4). Read-only surfacing; no invariant is at risk.
"""

from __future__ import annotations

from backend.app.db import repository as repo
from backend.app.models import (
    Address, Asset, Attribution, RiskAssessment, SourceQuery, Transaction, Transfer,
)
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.graph import build_graph
from backend.tests.integration._helpers import new_case, seed_btc_custom, seed_evm_address

_FACT_KINDS = ("transfer", "tx_input", "tx_output")


def test_edges_carry_source_for_tooltip(tmp_path):
    """Every fact edge names the connector that acquired it + carries its source_query id, and a
    risk/attributed node carries the source names behind those claims."""
    conn, db = new_case(tmp_path, title="Provenance on canvas")
    evm_addr = seed_evm_address(conn, "0x" + "ab" * 20)          # connector 'etherscan' -> a transfer edge
    seed_btc_custom(conn, txid="aa" * 32, input_addrs=["bc1qsrc"], output_amounts=[50_000])  # 'esplora'

    # A risk + attribution claim on the EVM address, from NAMED sources -> the node's hover source cue.
    sq = SourceQuery(connector="ofac", capability="get_risk", endpoint="sdn",
                     params={"bounds": "default"}, requested_at="2026-02-01T00:00:00Z", status="ok")

    def w(c, sqid):
        repo.upsert_risk_assessment(c, RiskAssessment(
            address_id=evm_addr, score=None, score_scale=None, category="sanctioned",
            rationale="OFAC SDN", source="ofac-sdn", retrieved_at="2026-02-01T00:00:00Z"), sqid)
        repo.insert_attribution(c, Attribution(
            address_id=evm_addr, label="Lazarus", category="threat_actor", source="graphsense",
            confidence=0.7, retrieved_at="2026-02-01T00:00:00Z"), sqid)

    write_with_provenance(conn, sq, w)

    g = build_graph(conn)

    # (1) every fact edge names the connector that acquired it + carries its source_query id (drill-through).
    facts = [e for e in g["edges"] if e["kind"] in _FACT_KINDS]
    assert facts, "expected transfer + tx_input + tx_output fact edges"
    for e in facts:
        assert e.get("source_name"), f"edge {e['id']} missing source_name"
        assert e.get("source_query_id"), f"edge {e['id']} missing source_query_id"
    by_kind = {e["kind"]: e for e in facts}
    assert by_kind["transfer"]["source_name"] == "etherscan"
    assert by_kind["tx_input"]["source_name"] == "esplora"
    assert by_kind["tx_output"]["source_name"] == "esplora"

    # (2) the risk/attributed node carries the SOURCE names behind those claims (the hover cue).
    node = next(n for n in g["nodes"] if n.get("id") == f"addr:{evm_addr}")
    assert node["risk_level"] == "sanctioned"
    assert node["risk_sources"] == ["ofac-sdn"]
    assert node["attribution_sources"] == ["graphsense"]
    conn.close()


def test_parallel_aggregate_keeps_distinct_sources(tmp_path):
    """Two A->B transfers from DIFFERENT connectors fold into one display rollup — but the rollup keeps
    BOTH sources side-by-side (Invariant #4: never collapse multi-source claims into one)."""
    conn, db = new_case(tmp_path, title="Aggregate sources")
    A = "0x" + "aa" * 20
    B = "0x" + "bb" * 20

    def seed_transfer(connector, tx_hash):
        sq = SourceQuery(connector=connector, capability="get_transactions", endpoint="txlist",
                         params={"address": A}, requested_at="2026-02-01T00:00:00Z", status="ok")

        def w(c, sqid):
            asset = repo.upsert_asset(c, Asset(chain="ethereum", symbol="ETH", decimals=18), sqid)
            a = repo.upsert_address(c, Address(chain="ethereum", address_display=A), sqid)
            b = repo.upsert_address(c, Address(chain="ethereum", address_display=B), sqid)
            tx = repo.upsert_transaction(c, Transaction(
                chain="ethereum", tx_hash=tx_hash, confirmations=100, finality_status="final"), sqid)
            repo.upsert_transfer(c, Transfer(
                transaction_id=tx, chain="ethereum", from_address_id=a, to_address_id=b,
                asset_id=asset, amount="1000000000000000000", transfer_type="native", position=0), sqid)

        write_with_provenance(conn, sq, w)

    seed_transfer("etherscan", "0x" + "e" * 64)
    seed_transfer("arkham-import", "0x" + "d" * 64)

    g = build_graph(conn)  # aggregate=True (default) -> the two A->B transfers fold into one edge
    transfers = [e for e in g["edges"] if e["kind"] == "transfer"]
    assert len(transfers) == 1, "the two parallel A->B transfers should fold into one display edge"
    agg = transfers[0]
    assert agg.get("parallel_aggregate") is True and agg.get("count") == 2
    # BOTH acquiring sources are preserved side-by-side (sorted, distinct) — never collapsed to one.
    assert agg["source_names"] == ["arkham-import", "etherscan"]
    conn.close()
