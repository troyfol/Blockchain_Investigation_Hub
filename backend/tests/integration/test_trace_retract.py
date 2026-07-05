"""P9 / FN-04 — trace edit / retract (append-only).

An investigator can RETRACT a specific trace edge (EVM `trace_transfer`) or link (BTC `trace_btc_link`):
the edge is excluded from the effective trace, the graph, and the report — but the edge row AND an
append-only retraction row PERSIST (nothing is deleted). Re-adding the same edge after a retract works:
it produces a FRESH active edge (the retracted row stays retracted, untouched). Idempotent: a second
retract of the same edge is a no-op. All invariant audits (incl. the new retraction-append-only one) hold.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.app.audits.runner import run_audits
from backend.app.db import repository as repo
from backend.app.main import app, get_case_db_path
from backend.app.models import TraceBtcLink
from backend.app.services.reporting import _collect_traces
from backend.app.services.tracing import (
    add_trace_transfer,
    create_trace,
    retract_trace_btc_link,
    retract_trace_transfer,
    trace_btc_links,
    trace_transfers,
)
from backend.tests.integration._helpers import new_case, seed_btc_custom, seed_evm_address


def test_retract_excludes_edge_keeps_history(tmp_path):
    conn, db = new_case(tmp_path, title="Trace")
    seed_evm_address(conn, "0x" + "ab" * 20)  # creates exactly one transfer
    transfer_id = conn.execute("SELECT id FROM transfer").fetchone()["id"]
    trace_id = create_trace(conn, name="path")
    tt_id = add_trace_transfer(conn, trace_id=trace_id, transfer_id=transfer_id)
    assert len(trace_transfers(conn, trace_id)) == 1  # effective: one edge

    retract_trace_transfer(conn, trace_transfer_id=tt_id, reason="wrong hop")
    # excluded from the effective trace + report, but the edge row + retraction PERSIST (nothing deleted).
    assert trace_transfers(conn, trace_id) == []
    assert conn.execute("SELECT COUNT(*) FROM trace_transfer").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM trace_transfer_retraction").fetchone()[0] == 1
    assert _collect_traces(conn)[0]["transfers"] == []  # the report excludes the retracted edge

    # re-adding after retract works → a FRESH active edge (the retracted row is left untouched).
    tt_id2 = add_trace_transfer(conn, trace_id=trace_id, transfer_id=transfer_id)
    assert tt_id2 != tt_id
    assert len(trace_transfers(conn, trace_id)) == 1
    assert conn.execute("SELECT COUNT(*) FROM trace_transfer").fetchone()[0] == 2  # both rows kept
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_retract_trace_btc_link_excluded(tmp_path):
    conn, db = new_case(tmp_path, title="Trace BTC")
    tx_id = seed_btc_custom(conn, txid="a" * 64, input_addrs=["bc1in"], output_amounts=[60, 40])
    outs = [r["id"] for r in conn.execute(
        "SELECT id FROM tx_output WHERE transaction_id=? ORDER BY output_index", (tx_id,)).fetchall()]
    trace_id = create_trace(conn, name="btc path")
    link_id = repo.insert_trace_btc_link(conn, TraceBtcLink(
        trace_id=trace_id, transaction_id=tx_id, source_output_id=outs[0], dest_output_id=outs[1],
        basis="investigator", ordering=0))
    assert len(trace_btc_links(conn, trace_id)) == 1

    retract_trace_btc_link(conn, trace_btc_link_id=link_id, reason="mislink")
    assert trace_btc_links(conn, trace_id) == []  # excluded from the read path (report + graph reuse it)
    assert conn.execute("SELECT COUNT(*) FROM trace_btc_link").fetchone()[0] == 1  # not deleted
    assert conn.execute("SELECT COUNT(*) FROM trace_btc_link_retraction").fetchone()[0] == 1
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_retract_is_idempotent(tmp_path):
    conn, _ = new_case(tmp_path, title="Trace")
    seed_evm_address(conn, "0x" + "cd" * 20)
    transfer_id = conn.execute("SELECT id FROM transfer").fetchone()["id"]
    trace_id = create_trace(conn, name="p")
    tt_id = add_trace_transfer(conn, trace_id=trace_id, transfer_id=transfer_id)

    r1 = retract_trace_transfer(conn, trace_transfer_id=tt_id, reason="x")
    r2 = retract_trace_transfer(conn, trace_transfer_id=tt_id, reason="y")  # already retracted → no-op
    assert r1 == r2
    assert conn.execute("SELECT COUNT(*) FROM trace_transfer_retraction").fetchone()[0] == 1


def test_retract_unknown_edge_raises(tmp_path):
    conn, _ = new_case(tmp_path, title="Trace")
    try:
        retract_trace_transfer(conn, trace_transfer_id="does-not-exist", reason="x")
        raise AssertionError("expected ValueError for an unknown trace_transfer")
    except ValueError:
        pass


def test_retract_whole_trace_soft_deletes_and_keeps_history(tmp_path):
    """v1.3.1 — retracting a WHOLE trace withdraws it from the effective views (report / list) while the
    trace row + its edges PERSIST (append-only). Idempotent; every audit (incl. trace-retraction-append-only)
    still holds."""
    from backend.app.services.tracing import retract_trace

    conn, db = new_case(tmp_path, title="Whole")
    seed_evm_address(conn, "0x" + "ef" * 20)
    transfer_id = conn.execute("SELECT id FROM transfer").fetchone()["id"]
    trace_id = create_trace(conn, name="doomed path")
    add_trace_transfer(conn, trace_id=trace_id, transfer_id=transfer_id)
    assert len(_collect_traces(conn)) == 1  # the report lists the trace

    rid = retract_trace(conn, trace_id=trace_id, reason="built by mistake")
    # withdrawn from the report/list, but the trace + its edge + the retraction ALL persist (nothing deleted).
    assert _collect_traces(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM trace").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM trace_transfer").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM trace_retraction").fetchone()[0] == 1

    # idempotent — a second retract of the same trace returns the same id (no duplicate row).
    assert retract_trace(conn, trace_id=trace_id, reason="again") == rid
    assert conn.execute("SELECT COUNT(*) FROM trace_retraction").fetchone()[0] == 1
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_retract_unknown_trace_raises(tmp_path):
    from backend.app.services.tracing import retract_trace

    conn, _ = new_case(tmp_path, title="Whole")
    try:
        retract_trace(conn, trace_id="does-not-exist", reason="x")
        raise AssertionError("expected ValueError for an unknown trace")
    except ValueError:
        pass


def test_retract_trace_endpoint(tmp_path):
    """v1.3.1 — POST /api/trace/{id}/retract: soft-deletes (the trace leaves /api/traces), requires a reason
    (400), and 404s an unknown trace."""
    conn, db = new_case(tmp_path, title="ep")
    trace_id = create_trace(conn, name="to remove")
    app.dependency_overrides[get_case_db_path] = lambda: str(db)
    try:
        c = TestClient(app)
        assert len(c.get("/api/traces").json()["traces"]) == 1
        assert c.post(f"/api/trace/{trace_id}/retract", json={"reason": "  "}).status_code == 400  # reason required
        assert c.post("/api/trace/does-not-exist/retract", json={"reason": "x"}).status_code == 404  # unknown
        r = c.post(f"/api/trace/{trace_id}/retract", json={"reason": "built by mistake"})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert c.get("/api/traces").json()["traces"] == []  # gone from the list after soft-delete
    finally:
        app.dependency_overrides.clear()
