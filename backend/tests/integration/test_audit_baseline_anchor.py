"""P27 / FN-19: in-DB append-only ``audit_baseline`` anchoring.

The final-immutability baseline lived ONLY in a JSON sidecar (``.audit_baselines/``). An adversary
who rewrites a ``final`` row can also delete that sidecar; the next audit then finds no baseline and
silently RE-BASELINES the already-tampered state — passing green. That hole is admitted in
``immutability.py``'s own trust model ("an adversary who can rewrite rows can also delete the
sidecar ... or ride a migration through the re-baseline path").

P27 anchors the baseline IN the case DB: an append-only ``audit_baseline`` table whose ``anchor_hash``
ties the immutable final snapshot to the ``source_query.raw_response_hash`` provenance the case
commits (Invariant #3/#6). A re-open whose committed final-state no longer matches that anchor cannot
silently re-baseline — the ``final-immutability`` audit FAILS. The anchor travels inside ``case.db``
(manifest-hashed) so export stays tamper-evident.
"""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from backend.app.audits.baselines import default_baseline_dir, read_latest_anchor
from backend.app.audits.runner import run_audits
from backend.app.db import apply_migrations, get_connection
from backend.app.db import repository as repo
from backend.app.services.export import export_case, verify_casefile
from backend.tests.integration.test_seeded_case import _result, seed_evm_transfer


@pytest.fixture
def case(tmp_path):
    db = tmp_path / "case.db"
    apply_migrations(db)
    conn = get_connection(db)
    repo.init_case(conn, title="Anchor Test Case")
    yield conn, db
    conn.close()


def test_baseline_is_committed_in_db_and_append_only(case):
    # Acceptance: the baseline is committed in-DB (not only the sidecar) AND is append-only.
    conn, db = case
    seed_evm_transfer(conn, final=True)
    assert _result(run_audits(db_path=str(db)), "final-immutability").passed  # establishes baseline

    anchor = read_latest_anchor(conn, "final-immutability")
    assert anchor, "the first audit must commit an in-DB anchor, not only the JSON sidecar"

    # Append-only: UPDATE/DELETE are refused at the DB layer, and the row is left intact. (RAISE(ABORT)
    # surfaces as a sqlite3.Error whose exact subclass varies by SQLite version — assert the base.)
    before = conn.execute("SELECT anchor_hash FROM audit_baseline").fetchone()[0]
    with pytest.raises(sqlite3.Error):
        conn.execute("UPDATE audit_baseline SET anchor_hash='tampered'")
    with pytest.raises(sqlite3.Error):
        conn.execute("DELETE FROM audit_baseline")
    assert conn.execute("SELECT anchor_hash FROM audit_baseline").fetchone()[0] == before


def test_reopen_cannot_rebaseline_tampered(case):
    # Acceptance (the core hole): a re-open cannot silently re-baseline a pre-tampered state.
    conn, db = case
    ids = seed_evm_transfer(conn, final=True)
    assert _result(run_audits(db_path=str(db)), "final-immutability").passed  # baseline recorded

    # The attack: rewrite a FINAL row AND delete the sidecar (the documented bypass) before re-opening.
    conn.execute("UPDATE transfer SET amount='9' WHERE id=?", (ids["transfer_id"],))
    shutil.rmtree(default_baseline_dir(db))

    # Re-open + re-audit. WITHOUT P27 the missing sidecar makes this silently re-baseline the tampered
    # state and PASS. WITH P27 the in-DB anchor no longer matches the committed final-state -> FAIL.
    fi = _result(run_audits(db_path=str(db)), "final-immutability")
    assert not fi.passed
    assert "anchor" in (fi.detail + str(fi.offending)).lower()


def test_explicit_rebaseline_still_works_after_sidecar_loss(case):
    # The `--rebaseline` operator escape hatch (for a change VERIFIED out-of-band) must still work even
    # though the anchor guard fires on a bare sidecar loss: an EXPLICIT re-baseline re-anchors + passes.
    conn, db = case
    ids = seed_evm_transfer(conn, final=True)
    assert _result(run_audits(db_path=str(db)), "final-immutability").passed

    conn.execute("UPDATE transfer SET amount='9' WHERE id=?", (ids["transfer_id"],))
    shutil.rmtree(default_baseline_dir(db))

    # A bare re-open FAILS (guarded)...
    assert not _result(run_audits(db_path=str(db)), "final-immutability").passed
    # ...but an explicit operator re-baseline re-establishes both sidecar and anchor and passes.
    ok = _result(run_audits(db_path=str(db), rebaseline=["final-immutability"]), "final-immutability")
    assert ok.passed
    # And it is now stable on the next plain run (anchor advanced to the re-verified state).
    assert _result(run_audits(db_path=str(db)), "final-immutability").passed


def test_export_carries_anchor_and_reverifies(case, tmp_path):
    # Acceptance: export still verifies the baseline travels + is tamper-evident (anchor rides in case.db).
    conn, db = case
    seed_evm_transfer(conn, final=True)
    assert _result(run_audits(db_path=str(db)), "final-immutability").passed
    conn.close()  # release the handle so export can checkpoint + zip cleanly (Windows lock)

    bundle = export_case(db.parent)
    report = verify_casefile(bundle, extract_to=tmp_path / "extracted")
    assert report["ok"]
    assert report["self_contained"]["audits_passed"]
    assert report["self_contained"]["final_anchor_present"], "the in-DB anchor must travel in the bundle"

    extracted = get_connection(tmp_path / "extracted" / "case.db")
    try:
        assert read_latest_anchor(extracted, "final-immutability"), "anchor row must survive the round-trip"
    finally:
        extracted.close()
