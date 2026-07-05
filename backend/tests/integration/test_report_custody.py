"""P2 / FN-02 — the chain-of-custody report appendix.

A court-ready case must let a reader trace every exhibit back to the exact query that acquired it
(Invariant #3). The report now carries a **Chain of custody** appendix enumerating every ``source_query``
in the case — connector, capability, endpoint, params/bounds, retrieval time, the FULL (untruncated)
raw-response SHA-256 for tamper-checking, and the count of fact/claim rows it produced — in a
deterministic order, so an unchanged case re-renders byte-identically (the report's ``content_hash``
stays stable). Read-only surfacing of the provenance spine; no invariant is at risk.
"""

from __future__ import annotations

from backend.app.db import repository as repo
from backend.app.models import Address, Attribution, SourceQuery
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.reporting import generate_report
from backend.tests.integration._helpers import new_case

ADDR = "0x52908400098527886e0f7030069857d2e4169ee7"  # canonical (lowercase)


def _custody_slice(html: str) -> str:
    """The Chain-of-custody appendix, sliced out of the rendered report (heading → footer)."""
    start = html.index("<h2>Chain of custody</h2>")
    end = html.find('<div class="footer">', start)
    return html[start:(end if end != -1 else len(html))]


def _seed_two_queries(tmp_path):
    """Two distinct source_queries (different connectors), each with a captured raw response so each has a
    real 64-char ``raw_response_hash``."""
    conn, db = new_case(tmp_path, title="Custody")

    sq1 = SourceQuery(connector="graphsense", capability="get_attributions", endpoint="tagpack",
                      params={"address": ADDR, "bounds": "default"},
                      requested_at="2026-02-01T00:00:00Z", status="ok", result_summary="1 attribution")

    def w1(c, sqid):
        aid = repo.upsert_address(c, Address(chain="ethereum", address_display=ADDR), sqid)
        repo.insert_attribution(c, Attribution(
            address_id=aid, label="Tornado Cash", category="mixing_service", source="graphsense",
            confidence=0.6, retrieved_at="2026-02-01T00:00:00Z"), sqid)

    sqid1, _ = write_with_provenance(conn, sq1, w1, raw_response=b'{"ok":true}')

    sq2 = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="txlist",
                      params={"address": ADDR}, requested_at="2026-02-02T00:00:00Z", status="ok")

    def w2(c, sqid):
        repo.upsert_address(c, Address(chain="ethereum", address_display=ADDR), sqid)  # idempotent re-touch

    sqid2, _ = write_with_provenance(conn, sq2, w2, raw_response=b'{"result":[]}')
    return conn, db, tmp_path, sqid1, sqid2


def test_custody_appendix_lists_all_source_queries(tmp_path):
    """The report has a Chain-of-custody appendix listing EVERY source_query with its full raw-response
    hash and a facts/claims count."""
    conn, db, tmp_path, sqid1, sqid2 = _seed_two_queries(tmp_path)
    hash1 = conn.execute("SELECT raw_response_hash FROM source_query WHERE id=?", (sqid1,)).fetchone()[0]

    result = generate_report(conn, case_dir=tmp_path, title="Custody Report", render_pdf=False)
    page = result["html_path"].read_text(encoding="utf-8")
    custody = _custody_slice(page)

    # Both source queries (by connector) are enumerated in the appendix.
    assert "graphsense" in custody
    assert "etherscan" in custody
    # The FULL (untruncated) raw-response hash appears verbatim — tamper-checkable, not a shortened form.
    assert hash1 and len(hash1) == 64
    assert hash1 in custody
    conn.close()


def test_custody_section_is_deterministic(tmp_path):
    """An unchanged case re-renders byte-identically: both the whole-report content_hash and the custody
    appendix itself are stable across two renders with the same generated_at."""
    conn, db, tmp_path, sqid1, sqid2 = _seed_two_queries(tmp_path)
    a = generate_report(conn, case_dir=tmp_path, title="R", generated_at="2026-03-01T00:00:00Z",
                        render_pdf=False)
    b = generate_report(conn, case_dir=tmp_path, title="R", generated_at="2026-03-01T00:00:00Z",
                        render_pdf=False)

    # Whole-report determinism (hash over the canonical HTML) holds with the custody section present.
    assert a["content_hash"] == b["content_hash"]

    # ...and the custody appendix itself is present and byte-identical between the two renders.
    sa = _custody_slice(a["html_path"].read_text(encoding="utf-8"))
    sb = _custody_slice(b["html_path"].read_text(encoding="utf-8"))
    assert sa == sb
    assert "graphsense" in sa and "etherscan" in sa
    conn.close()
