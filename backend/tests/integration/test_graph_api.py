"""Graph read-API integration tests (phase_04 step 4).

Seeds a real EVM + BTC case, then asserts the /api/graph projection: EVM address->address edges,
BTC routing through visible transaction nodes (never a fabricated input->output edge), and
provisional-vs-final flagging. Also exercises bounded expansion + the partial flag.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.db import apply_migrations, get_connection
from backend.app.db import repository as repo
from backend.app.main import app, get_case_db_path, get_orchestrator
from backend.app.models import Address, Asset, SourceQuery, Transaction, Transfer
from backend.app.provenance.atomic import write_with_provenance
from backend.tests.integration.test_seeded_case import ETH_FROM, seed_btc_tx, seed_evm_transfer


def _seed(tmp_path, *, evm_final=True):
    db = tmp_path / "case.db"
    apply_migrations(db)
    conn = get_connection(db)
    repo.init_case(conn, title="Graph Case")
    seed_evm_transfer(conn, final=evm_final)
    seed_btc_tx(conn)
    conn.close()
    return db


@pytest.fixture
def client(tmp_path):
    db = _seed(tmp_path)
    app.dependency_overrides[get_case_db_path] = lambda: str(db)
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.mark.smoke
def test_graph_renders_heterogeneous(client):
    g = client.get("/api/graph").json()
    nodes = {n["id"]: n for n in g["nodes"]}
    edges = g["edges"]

    # EVM: a single address -> address transfer edge.
    evm = [e for e in edges if e["kind"] == "transfer"]
    assert len(evm) == 1
    assert nodes[evm[0]["source"]]["kind"] == "address"
    assert nodes[evm[0]["target"]]["kind"] == "address"

    # Bitcoin: ONE visible transaction routing node; outputs go tx->address, inputs address->tx.
    tx_nodes = [n for n in g["nodes"] if n["kind"] == "transaction"]
    assert len(tx_nodes) == 1
    txid = tx_nodes[0]["id"]
    outs = [e for e in edges if e["kind"] == "tx_output"]
    ins = [e for e in edges if e["kind"] == "tx_input"]
    # P8.7.3 #3 — parallel same-(source,target,asset) facts may collapse into a rollup carrying ``count`` +
    # ``underlying`` (here the 2 same-address inputs fold to one ×2 edge). Count MOVEMENTS via ``count``.
    def _n(es):
        return sum(e.get("count", 1) for e in es)
    assert _n(outs) == 2 and _n(ins) == 2
    assert all(e["source"] == txid for e in outs)   # tx -> output address
    assert all(e["target"] == txid for e in ins)    # input address -> tx

    # Invariant #5: no fabricated direct input->output (UTXO) edge ever appears.
    assert not any(e["paradigm"] == "utxo" and e["kind"] == "transfer" for e in edges)

    assert len([n for n in g["nodes"] if n["kind"] == "address"]) == 5  # 2 EVM + 3 BTC


@pytest.mark.smoke
def test_graph_flags_provisional(tmp_path):
    db = _seed(tmp_path, evm_final=False)  # the EVM tx is provisional
    app.dependency_overrides[get_case_db_path] = lambda: str(db)
    try:
        g = TestClient(app).get("/api/graph").json()
    finally:
        app.dependency_overrides.clear()
    statuses = {e["kind"]: e["finality_status"] for e in g["edges"]}
    assert statuses["transfer"] == "provisional"  # EVM seeded provisional
    assert statuses["tx_output"] == "final"        # BTC seeded final
    # transaction node also carries finality for styling
    assert [n for n in g["nodes"] if n["kind"] == "transaction"][0]["finality_status"] == "final"


def test_graph_reads_view_no_paradigm_branch(client):
    # Every edge is one of the unified kinds; the frontend never needs the chain to render.
    g = client.get("/api/graph").json()
    assert {e["kind"] for e in g["edges"]} <= {"transfer", "tx_output", "tx_input"}


def test_node_labels_are_short_and_marker_free(client):
    """Labels stay legible (≤ 2 lines, never crowded): a tx hash is aliased the SAME way as an address
    (first4…last4), and status markers (★/⛔/⚠) are drawn ON the glyph by the stylesheet — never the text."""
    from backend.app.services.graph import _alias

    g = client.get("/api/graph").json()

    tx = [n for n in g["nodes"] if n["kind"] == "transaction"]
    assert tx, "expected a Bitcoin transaction routing node"
    for n in tx:
        # The tx label uses the same first4…last4 alias as addresses (a real 64-char hash collapses to
        # 'abcd…wxyz'); the full hash stays in `tx_hash` for hover + the SidePanel.
        assert n["label"] == _alias(n["tx_hash"])

    # A long (real) hash MUST collapse to the short alias — guards the aliasing itself.
    long_hash = "e5015b6e" + "0" * 50 + "cf68d8"
    assert _alias(long_hash) == "e501…68d8" and len(_alias(long_hash)) == 9

    # No status glyph is ever baked into a label line (markers live on the glyph, not the text).
    for n in g["nodes"]:
        assert not any(ch in (n.get("label") or "") for ch in ("★", "⛔", "⚠"))
        # Address labels are at most two lines (entity over alias).
        if n["kind"] == "address":
            assert (n.get("label") or "").count("\n") <= 1


# --- investigator display-label override (feature 4) ---------------------------------------

def test_custom_investigator_label_takes_display_precedence(tmp_path):
    """An investigator's custom label WINS over the auto entity/alias label, and flags the node — but
    the underlying address fact is untouched (a display claim, never a rewrite of a fact)."""
    from backend.app.db import get_connection
    from backend.app.services.graph import build_graph
    from backend.app.services.investigator import set_label

    db = _seed(tmp_path)
    conn = get_connection(db)
    try:
        g0 = build_graph(conn)
        addr_node = next(n for n in g0["nodes"] if n["kind"] == "address")
        addr_id = addr_node["id"].replace("addr:", "")
        auto_label = addr_node["label"]

        set_label(conn, target_type="address", target_id=addr_id, label="Lazarus cash-out wallet")

        node = next(n for n in build_graph(conn)["nodes"] if n["id"] == addr_node["id"])
        assert node["label"].startswith("Lazarus cash-out")   # custom label shown (capped to one line)
        assert node.get("custom_label") is True
        assert node["label"] != auto_label
        assert node["address"] == addr_node["address"]        # the address fact is unchanged
    finally:
        conn.close()


def test_set_address_label_endpoint_updates_graph(client):
    g = client.get("/api/graph").json()
    addr_node = next(n for n in g["nodes"] if n["kind"] == "address")
    addr_id = addr_node["id"].replace("addr:", "")

    resp = client.post(f"/api/address/{addr_id}/label", json={"label": "renamed wallet"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    node = next(n for n in body["graph"]["nodes"] if n["id"] == addr_node["id"])
    assert node["label"] == "renamed wallet" and node["custom_label"] is True


def test_set_address_label_rejects_unknown_and_blank(client):
    g = client.get("/api/graph").json()
    addr_id = next(n for n in g["nodes"] if n["kind"] == "address")["id"].replace("addr:", "")
    assert client.post("/api/address/ghost/label", json={"label": "x"}).status_code == 404
    assert client.post(f"/api/address/{addr_id}/label", json={"label": "   "}).status_code == 400


def test_traces_endpoint_empty_when_no_traces(client):
    assert client.get("/api/traces").json() == {"traces": []}


# --- bounded expansion ---------------------------------------------------------------------

class _StubOrchestrator:
    """Writes one new transfer (and a partial source_query) so we can assert expand + partial."""

    NEW_TO = "0x" + "9" * 40

    def get_transactions(self, conn, chain, address, bounds):
        sq = SourceQuery(connector="stub", capability="get_transactions", endpoint="txlist",
                         params={"address": address, "bounds": bounds or "default"},
                         requested_at="2026-06-27T00:00:00Z",
                         status="partial" if (bounds or {}).get("max_pages") else "ok")

        def write(c, sqid):
            asset = repo.upsert_asset(c, Asset(chain=chain, symbol="ETH", decimals=18), sqid)
            a = repo.upsert_address(c, Address(chain=chain, address_display=address), sqid)
            b = repo.upsert_address(c, Address(chain=chain, address_display=self.NEW_TO), sqid)
            tx = repo.upsert_transaction(c, Transaction(
                chain=chain, tx_hash="0x" + "e" * 64, confirmations=100, finality_status="final"), sqid)
            repo.upsert_transfer(c, Transfer(transaction_id=tx, chain=chain, from_address_id=a,
                                             to_address_id=b, asset_id=asset, amount="5",
                                             transfer_type="native", position=0), sqid)

        write_with_provenance(conn, sq, write)


@pytest.fixture
def client_expand(tmp_path):
    db = _seed(tmp_path)
    app.dependency_overrides[get_case_db_path] = lambda: str(db)
    app.dependency_overrides[get_orchestrator] = lambda: _StubOrchestrator()
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_expand_pulls_and_surfaces_partial(client_expand):
    before = len(client_expand.get("/api/graph").json()["edges"])
    resp = client_expand.post("/api/graph/expand",
                              json={"chain": "ethereum", "address": ETH_FROM, "bounds": {"max_pages": 1}})
    body = resp.json()
    assert body["partial"] is True                       # the stub's source_query was 'partial'
    assert len(body["graph"]["edges"]) == before + 1     # one new transfer edge


def test_expand_without_bounds_is_not_partial(client_expand):
    body = client_expand.post("/api/graph/expand",
                              json={"chain": "ethereum", "address": ETH_FROM}).json()
    assert body["partial"] is False


# --- robustness ----------------------------------------------------------------------------

def test_graph_503_when_case_missing(tmp_path):
    app.dependency_overrides[get_case_db_path] = lambda: str(tmp_path / "nope.db")
    try:
        resp = TestClient(app).get("/api/graph")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 503  # clear "run make migrate", not a confusing 500


def test_graph_503_when_unmigrated(tmp_path):
    db = tmp_path / "empty.db"
    db.touch()  # exists but has no schema
    app.dependency_overrides[get_case_db_path] = lambda: str(db)
    try:
        resp = TestClient(app).get("/api/graph")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 503


def test_expand_unknown_chain_returns_error_not_500(client):
    # Default orchestrator: no connector serves 'dogecoin' -> graceful {error}, not a 500.
    resp = client.post("/api/graph/expand", json={"chain": "dogecoin", "address": "0x" + "a" * 40})
    assert resp.status_code == 200
    body = resp.json()
    assert "error" in body and body["partial"] is False
