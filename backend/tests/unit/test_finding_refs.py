"""Findings / annotations / tags poly-refs (phase_08): validated on write, audit guards them."""

from __future__ import annotations

import pytest

from backend.app.audits.runner import run_audits
from backend.app.db import repository as repo
from backend.app.models import Address, Entity, SourceQuery
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.investigator import (
    add_annotation,
    add_finding_ref,
    add_tag,
    create_finding,
    current_labels,
    set_label,
)
from backend.app.services.tracing import create_trace
from backend.tests.integration._helpers import new_case


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="Investigator")
    yield conn, db
    conn.close()


def _seed_address(conn):
    sq = SourceQuery(connector="etherscan", capability="x", endpoint="e", params={"bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")
    holder = {}
    write_with_provenance(conn, sq, lambda c, sqid: holder.update(
        a=repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + "a" * 40), sqid)))
    return holder["a"]


def test_findings_annotations_tags_resolve(case):
    conn, db = case
    addr = _seed_address(conn)
    entity = repo.insert_entity(conn, Entity(origin="investigator", name="Acme"))
    trace = create_trace(conn, name="T1")

    f = create_finding(conn, statement="Acme controls this address", assessment="high")
    add_finding_ref(conn, finding_id=f, ref_type="address", ref_id=addr)
    add_finding_ref(conn, finding_id=f, ref_type="entity", ref_id=entity)
    add_finding_ref(conn, finding_id=f, ref_type="trace", ref_id=trace)

    add_annotation(conn, target_type="address", target_id=addr, content="seen in mixer")
    add_annotation(conn, target_type="finding", target_id=f, content="needs review")
    add_tag(conn, target_type="address", target_id=addr, label="suspect")
    add_tag(conn, target_type="entity", target_id=entity, label="exchange")

    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_dangling_ref_is_rejected_on_write(case):
    conn, db = case
    f = create_finding(conn, statement="bad ref")
    with pytest.raises(ValueError):
        add_finding_ref(conn, finding_id=f, ref_type="address", ref_id="does-not-exist")
    with pytest.raises(ValueError):
        add_annotation(conn, target_type="entity", target_id="nope", content="x")
    with pytest.raises(ValueError):
        add_tag(conn, target_type="address", target_id="nope", label="x")


def test_invalid_target_type_rejected(case):
    conn, db = case
    addr = _seed_address(conn)
    with pytest.raises(ValueError):
        add_tag(conn, target_type="transfer", target_id=addr, label="x")  # tag only allows address|entity


def test_audit_catches_forced_dangling_ref(case):
    conn, db = case
    f = create_finding(conn, statement="forced")
    # Bypass the service to force a dangling poly ref -> the no-dangling-fk audit must catch it.
    conn.execute(
        "INSERT INTO finding_ref (id, finding_id, ref_type, ref_id, note) VALUES (?,?,?,?,?)",
        ("forced-ref", f, "address", "ghost-address", None))
    result = next(r for r in run_audits(db_path=str(db)) if r.name == "no-dangling-fk")
    assert not result.passed


# --- investigator display-label overrides (feature 4/5; migration 0008) ----------------------

def test_set_label_on_address_and_trace_resolve_and_audit(case):
    conn, db = case
    addr = _seed_address(conn)
    trace = create_trace(conn, name="Stolen funds")

    set_label(conn, target_type="address", target_id=addr, label="Lazarus cash-out")
    set_label(conn, target_type="trace", target_id=trace, label="Hop 1 → exchange")

    assert current_labels(conn, "address")[addr] == "Lazarus cash-out"
    assert current_labels(conn, "trace")[trace] == "Hop 1 → exchange"
    # Family C investigator construction (no source_query) — all invariant audits stay green.
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_set_label_is_append_only_latest_wins(case):
    conn, db = case
    addr = _seed_address(conn)
    set_label(conn, target_type="address", target_id=addr, label="first guess", now="2026-01-01T00:00:00Z")
    set_label(conn, target_type="address", target_id=addr, label="confirmed name", now="2026-01-02T00:00:00Z")
    # The most-recent label is the display value; the earlier row is retained (append-only history).
    assert current_labels(conn, "address")[addr] == "confirmed name"
    assert conn.execute("SELECT COUNT(*) FROM investigator_label WHERE target_id=?", (addr,)).fetchone()[0] == 2


def test_set_label_rejects_bad_input(case):
    conn, db = case
    addr = _seed_address(conn)
    with pytest.raises(ValueError):
        set_label(conn, target_type="address", target_id="nope", label="x")        # dangling target
    with pytest.raises(ValueError):
        set_label(conn, target_type="entity", target_id=addr, label="x")           # label only allows address|trace
    with pytest.raises(ValueError):
        set_label(conn, target_type="address", target_id=addr, label="   ")        # empty/blank label


def test_audit_catches_forced_dangling_investigator_label(case):
    conn, db = case
    # Force a dangling label poly ref (bypass the service) -> the no-dangling-fk audit must catch it.
    conn.execute(
        "INSERT INTO investigator_label (id, target_type, target_id, label, created_at) VALUES (?,?,?,?,?)",
        ("forced-label", "address", "ghost-address", "x", "2026-01-01T00:00:00Z"))
    result = next(r for r in run_audits(db_path=str(db)) if r.name == "no-dangling-fk")
    assert not result.passed
    assert any(o.get("kind") == "investigator_label.target_id" for o in result.offending)
