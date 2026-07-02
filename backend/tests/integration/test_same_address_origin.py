"""Batch 12 (COR-04): the same-address heuristic must mint entities with the HONEST machine origin
`heuristic-cluster` (migration 0010's purpose) — not `investigator`, which would make a machine cluster
indistinguishable, in `entity.origin`, from a human-authored group."""

from __future__ import annotations

from backend.app.db import repository as repo
from backend.app.models import Address, SourceQuery
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.entities import link_same_address
from backend.tests.integration._helpers import new_case


def test_same_address_entity_has_heuristic_origin(tmp_path):
    conn, db = new_case(tmp_path)
    same = "0x" + "ab" * 20  # the SAME EVM address seen on two chains

    def w(c, sqid):
        repo.upsert_address(c, Address(chain="ethereum", address_display=same), sqid)
        repo.upsert_address(c, Address(chain="polygon", address_display=same), sqid)

    write_with_provenance(conn, SourceQuery(
        connector="etherscan", capability="get_transactions", endpoint="txlist",
        params={"bounds": "default"}, requested_at="2026-01-01T00:00:00Z", status="ok"), w)

    res = link_same_address(conn)
    assert res["linked"] == 2
    origins = {r[0] for r in conn.execute(
        "SELECT origin FROM entity WHERE entity_type='same-address'").fetchall()}
    assert origins == {"heuristic-cluster"}, f"same-address entity has wrong origin: {origins} (COR-04)"
    assert "investigator" not in origins
    conn.close()
