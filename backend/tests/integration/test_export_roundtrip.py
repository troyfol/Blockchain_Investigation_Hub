"""Case export round-trip (phase_10): bundle hashes, self-containment, tamper detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.audits.runner import run_audits
from backend.app.db import repository as repo
from backend.app.models import Address, Asset, SourceQuery, Transaction, TxOutput
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.exhibits import attach_screenshot
from backend.app.services.export import (
    build_manifest,
    export_case,
    verify_casefile,
    verify_manifest,
)
from backend.tests.integration._helpers import new_case

FIXTURE_PNG = Path(__file__).resolve().parents[1] / "fixtures" / "imports" / "screenshot.png"


def _seed(conn):
    """A case that exercises several subdirs: a provenance raw file + an exhibit."""
    sq = SourceQuery(connector="esplora", capability="get_transactions", endpoint="address-txs",
                     params={"address": "probe", "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")

    def write(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)
        tx = repo.upsert_transaction(c, Transaction(
            chain="bitcoin", tx_hash="a" * 64, block_height=800000, block_ts="2026-01-01T00:00:00Z",
            confirmations=20, finality_status="final"), sqid)
        a = repo.upsert_address(c, Address(chain="bitcoin", address_display="1Seed"), sqid)
        repo.upsert_tx_output(c, TxOutput(transaction_id=tx, address_id=a, amount="100", output_index=0), sqid)

    # raw_response payload -> writes raw_responses/<sq>.json (a provenance file in the bundle).
    write_with_provenance(conn, sq, write, raw_response={"txs": [{"txid": "a" * 64}]})
    attach_screenshot(conn, file_path=FIXTURE_PNG, source="manual", description="exhibit")


@pytest.mark.smoke
def test_export_roundtrip_is_self_contained(tmp_path):
    conn, db = new_case(tmp_path, title="Export Case")
    _seed(conn)
    run_audits(db_path=str(db))  # establish the .audit_baselines/ sidecar (shipped + verified)
    conn.close()

    bundle = export_case(tmp_path)
    assert bundle.exists() and bundle.suffix == ".casefile"

    report = verify_casefile(bundle, extract_to=tmp_path / "extracted")
    assert report["ok"], report
    sc = report["self_contained"]
    assert sc["attached_databases"] == []          # no shared-cache runtime dependency
    assert sc["fk_violations"] == 0
    assert sc["missing_referenced_files"] == []     # every raw/exhibit file resolves in-bundle
    assert sc["audits_passed"]

    # The manifest covered case.db + a raw response + the exhibit + the baseline sidecar.
    listed = report["manifest"]
    assert listed["file_count"] >= 4
    files = set(build_manifest(tmp_path)["files"])
    assert "case.db" in files
    assert any(f.startswith("raw_responses/") for f in files)
    assert any(f.startswith("exhibits/") for f in files)
    assert any(f.startswith(".audit_baselines/") for f in files)


@pytest.mark.smoke
def test_export_roundtrip_preserves_investigator_labels(tmp_path):
    """Investigator display-label overrides (custom node label + trace label) MUST survive a .casefile
    round-trip — they ride inside case.db, which export ships whole and the verifier re-audits."""
    from backend.app.db import get_connection
    from backend.app.services.investigator import current_labels, set_label
    from backend.app.services.tracing import create_trace

    conn, db = new_case(tmp_path, title="Labels Export")
    _seed(conn)
    addr = conn.execute("SELECT id FROM address LIMIT 1").fetchone()["id"]
    trace = create_trace(conn, name="orig path")
    set_label(conn, target_type="address", target_id=addr, label="Custom node name")
    set_label(conn, target_type="trace", target_id=trace, label="Custom path name")
    run_audits(db_path=str(db))   # baseline written while fresh
    conn.close()

    bundle = export_case(tmp_path)
    report = verify_casefile(bundle, extract_to=tmp_path / "ex_labels")
    assert report["ok"], report            # hashes + self-containment + re-run audits all pass

    rconn = get_connection(tmp_path / "ex_labels" / "case.db")
    try:
        assert current_labels(rconn, "address").get(addr) == "Custom node name"
        assert current_labels(rconn, "trace").get(trace) == "Custom path name"
    finally:
        rconn.close()


@pytest.mark.smoke
def test_export_checkpoints_wal_when_db_still_open(tmp_path):
    """A case DB runs in WAL mode; export must bundle a COMPLETE case.db even when a connection is still
    OPEN (writes not yet checkpointed). export_case checkpoints the WAL first, so the bundle re-verifies
    instead of shipping a silently-incomplete case (regression guard for the Ronin-validation finding)."""
    conn, db = new_case(tmp_path, title="Open Export")
    _seed(conn)
    run_audits(db_path=str(db))  # baseline written while the data is fresh
    # conn is intentionally LEFT OPEN (uncheckpointed WAL) — the previously-incomplete-bundle failure mode.
    bundle = export_case(tmp_path)
    report = verify_casefile(bundle, extract_to=tmp_path / "ex_open")
    assert report["ok"], report                       # complete + self-contained despite the open conn
    assert report["self_contained"]["audits_passed"]  # no false 'deleted' rows from a stale .db
    conn.close()


@pytest.mark.smoke
def test_verify_detects_tampering(tmp_path):
    conn, db = new_case(tmp_path, title="Tamper Case")
    _seed(conn)
    run_audits(db_path=str(db))
    conn.close()

    bundle = export_case(tmp_path)
    extract = tmp_path / "ex"
    assert verify_casefile(bundle, extract_to=extract)["ok"]

    # Flip a byte in the extracted case.db -> hash mismatch is caught.
    dbf = extract / "case.db"
    data = bytearray(dbf.read_bytes())
    data[-1] ^= 0xFF
    dbf.write_bytes(bytes(data))
    after = verify_manifest(extract)
    assert not after["ok"] and "case.db" in after["mismatched"]

    # An unlisted file slipped into the bundle is also caught.
    (extract / "reports").mkdir(exist_ok=True)
    (extract / "reports" / "rogue.pdf").write_bytes(b"%PDF-1.4 not in manifest")
    assert "reports/rogue.pdf" in verify_manifest(extract)["extra"]


def test_verify_rejects_unsafe_db_path_refs(tmp_path):
    conn, db = new_case(tmp_path, title="Unsafe Refs")
    _seed(conn)
    # Forge a malicious exhibit file_ref (bypassing the sanitizing attach_screenshot service) — an
    # untrusted .casefile must not make verification probe outside the case folder.
    conn.execute(
        "INSERT INTO exhibit (id, exhibit_type, source, captured_at, file_ref, content_hash, description) "
        "VALUES (?,?,?,?,?,?,?)",
        ("evil", "screenshot", "x", "2026-01-01T00:00:00Z", "../../escape.txt", "deadbeef", None))
    run_audits(db_path=str(db))
    conn.close()

    bundle = export_case(tmp_path)
    report = verify_casefile(bundle, extract_to=tmp_path / "ex_unsafe")
    assert not report["ok"]
    assert "../../escape.txt" in report["self_contained"]["unsafe_referenced_paths"]


def test_manifest_is_deterministic(tmp_path):
    conn, db = new_case(tmp_path, title="Deterministic")
    _seed(conn)
    run_audits(db_path=str(db))
    conn.close()

    m1 = build_manifest(tmp_path)
    m2 = build_manifest(tmp_path)
    assert m1 == m2                       # same case -> identical manifest (sorted, content-hashed)
    # export_case scrubs yoyo's `_yoyo_log` (privacy) on first run — a one-time, intentional change to
    # case.db's hash — and writing manifest.json must not perturb the other files. From then on export is
    # IDEMPOTENT: a second export over an already-scrubbed case yields the byte-identical manifest.
    export_case(tmp_path)
    after_first = build_manifest(tmp_path)["files"]
    export_case(tmp_path)
    assert build_manifest(tmp_path)["files"] == after_first


def test_export_scrubs_os_username_hostname_from_migration_log(tmp_path):
    """Privacy gate (P10): yoyo's `_yoyo_log` records the OS username + hostname that applied each
    migration. That investigator-identifying data must NOT ride inside an exported .casefile — export
    scrubs it — while `_yoyo_migration` (the applied-migration STATE) is preserved so an imported case
    still forward-migrates."""
    from backend.app.db import get_connection

    conn, db = new_case(tmp_path, title="Scrub Case")
    _seed(conn)
    # the act of migrating populated _yoyo_log with this machine's username/hostname
    assert conn.execute("SELECT COUNT(*) FROM _yoyo_log").fetchone()[0] > 0
    run_audits(db_path=str(db))
    conn.close()

    bundle = export_case(tmp_path)
    report = verify_casefile(bundle, extract_to=tmp_path / "ex_scrub")
    assert report["ok"], report  # still self-contained + verifies after the scrub

    rconn = get_connection(tmp_path / "ex_scrub" / "case.db")
    try:
        assert rconn.execute("SELECT COUNT(*) FROM _yoyo_log").fetchone()[0] == 0   # PII gone
        assert rconn.execute("SELECT COUNT(*) FROM _yoyo_migration").fetchone()[0] > 0  # state preserved
    finally:
        rconn.close()


def test_export_includes_reports(tmp_path):
    # No browser engine needed: the report's hashed artifact is its self-contained HTML, which is what
    # the row's rendered_file_ref points at and what must resolve in the bundle (P3). render_pdf=False
    # keeps this deterministic in CI; the PDF render is smoke-tested separately in test_report.py.
    from backend.app.services.reporting import generate_report

    conn, db = new_case(tmp_path, title="Report Export")
    _seed(conn)
    result = generate_report(conn, case_dir=tmp_path, title="R1", render_pdf=False)
    run_audits(db_path=str(db))
    conn.close()

    bundle = export_case(tmp_path)
    report = verify_casefile(bundle, extract_to=tmp_path / "ex2")
    assert report["ok"], report  # the report's rendered_file_ref (the HTML) resolves in-bundle
    files = set(build_manifest(tmp_path)["files"])
    assert f"reports/{result['report_id']}.html" in files  # the hashed source-of-truth travels
