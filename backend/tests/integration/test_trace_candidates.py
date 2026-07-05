"""P11 / FN-16 — guided multi-hop trace expansion (PROPOSE next hops; never auto-add).

From a trace's TERMINAL nodes (leaves of the effective trace — value arrived but not yet traced onward),
`trace_next_hops` surfaces candidate next movements drawn ONLY from facts already in the case. It is
strictly READ-ONLY: the investigator chooses which to add (via the existing add endpoints); the tool never
auto-decides a path or attributes flow (Invariants #4/#5; the scope guard against auto-discovery). Adding a
hop advances the frontier; an edge already in the trace is never re-proposed. BTC next hops point at the
in-DB transaction that spends a terminal output (the link is still added as a basis-labeled claim).
"""

from __future__ import annotations

from backend.app.db import repository as repo
from backend.app.models import (
    Address, Asset, SourceQuery, Transaction, TraceBtcLink, Transfer, TxInput, TxOutput,
)
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.tracing import (
    add_trace_transfer, create_trace, trace_btc_links, trace_next_hops, trace_transfers,
)
from backend.tests.integration._helpers import new_case


def _seed_evm_chain(conn) -> dict:
    """X→A→B→C as three in-DB `transfer` facts (distinct txs). Returns transfer ids."""
    sq = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="txlist",
                     params={"bounds": "default"}, requested_at="2026-01-01T00:00:00Z", status="ok")
    ids: dict = {}

    def w(c, sqid):
        asset = repo.upsert_asset(c, Asset(chain="ethereum", symbol="ETH", decimals=18), sqid)
        addr = {n: repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + d * 40), sqid)
                for n, d in [("X", "1"), ("A", "2"), ("B", "3"), ("C", "4")]}

        def tr(hash_suffix, frm, to):
            tx = repo.upsert_transaction(c, Transaction(chain="ethereum", tx_hash="0x" + hash_suffix * 64,
                block_ts="2026-01-01T00:00:00Z", confirmations=100, finality_status="final"), sqid)
            return repo.upsert_transfer(c, Transfer(transaction_id=tx, chain="ethereum",
                from_address_id=addr[frm], to_address_id=addr[to], asset_id=asset,
                amount="1000000000000000000", transfer_type="native", position=0), sqid)

        ids["xa"] = tr("a", "X", "A")
        ids["ab"] = tr("b", "A", "B")
        ids["bc"] = tr("c", "B", "C")

    write_with_provenance(conn, sq, w)
    return ids


def test_lists_next_hops_without_auto_adding(tmp_path):
    conn, db = new_case(tmp_path, title="hops")
    ids = _seed_evm_chain(conn)
    trace_id = create_trace(conn, name="p")
    add_trace_transfer(conn, trace_id=trace_id, transfer_id=ids["xa"])  # trace X→A; terminal = A

    hops = trace_next_hops(conn, trace_id)
    # A→B is PROPOSED (a real in-DB fact leaving the terminal A)...
    assert any(h["transfer_id"] == ids["ab"] for h in hops["evm"])
    # ...but the tool ADDED NOTHING — the trace is unchanged (it proposes, never auto-adds).
    assert [t["transfer_id"] for t in trace_transfers(conn, trace_id)] == [ids["xa"]]
    # B→C is NOT proposed — B only becomes a frontier AFTER the user explicitly adds A→B (no path discovery).
    assert all(h["transfer_id"] != ids["bc"] for h in hops["evm"])


def test_added_hop_advances_frontier_and_is_not_reproposed(tmp_path):
    conn, db = new_case(tmp_path, title="frontier")
    ids = _seed_evm_chain(conn)
    trace_id = create_trace(conn, name="p")
    add_trace_transfer(conn, trace_id=trace_id, transfer_id=ids["xa"])
    add_trace_transfer(conn, trace_id=trace_id, transfer_id=ids["ab"])  # user picks A→B; frontier → B

    hop_ids = {h["transfer_id"] for h in trace_next_hops(conn, trace_id)["evm"]}
    assert ids["bc"] in hop_ids       # B→C now proposed (frontier advanced by the user's pick)
    assert ids["ab"] not in hop_ids   # already an edge — never re-proposed
    assert ids["xa"] not in hop_ids


def _seed_btc_terminal_spent(conn) -> dict:
    """A trace link source=OP → dest=O, where O is later SPENT by tx1 (in-DB). Returns ids + trace_id."""
    sq = SourceQuery(connector="esplora", capability="get_transactions", endpoint="tx",
                     params={"bounds": "default"}, requested_at="2026-01-01T00:00:00Z", status="ok")
    ids: dict = {}

    def w(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)
        addr = repo.upsert_address(c, Address(chain="bitcoin", address_display="bc1x"), sqid)
        txp = repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="p" * 64, block_height=1,
            block_ts="2026-01-01T00:00:00Z", confirmations=100, finality_status="final"), sqid)
        op = repo.upsert_tx_output(c, TxOutput(transaction_id=txp, address_id=addr, amount="100",
                                               output_index=0), sqid)
        tx0 = repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="0" * 64, block_height=2,
            block_ts="2026-01-02T00:00:00Z", confirmations=100, finality_status="final"), sqid)
        repo.upsert_tx_input(c, TxInput(transaction_id=tx0, prev_output_id=op, address_id=addr,
                                        amount="100", input_index=0), sqid)
        o = repo.upsert_tx_output(c, TxOutput(transaction_id=tx0, address_id=addr, amount="99",
                                              output_index=0), sqid)
        tx1 = repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="1" * 64, block_height=3,
            block_ts="2026-01-03T00:00:00Z", confirmations=100, finality_status="final"), sqid)
        repo.upsert_tx_input(c, TxInput(transaction_id=tx1, prev_output_id=o, address_id=addr,
                                        amount="99", input_index=0), sqid)
        ids.update(op=op, o=o, tx0=tx0, tx1=tx1)

    write_with_provenance(conn, sq, w)
    # O is spent by tx1 (the spend linkage the next-hop query keys on).
    conn.execute("UPDATE tx_output SET spent=1, spending_tx_id=? WHERE id=?", (ids["tx1"], ids["o"]))
    trace_id = create_trace(conn, name="btc")
    ids["link"] = repo.insert_trace_btc_link(conn, TraceBtcLink(trace_id=trace_id, transaction_id=ids["tx0"],
        source_output_id=ids["op"], dest_output_id=ids["o"], basis="investigator", ordering=0))
    ids["trace_id"] = trace_id
    return ids


def test_btc_next_hop_points_at_spending_tx(tmp_path):
    conn, db = new_case(tmp_path, title="btc hop")
    ids = _seed_btc_terminal_spent(conn)
    hops = trace_next_hops(conn, ids["trace_id"])
    # terminal output O is spent by tx1 → the proposed next hop is that spending tx (still added as a claim).
    assert any(h["source_output_id"] == ids["o"] and h["spending_tx_id"] == ids["tx1"] for h in hops["btc"])
    # nothing was added — the trace still has exactly its one link.
    assert len(trace_btc_links(conn, ids["trace_id"])) == 1
