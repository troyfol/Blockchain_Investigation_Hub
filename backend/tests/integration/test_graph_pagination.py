"""P25/FN-20 — /api/graph scope + pagination: bound a LEA-scale case's projection.

`?address_id` scopes the projection to a node's neighbourhood (reusing the `focus_incident` DB-scan bound);
`?limit=N` returns the N highest-degree nodes as a bounded subgraph with a `meta` block. With NEITHER param
the payload is byte-identical to before (back-compat). Read-only — no fact is changed.

The case is a hub-and-spokes graph: 5 spoke addresses each send a transfer to one HUB, so HUB has degree 5
and each spoke degree 1 — deterministic degrees to assert the top-N selection.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app, get_case_db_path
from backend.app.services.graph import bound_subgraph, build_graph
from backend.tests.integration._helpers import new_case, seed_evm_address

HUB = "0x" + "aa" * 20
SPOKES = ["0x" + f"{i + 1:040x}" for i in range(5)]


def _seed(tmp_path):
    conn, db = new_case(tmp_path, title="Scale")
    for spoke in SPOKES:
        seed_evm_address(conn, spoke, counterparty=HUB)           # transfer spoke -> HUB
    hub_id = conn.execute("SELECT id FROM address WHERE address=?", (HUB.lower(),)).fetchone()["id"]
    spoke0_id = conn.execute("SELECT id FROM address WHERE address=?", (SPOKES[0].lower(),)).fetchone()["id"]
    conn.close()
    return db, hub_id, spoke0_id


@pytest.fixture
def client(tmp_path):
    db, hub_id, spoke0_id = _seed(tmp_path)
    app.dependency_overrides[get_case_db_path] = lambda: str(db)
    yield TestClient(app), hub_id, spoke0_id
    app.dependency_overrides.clear()


def test_limit_and_address_scope(client):
    cl, hub_id, spoke0_id = client

    # Default (no params): the full case — 6 address nodes (HUB + 5 spokes), 5 edges, and NO meta block
    # (byte-identical to before — back-compat).
    full = cl.get("/api/graph").json()
    assert len({n["id"] for n in full["nodes"]}) == 6 and len(full["edges"]) == 5
    assert "meta" not in full

    # ?limit=3 -> the 3 highest-degree nodes (HUB deg 5 + 2 spokes deg 1) as a bounded subgraph + meta.
    bounded = cl.get("/api/graph?limit=3").json()
    assert bounded["meta"] == {"total_nodes": 6, "returned_nodes": 3, "limit": 3, "truncated": True}
    ids = {n["id"] for n in bounded["nodes"]}
    assert f"addr:{hub_id}" in ids and len(ids) == 3
    # every returned edge is among the returned nodes (no dangling edge to a dropped spoke).
    assert all(e["source"] in ids and e["target"] in ids for e in bounded["edges"])

    # ?address_id=<a spoke> -> only that spoke's neighbourhood: the spoke + HUB (its single counterparty).
    scoped = cl.get(f"/api/graph?address_id={spoke0_id}").json()
    sids = {n["id"] for n in scoped["nodes"]}
    assert sids == {f"addr:{spoke0_id}", f"addr:{hub_id}"} and len(scoped["edges"]) == 1

    # a limit that covers everything -> intact graph, truncated False.
    big = cl.get("/api/graph?limit=99").json()
    assert big["meta"]["truncated"] is False and len(big["nodes"]) == 6


def test_bad_scope_params_are_clean_errors(client):
    cl, _, _ = client
    assert cl.get("/api/graph?address_id=does-not-exist").status_code == 404
    assert cl.get("/api/graph?limit=0").status_code == 400
    assert cl.get("/api/graph?limit=-5").status_code == 400


def test_default_payload_is_byte_identical(tmp_path):
    # The no-param endpoint result must equal build_graph(conn) exactly (no scope/meta leakage).
    conn, db = new_case(tmp_path, title="Compat")
    for spoke in SPOKES:
        seed_evm_address(conn, spoke, counterparty=HUB)
    direct = build_graph(conn)
    conn.close()
    app.dependency_overrides[get_case_db_path] = lambda: str(db)
    try:
        via_api = TestClient(app).get("/api/graph").json()
    finally:
        app.dependency_overrides.clear()
    assert via_api == direct


def test_bound_subgraph_is_deterministic_and_read_only(tmp_path):
    # bound_subgraph is a pure reshaping: same input + limit -> identical subgraph; input graph untouched.
    conn, _ = new_case(tmp_path, title="Determinism")
    for spoke in SPOKES:
        seed_evm_address(conn, spoke, counterparty=HUB)
    graph = build_graph(conn)
    conn.close()
    before_nodes = len(graph["nodes"])
    assert bound_subgraph(graph, 3) == bound_subgraph(graph, 3)   # deterministic
    assert len(graph["nodes"]) == before_nodes                    # input graph not mutated
