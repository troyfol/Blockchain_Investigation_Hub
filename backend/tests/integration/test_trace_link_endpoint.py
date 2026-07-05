"""P10 / UX-06 — manual BTC trace link: within-tx guard + link candidates + trace annotations in report.

A manual `basis='investigator'` BTC link is an apportionment WITHIN ONE transaction — `source_output_id`
must be a prev-output the tx actually spends, `dest_output_id` an output of that same tx. The endpoint
(via `add_manual_link`) rejects anything cross-tx (Invariant #5 — never fabricate a cross-tx edge). The new
`btc_link_candidates` endpoint surfaces exactly those two legal option sets so the UI can offer pickers.
Trace annotations (a `trace` target, already accepted by the generic annotation endpoint) appear in the
report's investigator-notes appendix, grouped under the trace's display name.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.app.db import repository as repo
from backend.app.main import app, get_case_db_path
from backend.app.models import Address, Asset, SourceQuery, Transaction, TxInput, TxOutput
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.investigator import add_annotation, collect_notes
from backend.app.services.tracing import create_trace
from backend.tests.integration._helpers import new_case


def _seed_btc_two_tx(conn) -> dict:
    """tx0 funds output O0; tx1 SPENDS O0 (input.prev_output_id=O0) and has outputs O1a/O1b. So within tx1
    the only legal manual link is source=O0 → dest∈{O1a,O1b}. Returns the ids."""
    sq = SourceQuery(connector="esplora", capability="get_transactions", endpoint="tx",
                     params={"bounds": "default"}, requested_at="2026-01-01T00:00:00Z", status="ok")
    ids: dict = {}

    def w(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)
        src = repo.upsert_address(c, Address(chain="bitcoin", address_display="bc1src"), sqid)
        dst = repo.upsert_address(c, Address(chain="bitcoin", address_display="bc1dst"), sqid)
        tx0 = repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="t0" * 32, block_height=1,
            block_ts="2026-01-01T00:00:00Z", confirmations=100, finality_status="final"), sqid)
        o0 = repo.upsert_tx_output(c, TxOutput(transaction_id=tx0, address_id=src, amount="100",
                                               output_index=0), sqid)
        tx1 = repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="t1" * 32, block_height=2,
            block_ts="2026-01-02T00:00:00Z", confirmations=100, finality_status="final"), sqid)
        repo.upsert_tx_input(c, TxInput(transaction_id=tx1, prev_output_id=o0, address_id=src, amount="100",
                                        input_index=0), sqid)
        o1a = repo.upsert_tx_output(c, TxOutput(transaction_id=tx1, address_id=dst, amount="60",
                                                output_index=0), sqid)
        o1b = repo.upsert_tx_output(c, TxOutput(transaction_id=tx1, address_id=dst, amount="39",
                                                output_index=1), sqid)
        ids.update(tx0=tx0, tx1=tx1, o0=o0, o1a=o1a, o1b=o1b)

    write_with_provenance(conn, sq, w)
    return ids


def _client(db) -> TestClient:
    app.dependency_overrides[get_case_db_path] = lambda: str(db)
    return TestClient(app)


def test_btc_link_candidates_lists_sources_and_dests(tmp_path):
    conn, db = new_case(tmp_path, title="candidates")
    ids = _seed_btc_two_tx(conn)
    conn.close()
    c = _client(db)
    try:
        r = c.get(f"/api/transaction/{ids['tx1']}/btc_link_candidates")
        assert r.status_code == 200, r.text
        body = r.json()
        # sources = the prev-outputs tx1 SPENDS; dests = tx1's OWN outputs — the only within-tx legal options.
        assert {s["id"] for s in body["sources"]} == {ids["o0"]}
        assert {d["id"] for d in body["dests"]} == {ids["o1a"], ids["o1b"]}
        assert all("label" in s for s in body["sources"])  # each candidate carries a human label for the UI
    finally:
        app.dependency_overrides.clear()


def test_manual_link_within_tx_only(tmp_path):
    conn, db = new_case(tmp_path, title="within-tx")
    ids = _seed_btc_two_tx(conn)
    trace_id = create_trace(conn, name="p")
    conn.close()
    c = _client(db)
    try:
        # LEGAL: source=O0 (spent by tx1's input) → dest=O1a (an output of tx1).
        ok = c.post(f"/api/trace/{trace_id}/link", json={
            "transaction_id": ids["tx1"], "source_output_id": ids["o0"], "dest_output_id": ids["o1a"]})
        assert ok.status_code == 200 and ok.json()["ok"]

        # CROSS-TX (dest is an output of tx0, not tx1) → rejected: can't fabricate a cross-tx edge (Inv #5).
        bad_dest = c.post(f"/api/trace/{trace_id}/link", json={
            "transaction_id": ids["tx1"], "source_output_id": ids["o0"], "dest_output_id": ids["o0"]})
        assert bad_dest.status_code == 400

        # source NOT spent by tx1 (O1b is one of tx1's own outputs, not a prev-output it spends) → rejected.
        bad_src = c.post(f"/api/trace/{trace_id}/link", json={
            "transaction_id": ids["tx1"], "source_output_id": ids["o1b"], "dest_output_id": ids["o1a"]})
        assert bad_src.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_trace_annotation_shown_in_report(tmp_path):
    conn, db = new_case(tmp_path, title="trace note")
    trace_id = create_trace(conn, name="Lazarus hop")
    add_annotation(conn, target_type="trace", target_id=trace_id, content="suspicious peel chain")

    # collect_notes IS the report's investigator-notes source (reporting._collect_notes wraps it).
    groups = collect_notes(conn)
    trace_groups = [g for g in groups if g["target_type"] == "trace" and g["target_id"] == trace_id]
    assert len(trace_groups) == 1
    g = trace_groups[0]
    assert any(a["content"] == "suspicious peel chain" for a in g["annotations"])
    assert g["label"] == "Lazarus hop"  # grouped under the trace's readable display name
    conn.close()
