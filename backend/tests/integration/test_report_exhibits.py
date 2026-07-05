"""FN-10 (P15, Track D — court-ready reporting): exhibit numbering + a List of Exhibits.

Every ``exhibit`` (a hashed screenshot/file/export artifact) gets a STABLE number assigned by a
deterministic sort (``captured_at``, ``id`` — both immutable), so the same case always yields the same
exhibit numbers across report renders (report immutability). The report carries a "List of Exhibits" with
each artifact's number + full content hash, and a finding that references an exhibit cites its number.
"""

from __future__ import annotations

import hashlib

from backend.app.services.exhibits import attach_screenshot, numbered_exhibits
from backend.app.services.investigator import add_finding_ref, create_finding
from backend.app.services.reporting import generate_report
from backend.tests.integration._helpers import new_case


def _attach(conn, tmp_path, name, data, *, captured_at, source=None, desc=None):
    p = tmp_path / name
    p.write_bytes(data)
    return attach_screenshot(conn, file_path=p, source=source, description=desc, captured_at=captured_at)


def test_exhibit_numbering_is_deterministic(tmp_path):
    """Numbers follow a stable sort (captured_at, id) — NOT insertion order — and recompute identically."""
    conn, db = new_case(tmp_path, title="Exhibit Case")
    # Attach OUT of capture-time order to prove numbering follows the sort, not insertion order.
    late = _attach(conn, tmp_path, "late.png", b"LATE", captured_at="2026-02-02T00:00:00Z", desc="late shot")
    early = _attach(conn, tmp_path, "early.png", b"EARLY", captured_at="2026-01-01T00:00:00Z", desc="early shot")

    nums = {e["id"]: e["number"] for e in numbered_exhibits(conn)}
    assert nums[early] == 1 and nums[late] == 2          # ordered by captured_at, not insertion order
    assert {e["id"]: e["number"] for e in numbered_exhibits(conn)} == nums   # deterministic recompute
    conn.close()


def test_report_lists_exhibits_and_findings_cite_numbers(tmp_path):
    """The report has a List of Exhibits (numbered, full content hash) and a finding referencing an exhibit
    cites its stable number rather than a raw id."""
    conn, db = new_case(tmp_path, title="Exhibit Case")
    late = _attach(conn, tmp_path, "late.png", b"LATE", captured_at="2026-02-02T00:00:00Z",
                   source="explorer-ui", desc="late shot")
    early = _attach(conn, tmp_path, "early.png", b"EARLY", captured_at="2026-01-01T00:00:00Z",
                    source="exchange-ui", desc="early shot")
    fid = create_finding(conn, statement="The withdrawal is shown in the exchange UI", assessment="high")
    add_finding_ref(conn, finding_id=fid, ref_type="exhibit", ref_id=early)   # early == Exhibit 1

    page = generate_report(conn, case_dir=tmp_path, title="R", render_pdf=False)["html_path"].read_text(
        encoding="utf-8")
    conn.close()

    # The List of Exhibits section, numbered, with descriptions + FULL content hashes.
    assert "List of Exhibits" in page
    assert "Exhibit 1" in page and "Exhibit 2" in page
    assert "early shot" in page and "late shot" in page
    assert hashlib.sha256(b"EARLY").hexdigest() in page          # full 64-char content hash listed
    assert hashlib.sha256(b"LATE").hexdigest() in page

    # The finding CITES the exhibit by number (not the raw uuid) in the Findings section.
    findings_region = page.split("<h2>Findings</h2>")[1].split("<h2>")[0]
    assert "Exhibit 1" in findings_region
    assert early not in findings_region                          # raw exhibit id is not shown


def test_no_exhibits_renders_gracefully(tmp_path):
    """A case with no exhibits still renders the section with an honest empty message (no crash)."""
    conn, db = new_case(tmp_path, title="No Exhibits")
    page = generate_report(conn, case_dir=tmp_path, title="R", render_pdf=False)["html_path"].read_text(
        encoding="utf-8")
    conn.close()
    assert "List of Exhibits" in page
    assert "No exhibits" in page


def test_exhibit_list_render_is_deterministic(tmp_path):
    """Court-ready determinism: the same case renders byte-identical HTML (fixed generated_at), so the
    exhibit numbering keeps content_hash stable."""
    conn, db = new_case(tmp_path, title="Exhibit Case")
    _attach(conn, tmp_path, "a.png", b"A", captured_at="2026-01-01T00:00:00Z", desc="a")
    _attach(conn, tmp_path, "b.png", b"B", captured_at="2026-01-02T00:00:00Z", desc="b")
    a = generate_report(conn, case_dir=tmp_path, title="R", generated_at="2026-03-03T00:00:00Z",
                        render_pdf=False)
    b = generate_report(conn, case_dir=tmp_path, title="R", generated_at="2026-03-03T00:00:00Z",
                        render_pdf=False)
    conn.close()
    assert a["html_path"].read_text(encoding="utf-8") == b["html_path"].read_text(encoding="utf-8")
    assert a["content_hash"] == b["content_hash"]
