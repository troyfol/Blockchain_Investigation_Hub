"""Merge/split + display tests (phase_06): merge round-trips, no rewrites, contested display."""

from __future__ import annotations

import pytest

from backend.app.audits.runner import run_audits
from backend.app.db import repository as repo
from backend.app.models import Address, Entity, SourceQuery
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.entities import (
    cluster_cospend,
    merge_entities,
    resolve,
    set_canonical_membership,
    split_address,
)
from backend.app.services.entity_display import active_memberships, entity_display
from backend.tests.integration._helpers import make_membership, new_case, seed_btc_custom


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="Merge/Split")
    yield conn, db
    conn.close()


def _result(results, name):
    return next(r for r in results if r.name == name)


def test_merge_sets_pointer_and_resolution_chases(case):
    conn, db = case
    e1 = repo.insert_entity(conn, Entity(origin="investigator"))
    e2 = repo.insert_entity(conn, Entity(origin="investigator"))
    merge_entities(conn, into_id=e1, from_id=e2)
    assert resolve(conn, e2) == e1 and resolve(conn, e1) == e1
    assert _result(run_audits(db_path=str(db)), "entity-resolution-sanity").passed


def test_merge_is_cycle_safe(case):
    conn, db = case
    e1 = repo.insert_entity(conn, Entity(origin="investigator"))
    e2 = repo.insert_entity(conn, Entity(origin="investigator"))
    merge_entities(conn, into_id=e1, from_id=e2)  # e2 -> e1
    # Merging the other direction is a no-op (already the same group) — never forms a cycle.
    assert merge_entities(conn, into_id=e2, from_id=e1) == e1
    assert resolve(conn, e1) == e1 and resolve(conn, e2) == e1
    assert _result(run_audits(db_path=str(db)), "entity-resolution-sanity").passed


def test_audit_catches_forced_cycle(case):
    conn, db = case
    e1 = repo.insert_entity(conn, Entity(origin="investigator"))
    e2 = repo.insert_entity(conn, Entity(origin="investigator"))
    # Forcibly create a cycle (bypassing merge_entities) — audit #7 must catch it.
    conn.execute("UPDATE entity SET merged_into=? WHERE id=?", (e2, e1))
    conn.execute("UPDATE entity SET merged_into=? WHERE id=?", (e1, e2))
    assert not _result(run_audits(db_path=str(db)), "entity-resolution-sanity").passed


def test_split_round_trips_append_only(case):
    conn, db = case
    seed_btc_custom(conn, txid="f" * 64, input_addrs=["1p", "1q"], output_amounts=[1, 2])
    cluster_cospend(conn)
    m = conn.execute("SELECT id, entity_id, address_id FROM entity_membership WHERE method='co-spend' LIMIT 1").fetchone()

    new_e = split_address(conn, membership_id=m["id"], reason="missed-coinjoin")

    # Retraction is append-only; the original membership row is NOT rewritten or deleted.
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_membership_retraction WHERE membership_id=?", (m["id"],)).fetchone()[0] == 1
    assert conn.execute("SELECT 1 FROM entity_membership WHERE id=?", (m["id"],)).fetchone() is not None
    # The split-out address gets a NEW investigator membership to a NEW entity.
    assert conn.execute(
        "SELECT 1 FROM entity_membership WHERE address_id=? AND entity_id=? AND source='investigator'",
        (m["address_id"], new_e)).fetchone() is not None
    # The retracted membership no longer shows as active.
    active = active_memberships(conn, resolve(conn, m["entity_id"]))
    assert all(a["id"] != m["id"] for a in active)
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_display_contested_then_curated_canonical(case):
    conn, db = case
    sq = SourceQuery(connector="x", capability="seed", endpoint="local", params={},
                     requested_at="2026-01-01T00:00:00Z", status="ok")
    addr_holder = []
    write_with_provenance(conn, sq, lambda c, sqid: addr_holder.append(
        repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + "a" * 40), sqid)))
    addr = addr_holder[0]
    ent = repo.insert_entity(conn, Entity(origin="source", name="Acme"))

    # Two sources assert the same address -> contested (side-by-side, never collapsed).
    m_arkham = make_membership(conn, entity_id=ent, address_id=addr, source="arkham",
                               method="shared-label", connector="arkham-import")
    make_membership(conn, entity_id=ent, address_id=addr, source="misttrack",
                    method="shared-label", connector="misttrack-import")

    d = entity_display(conn, ent)
    assert d["status"] == "contested" and len(d["memberships"]) == 2
    assert {m["source"] for m in d["memberships"]} == {"arkham", "misttrack"}  # no synthetic merge

    # Investigator curates a canonical membership -> status canonical (both still shown).
    set_canonical_membership(conn, entity_id=ent, membership_id=m_arkham)
    d2 = entity_display(conn, ent)
    assert d2["status"] == "canonical" and d2["canonical_membership_id"] == m_arkham
    assert len(d2["memberships"]) == 2
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_retracted_membership_cannot_be_canonical(case):
    conn, db = case
    seed_btc_custom(conn, txid="9" * 64, input_addrs=["1r", "1s"], output_amounts=[1, 2])
    cluster_cospend(conn)
    m = conn.execute("SELECT id, entity_id FROM entity_membership WHERE method='co-spend' LIMIT 1").fetchone()
    split_address(conn, membership_id=m["id"], reason="test")  # retracts m

    with pytest.raises(ValueError):
        set_canonical_membership(conn, entity_id=m["entity_id"], membership_id=m["id"])

    # A forcibly-set retracted canonical is caught by audit #7.
    conn.execute("UPDATE entity SET canonical_membership_id=? WHERE id=?",
                 (m["id"], resolve(conn, m["entity_id"])))
    assert not _result(run_audits(db_path=str(db)), "entity-resolution-sanity").passed
