"""FN-11 (P17, Track D — court-ready reporting): an auto, case-scoped glossary appendix. It defines ONLY
the specialized terms the case actually uses (data-driven triggers — a term appears iff the case holds the
kind of evidence it describes), renders deterministically, and is omitted entirely (section AND TOC entry)
when no term applies — respecting the P16 TOC/section set-equality invariant."""

from __future__ import annotations

from backend.app.db import repository as repo
from backend.app.models import Address, Asset, SourceQuery, Transaction, TxInput, TxOutput
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.reporting import generate_report
from backend.app.services.tracing import create_trace, fifo_trace_transaction
from backend.tests.integration._helpers import new_case


def _render(conn, tmp_path, **kw):
    return generate_report(conn, case_dir=tmp_path, title="R", render_pdf=False,
                           **kw)["html_path"].read_text(encoding="utf-8")


def _glossary_region(page):
    if 'id="glossary"' not in page:
        return None
    return page.split('id="glossary"')[1].split("</section>")[0]


def test_glossary_lists_only_used_terms(tmp_path):
    """A BTC case with a FIFO trace + a provisional tx uses UTXO / FIFO / provisional-vs-final; it has NO
    valuation, NO sanctioned risk, NO CoinJoin, NO bridge — so those terms must NOT appear."""
    conn, db = new_case(tmp_path, title="Glossary Case")
    sq = SourceQuery(connector="esplora", capability="get_transactions", endpoint="address-txs",
                     params={"address": "probe"}, requested_at="2026-01-01T00:00:00Z", status="ok")
    ids = {}

    def write(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)

        def addr(n):
            return repo.upsert_address(c, Address(chain="bitcoin", address_display=n), sqid)

        tx0 = repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="0" * 64, block_height=1,
            block_ts="2026-01-01T00:00:00Z", confirmations=20, finality_status="final"), sqid)
        o0 = repo.upsert_tx_output(c, TxOutput(transaction_id=tx0, address_id=addr("A"), amount="100",
                                               output_index=0), sqid)
        tx1 = repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="1" * 64, block_height=2,
            block_ts="2026-01-01T01:00:00Z", confirmations=1, finality_status="provisional"), sqid)
        repo.upsert_tx_input(c, TxInput(transaction_id=tx1, prev_output_id=o0, address_id=addr("A"),
                                        amount="100", input_index=0), sqid)
        repo.upsert_tx_output(c, TxOutput(transaction_id=tx1, address_id=addr("C"), amount="90",
                                          output_index=0), sqid)
        ids["tx1"] = tx1

    write_with_provenance(conn, sq, write)
    tr = create_trace(conn, name="T")
    fifo_trace_transaction(conn, trace_id=tr, transaction_id=ids["tx1"])

    page = _render(conn, tmp_path)
    conn.close()
    region = _glossary_region(page)
    assert region is not None, "glossary section missing for a case that uses specialized terms"
    # used (match the term in its <dt>, robust to any cross-reference inside a definition):
    assert "<dt>UTXO" in region
    assert "<dt>FIFO" in region
    assert "<dt>Provisional" in region
    # NOT used -> the term is absent:
    assert "<dt>Value at time" not in region       # no valuation
    assert "<dt>Sanctioned" not in region          # no sanctioned risk
    assert "<dt>CoinJoin" not in region            # no coinjoin flag
    assert "<dt>Cross-chain bridge" not in region  # no bridge link


def test_glossary_absent_when_no_terms_used(tmp_path):
    """An empty case triggers no term — the glossary section is omitted AND the TOC does not link it
    (the P16 set-equality invariant: TOC targets == rendered section anchors)."""
    conn, db = new_case(tmp_path, title="Bare Case")
    page = _render(conn, tmp_path)
    conn.close()
    assert 'id="glossary"' not in page, "empty case must not render a glossary section"
    toc = page.split('class="toc"')[1].split("</nav>")[0]
    assert 'href="#glossary"' not in toc, "TOC links a glossary section that is not rendered"


def test_glossary_render_is_deterministic(tmp_path):
    conn, db = new_case(tmp_path, title="Det Case")
    sq = SourceQuery(connector="esplora", capability="get_transactions", endpoint="a",
                     params={}, requested_at="2026-01-01T00:00:00Z", status="ok")

    def write(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)
        repo.upsert_address(c, Address(chain="bitcoin", address_display="A"), sqid)
        repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="0" * 64, block_height=1,
            block_ts="2026-01-01T00:00:00Z", confirmations=20, finality_status="final"), sqid)

    write_with_provenance(conn, sq, write)
    a = generate_report(conn, case_dir=tmp_path, title="R", generated_at="2026-01-02T00:00:00Z",
                        render_pdf=False)
    b = generate_report(conn, case_dir=tmp_path, title="R", generated_at="2026-01-02T00:00:00Z",
                        render_pdf=False)
    conn.close()
    assert a["html_path"].read_text(encoding="utf-8") == b["html_path"].read_text(encoding="utf-8")
    assert a["content_hash"] == b["content_hash"]
