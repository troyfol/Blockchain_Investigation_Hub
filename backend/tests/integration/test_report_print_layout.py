"""UX-10 (P14, Track D — court-ready reporting): the report's print layout & typography pass.

Asserts on the rendered self-contained HTML/CSS: the print stylesheet carries `@page` size/margins +
`break-inside` rules (so the PDF paginates cleanly on the DEFAULT Edge/Chrome `--print-to-pdf` path, which
honors CSS `@page`, not only via Playwright); the applied-bounds scope renders as a formatted **table**, not
raw `<pre>` JSON; and every exhibit's raw-response SHA-256 renders in FULL (untruncated) under the new fixed
custody layout. The report `content_hash` stays deterministic.
"""

from __future__ import annotations

import hashlib
import re

from backend.app.db import repository as repo
from backend.app.models import Address, Asset, SourceQuery, Transaction, TxOutput
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.reporting import generate_report
from backend.tests.integration._helpers import new_case


def _seed(tmp_path, *, raw_response=None):
    """A minimal case with one Bitcoin exhibit; ``raw_response`` (when given) is captured so the
    source_query carries a real full-length SHA-256 in the chain-of-custody appendix."""
    conn, db = new_case(tmp_path, title="Layout Case")
    sq = SourceQuery(connector="esplora", capability="get_transactions", endpoint="address-txs",
                     params={"address": "probe", "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")

    def write(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)
        a = repo.upsert_address(c, Address(chain="bitcoin", address_display="A"), sqid)
        tx = repo.upsert_transaction(c, Transaction(
            chain="bitcoin", tx_hash="0" * 64, block_height=799999, block_ts="2026-01-01T00:00:00Z",
            confirmations=20, finality_status="final"), sqid)
        repo.upsert_tx_output(c, TxOutput(transaction_id=tx, address_id=a, amount="100",
                                          output_index=0), sqid)

    write_with_provenance(conn, sq, write, raw_response=raw_response)
    return conn, db


def _render(conn, tmp_path, **kw):
    return generate_report(conn, case_dir=tmp_path, title="R", render_pdf=False,
                           **kw)["html_path"].read_text(encoding="utf-8")


def test_page_and_breakinside_rules_present(tmp_path):
    """The print stylesheet sets `@page` size/margins + `break-inside` rules so the PDF paginates cleanly
    on the DEFAULT Edge/Chrome `--print-to-pdf` path (which honors CSS `@page`), not only via Playwright."""
    conn, db = _seed(tmp_path)
    page = _render(conn, tmp_path)
    conn.close()

    at_page = re.search(r"@page\s*\{[^}]*\}", page)
    assert at_page, "no @page rule — Edge/Chrome print margins won't apply"
    assert "margin" in at_page.group(0), "@page carries no print margins"
    assert re.search(r"break-inside:\s*avoid", page), "no break-inside:avoid — rows/sections can split mid-page"


def test_hashes_untruncated(tmp_path):
    """Every exhibit's raw-response SHA-256 renders in FULL (all 64 chars) — a court reviewer can re-verify
    the exact bytes — and the custody table is print-hardened (fixed layout + wrapping) so the wide hash
    column wraps rather than clipping."""
    raw = b'{"probe": "raw etherscan-style payload for the custody hash"}'
    full = hashlib.sha256(raw).hexdigest()
    conn, db = _seed(tmp_path, raw_response=raw)
    page = _render(conn, tmp_path)
    conn.close()

    assert len(full) == 64
    assert full in page, "the complete 64-char raw-response hash is not rendered verbatim"
    assert f"{full[:10]}…" not in page, "hash rendered in the _short()-elided form"
    # the custody table is print-hardened: fixed layout with a wrapping hash column (won't clip in print).
    assert re.search(r"\.custody\s*\{[^}]*table-layout:\s*fixed", page), "custody table isn't fixed-layout"
    assert re.search(r"\.custody[^{}]*\.hash[^{}]*\{[^}]*(word-break|overflow-wrap)", page)


def test_scope_renders_as_table(tmp_path):
    """The applied bounds render as a formatted key/value TABLE (nested dicts flattened to dotted keys),
    not a raw `<pre>` JSON dump."""
    conn, db = _seed(tmp_path)
    page = _render(conn, tmp_path, scope_spec={"selection": "current-view", "hops": 2,
                                               "hidden": {"dust_folded": 3}})
    conn.close()

    assert "Scope &amp; applied bounds</h2>" in page
    scope_region = page.split("Scope &amp; applied bounds</h2>")[1][:1000]
    assert "scope" in scope_region and "<table" in scope_region, "scope not rendered as a table"
    assert "<pre" not in scope_region, "scope still rendered as raw <pre> JSON"
    assert "hidden.dust_folded" in page, "nested scope dict not flattened to a dotted key"
    assert "current-view" in scope_region


def test_scope_table_render_is_deterministic(tmp_path):
    """Court-ready determinism: the same case + scope renders byte-identical HTML (fixed `generated_at`,
    sorted scope keys), so the immutable `content_hash` is stable."""
    conn, db = _seed(tmp_path)
    sc = {"selection": "current-view", "hops": 2, "hidden": {"dust_folded": 3, "poison_folded": 1},
          "denom_filters": ["0.1", "1.0"], "bounded": True}
    a = generate_report(conn, case_dir=tmp_path, title="R", generated_at="2026-01-02T00:00:00Z",
                        render_pdf=False, scope_spec=sc)
    b = generate_report(conn, case_dir=tmp_path, title="R", generated_at="2026-01-02T00:00:00Z",
                        render_pdf=False, scope_spec=sc)
    conn.close()
    assert a["html_path"].read_text(encoding="utf-8") == b["html_path"].read_text(encoding="utf-8")
    assert a["content_hash"] == b["content_hash"]
