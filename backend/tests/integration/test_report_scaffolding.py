"""FN-12 (P16, Track D — court-ready reporting): court-formal scaffolding — a cover page, a table of
contents, and a running page footer (case id + "Page N of M") via CSS ``@page`` margin boxes.

Asserts on the rendered self-contained HTML/CSS (the deterministic hashed source of truth), NOT the PDF
pixels — page numbers are a paged-media render concern. It was verified empirically (probe, this phase)
that both render paths — the DEFAULT Edge/Chrome ``--print-to-pdf`` CLI *and* the Playwright fallback
(both Blink) — render ``@page`` margin-box ``counter(page)``/``counter(pages)`` identically, while the
GCPM ``string()``/``string-set`` feature is unsupported (so the dynamic case id is injected as a literal
into a second ``@page`` rule, which Blink merges with report.css's ``@page{size;margin}``).

Watch item (documented): the report's OWN SHA-256 ``content_hash`` cannot be printed on the cover — a
document cannot contain a verifiable hash of itself (embedding it changes the very bytes being hashed). So
the cover carries the case id + generated-at + an integrity statement telling the reader how to recompute
and check the hash; the hash value itself lives in the immutable ``report`` row + export manifest.
"""

from __future__ import annotations

import hashlib
import re

from backend.app.db import repository as repo
from backend.app.models import Address, Asset, SourceQuery, Transaction, TxOutput
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.reporting import generate_report
from backend.tests.integration._helpers import new_case


def _seed(tmp_path):
    conn, db = new_case(tmp_path, title="Scaffolding Case")
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

    write_with_provenance(conn, sq, write)
    return conn, db


def _render(conn, tmp_path, **kw):
    return generate_report(conn, case_dir=tmp_path, title="R", render_pdf=False,
                           **kw)["html_path"].read_text(encoding="utf-8")


def test_cover_toc_and_page_numbers(tmp_path):
    conn, db = _seed(tmp_path)
    case_id = conn.execute("SELECT id FROM case_meta LIMIT 1").fetchone()["id"]
    page = _render(conn, tmp_path, generated_at="2026-01-02T00:00:00Z")
    conn.close()

    # --- cover page ---
    assert 'class="cover"' in page, "no cover page"
    cover = page.split('class="cover"')[1].split("</section>")[0]
    assert case_id in cover, "cover does not carry the case id"
    assert "2026-01-02T00:00:00Z" in cover, "cover does not carry the generated-at time"
    # honest integrity anchor (the hash can't self-embed): a verification statement, not a fake hash.
    assert re.search(r"SHA-256|content hash", cover, re.I), "cover has no integrity/verification anchor"
    assert "Prepared by" in cover, "no court-formal prepared-by/signature line on the cover"

    # --- table of contents (lists the sections, links to their anchors) ---
    assert 'class="toc"' in page, "no table of contents"
    toc = page.split('class="toc"')[1].split("</nav>")[0]
    for title, slug in [("Methodology", "methodology"), ("Findings", "findings"),
                        ("Chain of custody", "chain-of-custody"), ("List of Exhibits", "list-of-exhibits")]:
        assert title in toc and f'href="#{slug}"' in toc, f"TOC missing {title!r}"
        assert f'id="{slug}"' in page, f"section {title!r} has no anchor id for the TOC to target"

    # --- running footer: case id + Page N of M via @page margin-box counters (renders on both paths) ---
    assert "counter(page)" in page and "counter(pages)" in page, "no page-number counters in the footer"
    assert re.search(r"@bottom-(left|center|right)\s*\{[^}]*content", page), "no @page footer margin box"
    # the case id is in a footer margin box (court page-identification if pages are physically separated)
    assert re.search(r"@bottom-[a-z]+\s*\{[^}]*" + re.escape(case_id), page), "case id not in the footer"


def test_toc_entries_match_the_rendered_section_headings(tmp_path):
    """No drift: the set of TOC link targets is exactly the set of content-section ``<section id=...>``
    anchors in the body (headings stay bare ``<h2>`` so pre-existing section-splitting tests are unaffected;
    the TOC's own heading is not a section, so it is not a target of itself)."""
    conn, db = _seed(tmp_path)
    page = _render(conn, tmp_path)
    conn.close()
    section_ids = set(re.findall(r'<section id="([^"]+)"', page))
    toc = page.split('class="toc"')[1].split("</nav>")[0]
    toc_targets = set(re.findall(r'href="#([^"]+)"', toc))
    assert toc_targets, "empty TOC"
    assert toc_targets == section_ids, "TOC targets and rendered section anchors disagree"


def test_cover_does_not_fabricate_a_self_hash(tmp_path):
    """The report's own content_hash is NOT printed on the cover (self-reference is impossible); the cover
    states how to verify instead. Guards a future 'show the hash' change that would break verification (the
    printed value could never match the file's real SHA-256)."""
    conn, db = _seed(tmp_path)
    res = generate_report(conn, case_dir=tmp_path, title="R", render_pdf=False,
                          generated_at="2026-01-02T00:00:00Z")
    page = res["html_path"].read_text(encoding="utf-8")
    conn.close()
    cover = page.split('class="cover"')[1].split("</section>")[0]
    assert res["content_hash"] not in cover, "the report's own content_hash must not be self-embedded"
    assert hashlib.sha256(page.encode("utf-8")).hexdigest() == res["content_hash"]  # unbroken by the cover


def test_scaffolding_render_is_deterministic(tmp_path):
    """Court-ready determinism: same case + generated_at -> byte-identical HTML + stable content_hash."""
    conn, db = _seed(tmp_path)
    a = generate_report(conn, case_dir=tmp_path, title="R", generated_at="2026-01-02T00:00:00Z",
                        render_pdf=False)
    b = generate_report(conn, case_dir=tmp_path, title="R", generated_at="2026-01-02T00:00:00Z",
                        render_pdf=False)
    conn.close()
    assert a["html_path"].read_text(encoding="utf-8") == b["html_path"].read_text(encoding="utf-8")
    assert a["content_hash"] == b["content_hash"]
