"""P8.7.3 #3 — parallel same-(source,target,asset) fact edges collapse into ONE legible display rollup
(count + summed value), the individual movements stay reachable via ``underlying`` (drill-down), and the
rollup is a DISPLAY artifact only — never a synthesized transfer (Invariant #5). Singletons and any
annotated/flagged edge pass through individually."""

from __future__ import annotations

from backend.app.db import repository as repo
from backend.app.models import Address, Asset, SourceQuery, Transaction, Transfer, Valuation
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.graph import build_graph
from backend.app.services.graph_view import build_view
from backend.tests.integration._helpers import new_case

A = "0x" + "a1" * 20
B = "0x" + "b2" * 20


def _seed_parallel_transfers(conn, n=12, *, value_each_usd=None):
    """Seed ``n`` native-ETH transfers A->B, each its own tx. Returns the transfer ids (== movement ids)."""
    sq = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="txlist",
                     params={"address": A, "bounds": "default"}, requested_at="2026-01-01T00:00:00Z",
                     status="ok")
    ids: list[str] = []

    def write(c, sqid):
        asset_id = repo.upsert_asset(c, Asset(chain="ethereum", symbol="ETH", decimals=18), sqid)
        a_id = repo.upsert_address(c, Address(chain="ethereum", address_display=A), sqid)
        b_id = repo.upsert_address(c, Address(chain="ethereum", address_display=B), sqid)
        for i in range(n):
            tx_id = repo.upsert_transaction(c, Transaction(
                chain="ethereum", tx_hash="0x" + f"{i:064x}", block_height=900 + i,
                block_ts="2026-01-01T00:00:00Z", fee="0", status="1", confirmations=100,
                finality_status="final"), sqid)
            tr = repo.upsert_transfer(c, Transfer(
                transaction_id=tx_id, chain="ethereum", from_address_id=a_id, to_address_id=b_id,
                asset_id=asset_id, amount="1000000000000000000", transfer_type="native", position=0), sqid)
            ids.append(tr)
            if value_each_usd is not None:
                repo.insert_valuation(c, Valuation(
                    subject_type="transfer", subject_id=tr, currency="USD", unit_price=str(value_each_usd),
                    value=str(value_each_usd), price_timestamp="2026-01-01T00:00:00Z", confidence=0.9,
                    source="defillama", retrieved_at="2026-01-01T00:00:00Z"), sqid)

    write_with_provenance(conn, sq, write)
    return ids


def test_parallel_transfers_collapse_with_count_and_summed_value(tmp_path):
    conn, _db = new_case(tmp_path, title="Parallel")
    ids = _seed_parallel_transfers(conn, n=12, value_each_usd=5.0)

    g = build_graph(conn)                       # aggregated (the report path)
    transfers = [e for e in g["edges"] if e["kind"] == "transfer"]
    assert len(transfers) == 1                  # 12 parallels -> ONE rollup edge
    agg = transfers[0]
    assert agg["parallel_aggregate"] is True and agg["count"] == 12
    assert agg["value_num"] == 12.0             # 12 × 1 ETH summed
    assert agg["value_usd"] == 60.0             # 12 × $5 summed
    # drill-down: the rollup points back at the REAL underlying movements (Inv #5 — facts untouched)
    assert sorted(agg["underlying"]) == sorted(f"mv:{i}" for i in ids)
    assert len(agg["underlying"]) == 12
    # no fabricated paradigm: it stays a 'transfer' between the two real addresses
    assert agg["source"].startswith("addr:") and agg["target"].startswith("addr:")


def test_aggregate_false_keeps_individual_edges(tmp_path):
    conn, _db = new_case(tmp_path, title="Raw")
    _seed_parallel_transfers(conn, n=12)
    raw = build_graph(conn, aggregate=False)
    assert len([e for e in raw["edges"] if e["kind"] == "transfer"]) == 12   # un-collapsed


def test_annotated_edge_is_not_collapsed(tmp_path):
    """An investigator-annotated movement stays an INDIVIDUAL edge (selectable, its provenance intact)."""
    from backend.app.services.investigator import add_annotation

    conn, _db = new_case(tmp_path, title="Annotated")
    ids = _seed_parallel_transfers(conn, n=6)
    add_annotation(conn, target_type="transfer", target_id=ids[0], content="follow this one")

    g = build_graph(conn)
    transfers = [e for e in g["edges"] if e["kind"] == "transfer"]
    # one annotated singleton (kept) + one rollup of the other 5
    singles = [e for e in transfers if not e.get("parallel_aggregate")]
    rollups = [e for e in transfers if e.get("parallel_aggregate")]
    assert len(singles) == 1 and singles[0].get("has_annotation")
    assert len(rollups) == 1 and rollups[0]["count"] == 5


def test_build_view_collapses_parallels_and_reports_count(tmp_path):
    conn, _db = new_case(tmp_path, title="View")
    _seed_parallel_transfers(conn, n=20)
    v = build_view(conn, focus=A, hops=1)
    transfers = [e for e in v["edges"] if e["kind"] == "transfer"]
    assert len(transfers) == 1 and transfers[0]["count"] == 20
    assert v["meta"]["parallel_collapsed"] == 19      # 20 folded into 1

    raw = build_view(conn, focus=A, hops=1, aggregate_parallel=False)
    assert len([e for e in raw["edges"] if e["kind"] == "transfer"]) == 20
    assert raw["meta"]["parallel_collapsed"] == 0
