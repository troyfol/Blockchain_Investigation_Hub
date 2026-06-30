"""Golden entity-resolution tests (phase_06): co-spend clustering + CoinJoin flagging."""

from __future__ import annotations

import pytest

from backend.app.audits.runner import run_audits
from backend.app.db import repository as repo
from backend.app.models import Address, SourceQuery
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.entities import cluster_cospend, link_same_address, resolve
from backend.tests.integration._helpers import new_case, seed_btc_custom


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="Entities")
    yield conn, db
    conn.close()


def _cospend_memberships(conn):
    return conn.execute("SELECT * FROM entity_membership WHERE method='co-spend'").fetchall()


@pytest.mark.smoke
def test_cospend_cluster_forms(case):
    conn, db = case
    seed_btc_custom(conn, txid="a" * 64, input_addrs=["1InA", "1InB"], output_amounts=[120_000, 79_000])
    stats = cluster_cospend(conn)
    assert stats["clusters"] == 1 and stats["entities_created"] == 1 and stats["memberships_created"] == 2

    ms = _cospend_memberships(conn)
    assert len(ms) == 2
    assert all(m["source"] == "cospend-heuristic" and m["confidence"] == 0.9 and m["flags"] is None for m in ms)
    assert len({m["entity_id"] for m in ms}) == 1  # both addresses in the same entity
    assert all(r.passed for r in run_audits(db_path=str(db)))


@pytest.mark.smoke
def test_known_coinjoin_flags_memberships(case):
    conn, db = case
    seed_btc_custom(conn, txid="b" * 64, input_addrs=[f"1cj{i}" for i in range(5)],
                    output_amounts=[100_000] * 5)  # Whirlpool 0.001 equal-output pattern
    cluster_cospend(conn)
    ms = _cospend_memberships(conn)
    assert len(ms) == 5
    assert all(m["flags"] == "possible-coinjoin" and m["confidence"] == 0.5 for m in ms)
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_cospend_is_idempotent(case):
    conn, db = case
    seed_btc_custom(conn, txid="c" * 64, input_addrs=["1x", "1y"], output_amounts=[1, 2])
    cluster_cospend(conn)
    n1 = conn.execute("SELECT COUNT(*) FROM entity_membership").fetchone()[0]
    cluster_cospend(conn)  # re-run -> insert-once, no duplicate memberships
    n2 = conn.execute("SELECT COUNT(*) FROM entity_membership").fetchone()[0]
    assert n1 == n2 == 2
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_new_tx_bridges_clusters_via_merge(case):
    conn, db = case
    seed_btc_custom(conn, txid="d" * 64, input_addrs=["1a", "1b"], output_amounts=[1, 2])
    cluster_cospend(conn)
    # A later tx shares 1b with 1c -> bridges the two clusters into one (merged_into, no rewrites).
    seed_btc_custom(conn, txid="e" * 64, input_addrs=["1b", "1c"], output_amounts=[1, 2])
    cluster_cospend(conn)
    resolved = {resolve(conn, m["entity_id"]) for m in _cospend_memberships(conn)}
    assert len(resolved) == 1  # 1a, 1b, 1c all resolve to one entity
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_same_address_heuristic_is_idempotent(case):
    conn, db = case
    hexaddr = "0x" + "a" * 40
    sq = SourceQuery(connector="x", capability="seed", endpoint="local", params={},
                     requested_at="2026-01-01T00:00:00Z", status="ok")

    def seed(c, sqid):
        repo.upsert_address(c, Address(chain="ethereum", address_display=hexaddr), sqid)
        repo.upsert_address(c, Address(chain="arbitrum", address_display=hexaddr), sqid)

    write_with_provenance(conn, sq, seed)

    link_same_address(conn)
    e1 = conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0]
    m1 = conn.execute("SELECT COUNT(*) FROM entity_membership").fetchone()[0]
    link_same_address(conn)  # re-run -> no duplicate entities/memberships
    assert conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0] == e1 == 1
    assert conn.execute("SELECT COUNT(*) FROM entity_membership").fetchone()[0] == m1 == 2
    assert all(r.passed for r in run_audits(db_path=str(db)))
