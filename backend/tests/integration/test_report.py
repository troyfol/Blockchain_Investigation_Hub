"""Report generation (phase_09; render reworked in P3): HTML-hash immutability + clean supersession,
plus a lighter PDF render smoke.

P3 freezes the report's ``content_hash`` over the canonical self-contained HTML (engine-independent),
not the PDF bytes. So the immutability + supersession goldens run in CI with NO browser engine
(``render_pdf=False``). A separate smoke renders an actual PDF via the OS browser engine when one is
available, and skips cleanly when absent (mirrors the old 'skip when Chromium absent' behavior).
"""

from __future__ import annotations

import hashlib

import pytest

from backend.app.audits.runner import run_audits
from backend.app.db import repository as repo
from backend.app.models import Address, Asset, Entity, SourceQuery, Transaction, TxInput, TxOutput
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services import report_render
from backend.app.services.investigator import add_finding_ref, create_finding
from backend.app.services.reporting import generate_report
from backend.app.services.tracing import create_trace, fifo_trace_transaction
from backend.tests.integration._helpers import make_membership, new_case


@pytest.fixture
def seeded(tmp_path):
    """A small but representative case: a FIFO trace, a contested entity, a finding, BTC facts."""
    conn, db = new_case(tmp_path, title="Operation Test")
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
        o0 = repo.upsert_tx_output(c, TxOutput(transaction_id=tx0, address_id=addr("A"), amount="100", output_index=0), sqid)
        tx1 = repo.upsert_transaction(c, Transaction(
            chain="bitcoin", tx_hash="1" * 64, block_height=800000, block_ts="2026-01-01T01:00:00Z",
            fee="10", confirmations=1, finality_status="provisional"), sqid)  # provisional -> dashed in graph
        repo.upsert_tx_input(c, TxInput(transaction_id=tx1, prev_output_id=o0, address_id=addr("A"), amount="100", input_index=0), sqid)
        repo.upsert_tx_output(c, TxOutput(transaction_id=tx1, address_id=addr("C"), amount="90", output_index=0), sqid)
        ids["tx1"] = tx1
        ids["addr_a"] = addr("A")

    write_with_provenance(conn, sq, write)

    # A FIFO trace over tx1 (a labeled convention).
    trace_id = create_trace(conn, name="Trace of stolen BTC")
    fifo_trace_transaction(conn, trace_id=trace_id, transaction_id=ids["tx1"])

    # A contested entity (two sources disagree on the same address) — shown side-by-side.
    ent = repo.insert_entity(conn, Entity(origin="source", name="Acme Exchange"))
    make_membership(conn, entity_id=ent, address_id=ids["addr_a"], source="arkham",
                    method="shared-label", connector="arkham-import")
    make_membership(conn, entity_id=ent, address_id=ids["addr_a"], source="misttrack",
                    method="shared-label", connector="misttrack-import")

    # A finding referencing the address.
    f = create_finding(conn, statement="Address A is an Acme deposit address", assessment="medium")
    add_finding_ref(conn, finding_id=f, ref_type="address", ref_id=ids["addr_a"])

    yield conn, db, tmp_path
    conn.close()


@pytest.mark.smoke
def test_report_html_is_the_immutable_hashed_artifact(seeded):
    """The report is produced + its content_hash verifiable with NO browser engine: the hash is over
    the canonical HTML, which is what supersession + the export manifest key off."""
    conn, db, tmp_path = seeded
    result = generate_report(conn, case_dir=tmp_path, title="Operation Test — Report 1",
                             render_pdf=False)

    # The HTML exists and the row's content_hash matches its bytes (over the HTML, not a PDF).
    html_path = result["html_path"]
    assert html_path.exists() and html_path.stat().st_size > 0
    assert result["pdf_path"] is None  # no engine asked for -> HTML-only, still a complete report
    page = html_path.read_text(encoding="utf-8")
    assert hashlib.sha256(page.encode("utf-8")).hexdigest() == result["content_hash"]

    # The report row: immutable snapshot pointing at the HTML, with scope_spec (applied bounds) recorded.
    row = conn.execute("SELECT * FROM report WHERE id=?", (result["report_id"],)).fetchone()
    assert row["title"] == "Operation Test — Report 1"
    assert row["content_hash"] == result["content_hash"]
    assert row["rendered_file_ref"] == f"reports/{result['report_id']}.html"  # relative -> portable
    assert row["supersedes_report_id"] is None
    import json
    assert "bounds" in json.loads(row["scope_spec"])

    # Honesty content in the self-contained HTML.
    assert "frozen snapshot" in page                       # timestamp / completeness caveat
    assert "convention" in page and "FIFO" in page          # FIFO labeled as a convention
    assert "contested" in page                              # multi-source entity not collapsed
    assert "without a fabricated value" in page             # missing valuations shown honestly
    assert "cytoscape(" in page                             # the real library renders the graph

    assert all(r.passed for r in run_audits(db_path=str(db)))


@pytest.mark.smoke
def test_supersession_leaves_the_old_report_intact(seeded):
    conn, db, tmp_path = seeded
    first = generate_report(conn, case_dir=tmp_path, title="Report 1", render_pdf=False)
    first_row = dict(conn.execute("SELECT * FROM report WHERE id=?", (first["report_id"],)).fetchone())
    first_bytes = first["html_path"].read_bytes()

    second = generate_report(conn, case_dir=tmp_path, title="Report 2 (supersedes)",
                             supersedes_report_id=first["report_id"], render_pdf=False)

    # The new report points at the old; two distinct rows exist.
    second_row = conn.execute("SELECT * FROM report WHERE id=?", (second["report_id"],)).fetchone()
    assert second_row["supersedes_report_id"] == first["report_id"]
    assert conn.execute("SELECT COUNT(*) FROM report").fetchone()[0] == 2

    # The old report is untouched — row, file, and hash all unchanged (immutable snapshot).
    reread = dict(conn.execute("SELECT * FROM report WHERE id=?", (first["report_id"],)).fetchone())
    assert reread == first_row
    assert first["html_path"].read_bytes() == first_bytes
    assert _sha256_text(first["html_path"].read_text(encoding="utf-8")) == first["content_hash"]

    assert all(r.passed for r in run_audits(db_path=str(db)))


@pytest.mark.smoke
def test_report_renders_a_real_pdf_when_an_engine_is_available(seeded):
    """Lighter render smoke: with an OS browser engine present, a non-empty PDF is produced alongside
    the HTML; with none, skip cleanly (the HTML report + hashed row are unaffected either way)."""
    if not report_render.renderer_available():
        pytest.skip("no OS browser engine / Playwright available to render a PDF")

    conn, db, tmp_path = seeded
    result = generate_report(conn, case_dir=tmp_path, title="Operation Test — PDF", render_pdf=True)

    pdf = result["pdf_path"]
    assert pdf is not None and pdf.exists() and pdf.stat().st_size > 0
    assert pdf.read_bytes()[:5] == b"%PDF-"
    assert result["engine"]  # the engine that rendered it (edge / chrome / playwright)

    # The hash is still over the HTML (engine-independent), not the non-deterministic PDF bytes.
    page = result["html_path"].read_text(encoding="utf-8")
    assert hashlib.sha256(page.encode("utf-8")).hexdigest() == result["content_hash"]

    assert all(r.passed for r in run_audits(db_path=str(db)))


def _sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()
