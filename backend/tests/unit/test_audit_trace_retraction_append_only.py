"""P9 / FN-04 — the `trace-retraction-append-only` audit.

A trace edge/link is RETRACTED, never deleted. This cross-run audit baselines the retraction tables and
fails if a baselined retraction row disappears (deletion — an attempt to silently un-retract) or is
rewritten. Adding MORE retractions is normal append-only growth, not a regression. Mirrors the
`append-only-claims` audit; also proves the audit is registered (count 10→11).
"""

from __future__ import annotations

from backend.app.audits.runner import run_audits
from backend.app.services.tracing import add_trace_transfer, create_trace, retract_trace_transfer
from backend.tests.integration._helpers import new_case, seed_evm_address


def _result(results, name):
    return next(r for r in results if r.name == name)


def _seed_retracted_edge(conn, addr="0x" + "ab" * 20):
    seed_evm_address(conn, addr)
    transfer_id = conn.execute("SELECT id FROM transfer").fetchone()["id"]
    trace_id = create_trace(conn, name="p")
    tt_id = add_trace_transfer(conn, trace_id=trace_id, transfer_id=transfer_id)
    retract_trace_transfer(conn, trace_transfer_id=tt_id, reason="wrong")
    return trace_id, transfer_id


def test_audit_registered_and_passes(tmp_path):
    conn, db = new_case(tmp_path, title="A")
    _seed_retracted_edge(conn)
    results = run_audits(db_path=str(db))
    assert "trace-retraction-append-only" in {r.name for r in results}  # count 10→11
    assert _result(results, "trace-retraction-append-only").passed  # first run records the baseline
    assert all(r.passed for r in results)


def test_audit_detects_deleted_retraction(tmp_path):
    conn, db = new_case(tmp_path, title="A")
    _seed_retracted_edge(conn)
    assert _result(run_audits(db_path=str(db)), "trace-retraction-append-only").passed  # baseline

    conn.execute("DELETE FROM trace_transfer_retraction")  # tamper: silently un-retract by deletion
    r = _result(run_audits(db_path=str(db)), "trace-retraction-append-only")
    assert not r.passed
    assert any("deleted" in o.get("reason", "") for o in r.offending)


def test_audit_allows_more_retractions(tmp_path):
    conn, db = new_case(tmp_path, title="A")
    trace_id, transfer_id = _seed_retracted_edge(conn)
    assert _result(run_audits(db_path=str(db)), "trace-retraction-append-only").passed  # baseline

    # Re-add (fresh edge) then retract it too — append-only GROWTH, must stay green.
    tt2 = add_trace_transfer(conn, trace_id=trace_id, transfer_id=transfer_id)
    retract_trace_transfer(conn, trace_transfer_id=tt2, reason="again")
    assert _result(run_audits(db_path=str(db)), "trace-retraction-append-only").passed
