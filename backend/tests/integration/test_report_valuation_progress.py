"""P8.7.3 #4 — a report generated while a background valuation pass is RUNNING says so explicitly
(partial USD coverage), instead of silently freezing a half-priced snapshot."""

from __future__ import annotations

from backend.app.services import jobs, reporting
from backend.tests.integration._helpers import new_case, seed_btc_custom


def test_report_notes_valuation_in_progress(tmp_path):
    conn, _db = new_case(tmp_path, title="Valuing")
    seed_btc_custom(conn, txid="a" * 64, input_addrs=["bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"],
                    output_amounts=[1000])

    # No active valuation job -> no in-progress note (just the honest coverage line).
    v = reporting._valuation_honesty(conn)
    assert v["in_progress"] is False
    assert "Valuation in progress" not in reporting._valuation_section(v)

    # A running valuation job at generation -> the section explicitly notes partial coverage.
    jobs.start("valuation")
    v2 = reporting._valuation_honesty(conn)
    assert v2["in_progress"] is True
    section = reporting._valuation_section(v2)
    assert "Valuation in progress at generation" in section
    assert "re-generate after valuation completes" in section

    # A finished/other job does not trigger the note.
    jobs.active().finish()
    assert reporting._valuation_honesty(conn)["in_progress"] is False
