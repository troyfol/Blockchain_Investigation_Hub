"""Golden FIFO trace (phase_08): apportionment vs a hand-computed expected, audits green.

Funding tx0 pays two outputs (60, 40 sat). Spending tx1 consumes both as inputs and pays
(70, 25 sat) — fee 5. FIFO (ledger order) apportions:

    O0a(60) -> O1a : 60        # O0a fully into the first output
    O0b(40) -> O1a : 10        # O0b tops up the first output
    O0b(40) -> O1b : 25        # remaining O0b funds the second output; 5 left = fee

So three fifo links; into O1a == 70, into O1b == 25; out of O0a == 60, out of O0b == 35 (<= 40).
"""

from __future__ import annotations

import pytest

from backend.app.audits.runner import run_audits
from backend.app.db import repository as repo
from backend.app.models import Address, Asset, SourceQuery, Transaction, TxInput, TxOutput
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.tracing import (
    add_manual_link,
    create_trace,
    fifo_trace_transaction,
    trace_btc_links,
)
from backend.tests.integration._helpers import new_case


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="FIFO")
    yield conn, db
    conn.close()


def _seed_chain(conn):
    """Seed funding tx0 (outputs 60,40) and spending tx1 (inputs spend them; outputs 70,25)."""
    sq = SourceQuery(connector="esplora", capability="get_transactions", endpoint="address-txs",
                     params={"address": "probe", "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")
    ids = {}

    def write(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)

        def addr(name):
            return repo.upsert_address(c, Address(chain="bitcoin", address_display=name), sqid)

        tx0 = repo.upsert_transaction(c, Transaction(
            chain="bitcoin", tx_hash="0" * 64, block_height=799999, block_ts="2026-01-01T00:00:00Z",
            confirmations=20, finality_status="final"), sqid)
        o0a = repo.upsert_tx_output(c, TxOutput(transaction_id=tx0, address_id=addr("A"), amount="60", output_index=0), sqid)
        o0b = repo.upsert_tx_output(c, TxOutput(transaction_id=tx0, address_id=addr("B"), amount="40", output_index=1), sqid)

        tx1 = repo.upsert_transaction(c, Transaction(
            chain="bitcoin", tx_hash="1" * 64, block_height=800000, block_ts="2026-01-01T01:00:00Z",
            fee="5", confirmations=19, finality_status="final"), sqid)
        repo.upsert_tx_input(c, TxInput(transaction_id=tx1, prev_output_id=o0a, address_id=addr("A"), amount="60", input_index=0), sqid)
        repo.upsert_tx_input(c, TxInput(transaction_id=tx1, prev_output_id=o0b, address_id=addr("B"), amount="40", input_index=1), sqid)
        o1a = repo.upsert_tx_output(c, TxOutput(transaction_id=tx1, address_id=addr("C"), amount="70", output_index=0), sqid)
        o1b = repo.upsert_tx_output(c, TxOutput(transaction_id=tx1, address_id=addr("D"), amount="25", output_index=1), sqid)
        ids.update(tx1=tx1, o0a=o0a, o0b=o0b, o1a=o1a, o1b=o1b)

    write_with_provenance(conn, sq, write)
    return ids


@pytest.mark.smoke
def test_fifo_trace_matches_hand_computed(case):
    conn, db = case
    ids = _seed_chain(conn)
    trace_id = create_trace(conn, name="Stolen funds", description="FIFO from tx0")
    stats = fifo_trace_transaction(conn, trace_id=trace_id, transaction_id=ids["tx1"])
    assert stats == {"links_written": 3, "unresolved": 0, "next_ordering": 3}

    links = trace_btc_links(conn, trace_id)
    triples = [(l["source_output_id"], l["dest_output_id"], l["note"]) for l in links]
    assert triples == [
        (ids["o0a"], ids["o1a"], "fifo apportioned 60 sat"),
        (ids["o0b"], ids["o1a"], "fifo apportioned 10 sat"),
        (ids["o0b"], ids["o1b"], "fifo apportioned 25 sat"),
    ]
    assert all(l["basis"] == "fifo" and l["is_convention"] for l in links)  # labeled convention, not flow

    # Conservation, read back from the apportioned amounts.
    def amt(note):
        return int(note.split()[2])
    into_o1a = sum(amt(l["note"]) for l in links if l["dest_output_id"] == ids["o1a"])
    into_o1b = sum(amt(l["note"]) for l in links if l["dest_output_id"] == ids["o1b"])
    assert into_o1a == 70 and into_o1b == 25

    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_graph_trace_edges_carry_staggered_labels(case):
    """The read-model surfaces each FIFO link as a trace edge with a per-edge `label_dy` so adjacent
    trace labels don't stack at a hub (the 'two FIFO labels overlapping near the anchor' nit)."""
    from backend.app.services.graph import build_graph

    conn, _ = case
    ids = _seed_chain(conn)
    trace_id = create_trace(conn, name="Stolen funds")
    fifo_trace_transaction(conn, trace_id=trace_id, transaction_id=ids["tx1"])

    g = build_graph(conn)
    trace_edges = [e for e in g["edges"] if e["kind"] == "trace"]
    assert len(trace_edges) == 3
    # Every trace edge carries an integer vertical label offset.
    assert all(isinstance(e.get("label_dy"), int) for e in trace_edges)
    # The two links sharing the same source→dest-ish hub get DIFFERENT offsets (staggered, not stacked).
    assert trace_edges[0]["label_dy"] != trace_edges[1]["label_dy"]


def test_trace_label_surfaces_on_edges_report_and_api(case):
    """Feature 5: a trace's display name = its name, overridden by the investigator's latest custom
    label. It rides on every trace edge (`trace_name`), the report's trace section, and /api/traces."""
    from fastapi.testclient import TestClient

    from backend.app.main import app, get_case_db_path
    from backend.app.services.graph import build_graph
    from backend.app.services.investigator import set_label
    from backend.app.services.reporting import _collect_traces

    conn, db = case
    ids = _seed_chain(conn)
    trace_id = create_trace(conn, name="Stolen funds")
    fifo_trace_transaction(conn, trace_id=trace_id, transaction_id=ids["tx1"])

    # Default display name = the trace's name; every trace edge carries it + its trace_id.
    te0 = [e for e in build_graph(conn)["edges"] if e["kind"] == "trace"]
    assert te0 and all(e["trace_name"] == "Stolen funds" and e["trace_id"] == trace_id for e in te0)

    # Rename the path -> graph edges + report + API all reflect the custom label.
    set_label(conn, target_type="trace", target_id=trace_id, label="Hop to Garantex")
    assert all(e["trace_name"] == "Hop to Garantex"
               for e in build_graph(conn)["edges"] if e["kind"] == "trace")
    assert _collect_traces(conn)[0]["name"] == "Hop to Garantex"        # report uses the display name

    app.dependency_overrides[get_case_db_path] = lambda: str(db)
    try:
        c = TestClient(app)
        traces = c.get("/api/traces").json()["traces"]
        assert len(traces) == 1
        assert traces[0]["name"] == "Hop to Garantex" and traces[0]["original_name"] == "Stolen funds"
        assert traces[0]["custom_label"] is True and traces[0]["btc_link_count"] == 3

        r = c.post(f"/api/trace/{trace_id}/label", json={"label": "Final beneficiary"})
        assert r.status_code == 200 and "graph" not in r.json()  # EFF-01: client refetches the view
        assert all(e["trace_name"] == "Final beneficiary"
                   for e in c.get("/api/graph").json()["edges"] if e["kind"] == "trace")
        assert c.post("/api/trace/ghost/label", json={"label": "x"}).status_code == 404
    finally:
        app.dependency_overrides.clear()

    assert all(r.passed for r in run_audits(db_path=str(db)))   # labels are audit-clean (Family C)


def test_manual_override_is_investigator_basis(case):
    conn, db = case
    ids = _seed_chain(conn)
    trace_id = create_trace(conn, name="Manual")
    add_manual_link(conn, trace_id=trace_id, transaction_id=ids["tx1"],
                    source_output_id=ids["o0a"], dest_output_id=ids["o1b"],
                    confidence=0.8, note="investigator believes A funded D")
    links = trace_btc_links(conn, trace_id)
    assert len(links) == 1
    assert links[0]["basis"] == "investigator" and not links[0]["is_convention"]
    assert links[0]["confidence"] == 0.8
    # FIFO never overwrote a fact: the underlying tx_output rows are unchanged facts.
    assert conn.execute("SELECT amount FROM tx_output WHERE id=?", (ids["o1b"],)).fetchone()["amount"] == "25"
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_manual_link_must_stay_within_the_transaction(case):
    conn, db = case
    ids = _seed_chain(conn)
    # A second, unrelated tx with its own output.
    sq = SourceQuery(connector="esplora", capability="get_transactions", endpoint="address-txs",
                     params={"address": "probe", "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")
    other = {}

    def write(c, sqid):
        tx2 = repo.upsert_transaction(c, Transaction(
            chain="bitcoin", tx_hash="7" * 64, block_height=800002, block_ts="2026-01-01T03:00:00Z",
            confirmations=10, finality_status="final"), sqid)
        a = repo.upsert_address(c, Address(chain="bitcoin", address_display="Z"), sqid)
        other["o2"] = repo.upsert_tx_output(c, TxOutput(transaction_id=tx2, address_id=a, amount="5", output_index=0), sqid)

    write_with_provenance(conn, sq, write)
    trace_id = create_trace(conn, name="cross-tx")

    # dest_output from a DIFFERENT transaction -> rejected (Invariant #5 coherence).
    with pytest.raises(ValueError):
        add_manual_link(conn, trace_id=trace_id, transaction_id=ids["tx1"],
                        source_output_id=ids["o0a"], dest_output_id=other["o2"])
    # source_output not spent by this transaction -> rejected.
    with pytest.raises(ValueError):
        add_manual_link(conn, trace_id=trace_id, transaction_id=ids["tx1"],
                        source_output_id=other["o2"], dest_output_id=ids["o1a"])
    # nonexistent transaction -> rejected.
    with pytest.raises(ValueError):
        add_manual_link(conn, trace_id=trace_id, transaction_id="ghost",
                        source_output_id=ids["o0a"], dest_output_id=ids["o1a"])
    assert trace_btc_links(conn, trace_id) == []  # nothing was written


def test_unresolved_input_is_reported_not_guessed(case):
    conn, db = case
    # A spending tx whose input's prev output is NOT in-DB -> the link cannot be anchored.
    sq = SourceQuery(connector="esplora", capability="get_transactions", endpoint="address-txs",
                     params={"address": "probe", "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")
    holder = {}

    def write(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)
        tx = repo.upsert_transaction(c, Transaction(
            chain="bitcoin", tx_hash="2" * 64, block_height=800001, block_ts="2026-01-01T02:00:00Z",
            confirmations=18, finality_status="final"), sqid)
        addr = repo.upsert_address(c, Address(chain="bitcoin", address_display="X"), sqid)
        repo.upsert_tx_input(c, TxInput(transaction_id=tx, prev_output_id=None, address_id=addr,
                                        amount="100", input_index=0), sqid)
        repo.upsert_tx_output(c, TxOutput(transaction_id=tx, address_id=addr, amount="90", output_index=0), sqid)
        holder["tx"] = tx

    write_with_provenance(conn, sq, write)
    trace_id = create_trace(conn, name="Unresolved")
    stats = fifo_trace_transaction(conn, trace_id=trace_id, transaction_id=holder["tx"])
    assert stats["links_written"] == 0 and stats["unresolved"] == 1
    assert trace_btc_links(conn, trace_id) == []  # nothing fabricated
    assert all(r.passed for r in run_audits(db_path=str(db)))
