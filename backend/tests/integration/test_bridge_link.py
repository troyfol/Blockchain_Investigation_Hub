"""P12 / FN-17 — manual cross-chain bridge link (a labeled investigator CLAIM, never a fact).

A bridge crossing (outflow to a bridge on chain A ↔ inflow from the bridge on chain B) is asserted by the
investigator as a `basis='investigator'` link INSIDE a trace — connecting two real value movements across
chains. It is NEVER a synthesized `transfer`/ledger fact (Invariant #5) and never collapses the two sides
(Invariant #4). It must cross chains (a same-chain link is not a bridge). Automated bridge detection stays
rejected (RJ-02) — manual assertion only. Rendered as an investigator-basis link in panel + report.
"""

from __future__ import annotations

import pytest

from backend.app.audits.runner import run_audits
from backend.app.db import repository as repo
from backend.app.models import Address, Asset, SourceQuery, Transaction, Transfer, TxOutput
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.reporting import _collect_traces
from backend.app.services.tracing import add_bridge_link, create_trace, trace_bridge_links
from backend.tests.integration._helpers import new_case


def _seed_two_chains(conn) -> dict:
    """An ethereum `transfer` (chain-A outflow) + a bitcoin `tx_output` (chain-B inflow). Returns ids."""
    sq = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="txlist",
                     params={"bounds": "default"}, requested_at="2026-01-01T00:00:00Z", status="ok")
    ids: dict = {}

    def w(c, sqid):
        eth_asset = repo.upsert_asset(c, Asset(chain="ethereum", symbol="ETH", decimals=18), sqid)
        a = repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + "1" * 40), sqid)
        bridge = repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + "2" * 40), sqid)
        eth_tx = repo.upsert_transaction(c, Transaction(chain="ethereum", tx_hash="0x" + "e" * 64,
            block_ts="2026-01-01T00:00:00Z", confirmations=100, finality_status="final"), sqid)
        ids["eth_transfer"] = repo.upsert_transfer(c, Transfer(transaction_id=eth_tx, chain="ethereum",
            from_address_id=a, to_address_id=bridge, asset_id=eth_asset, amount="1000000000000000000",
            transfer_type="native", position=0), sqid)

        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)
        recipient = repo.upsert_address(c, Address(chain="bitcoin", address_display="bc1recv"), sqid)
        btc_tx = repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="b" * 64, block_height=1,
            block_ts="2026-01-02T00:00:00Z", confirmations=100, finality_status="final"), sqid)
        ids["btc_output"] = repo.upsert_tx_output(c, TxOutput(transaction_id=btc_tx, address_id=recipient,
            amount="100", output_index=0), sqid)
        # a SECOND ethereum transfer, for the same-chain rejection test
        b2 = repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + "3" * 40), sqid)
        eth_tx2 = repo.upsert_transaction(c, Transaction(chain="ethereum", tx_hash="0x" + "f" * 64,
            block_ts="2026-01-01T00:00:00Z", confirmations=100, finality_status="final"), sqid)
        ids["eth_transfer2"] = repo.upsert_transfer(c, Transfer(transaction_id=eth_tx2, chain="ethereum",
            from_address_id=bridge, to_address_id=b2, asset_id=eth_asset, amount="1000000000000000000",
            transfer_type="native", position=0), sqid)

    write_with_provenance(conn, sq, w)
    return ids


def test_cross_chain_link_is_claim_not_fact(tmp_path):
    conn, db = new_case(tmp_path, title="bridge")
    ids = _seed_two_chains(conn)
    trace_id = create_trace(conn, name="cross-chain hop")
    transfers_before = conn.execute("SELECT COUNT(*) FROM transfer").fetchone()[0]

    link_id = add_bridge_link(conn, trace_id=trace_id,
        src_subject_type="transfer", src_subject_id=ids["eth_transfer"],
        dst_subject_type="tx_output", dst_subject_id=ids["btc_output"], note="Wormhole ETH→BTC")

    row = conn.execute("SELECT * FROM trace_bridge_link WHERE id=?", (link_id,)).fetchone()
    assert row["basis"] == "investigator"  # a labeled investigator CLAIM
    # it is NOT a ledger fact: no `transfer` row was synthesized to represent the crossing (Invariant #5).
    assert conn.execute("SELECT COUNT(*) FROM transfer").fetchone()[0] == transfers_before
    # every audit holds — incl. no-fabricated-utxo-edge (no input→output transfer) + no-dangling-fk (poly-ref).
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_same_chain_link_is_rejected(tmp_path):
    conn, _ = new_case(tmp_path, title="bridge")
    ids = _seed_two_chains(conn)
    trace_id = create_trace(conn, name="p")
    with pytest.raises(ValueError, match="cross chains"):
        add_bridge_link(conn, trace_id=trace_id,
            src_subject_type="transfer", src_subject_id=ids["eth_transfer"],
            dst_subject_type="transfer", dst_subject_id=ids["eth_transfer2"])  # both ethereum → not a bridge


def test_bridge_link_renders_with_basis_and_chains(tmp_path):
    conn, _ = new_case(tmp_path, title="bridge")
    ids = _seed_two_chains(conn)
    trace_id = create_trace(conn, name="MyBridge")
    add_bridge_link(conn, trace_id=trace_id,
        src_subject_type="transfer", src_subject_id=ids["eth_transfer"],
        dst_subject_type="tx_output", dst_subject_id=ids["btc_output"], note="Wormhole")

    links = trace_bridge_links(conn, trace_id)
    assert len(links) == 1
    assert links[0]["basis"] == "investigator"
    assert {links[0]["src_chain"], links[0]["dst_chain"]} == {"ethereum", "bitcoin"}
    # the report's per-trace context carries the bridge link (rendered in panel + report).
    trace_ctx = next(t for t in _collect_traces(conn) if t["name"] == "MyBridge")
    assert trace_ctx["bridge_links"] and trace_ctx["bridge_links"][0]["note"] == "Wormhole"


def test_dangling_bridge_subject_is_caught_by_audit(tmp_path):
    conn, db = new_case(tmp_path, title="bridge")
    ids = _seed_two_chains(conn)
    trace_id = create_trace(conn, name="p")
    link_id = add_bridge_link(conn, trace_id=trace_id,
        src_subject_type="transfer", src_subject_id=ids["eth_transfer"],
        dst_subject_type="tx_output", dst_subject_id=ids["btc_output"])
    assert next(r for r in run_audits(db_path=str(db)) if r.name == "no-dangling-fk").passed  # baseline

    # tamper: repoint the dst at a non-existent movement — no-dangling-fk must catch the poly-ref.
    conn.execute("UPDATE trace_bridge_link SET dst_subject_id='ghost' WHERE id=?", (link_id,))
    r = next(r for r in run_audits(db_path=str(db)) if r.name == "no-dangling-fk")
    assert not r.passed
    assert any(o.get("kind") == "trace_bridge_link.dst_subject_id" for o in r.offending)
