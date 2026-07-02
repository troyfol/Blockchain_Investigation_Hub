"""Batch 9 (LOG-04 / LOG-07): the trace-CONSTRUCTION service is now reachable through HTTP routes — a
trace can be created, populated (EVM edge / BTC FIFO / manual link), and rendered end-to-end. Re-running
FIFO does not append duplicate `trace_btc_link` rows (LOG-07 idempotency). Previously the app could only
list/rename/render traces, so `/api/traces` was permanently empty (a headline capability was unreachable).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.db import repository as repo
from backend.app.main import app
from backend.app.models import Address, Asset, SourceQuery, Transaction, Transfer, TxInput, TxOutput
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services import cases


@pytest.fixture
def client_with_case(tmp_path, monkeypatch):
    monkeypatch.setenv("BIH_APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("BIH_CASES_ROOT", str(tmp_path / "cases_root"))
    monkeypatch.delenv("BIH_CASE_DB", raising=False)
    cases.clear_active_case()
    cases.new_case("Trace Build")
    from backend.app.services.cases import active_case_path
    from backend.app.db import get_connection
    conn = get_connection(active_case_path())
    ids = {}

    sq = SourceQuery(connector="esplora", capability="get_transactions", endpoint="address-txs",
                     params={"address": "probe", "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")

    def write(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)
        a = repo.upsert_address(c, Address(chain="bitcoin", address_display="1A"), sqid)
        tx0 = repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="0" * 64, block_height=1,
                                      confirmations=50, finality_status="final"), sqid, authoritative=True)
        o0 = repo.upsert_tx_output(c, TxOutput(transaction_id=tx0, address_id=a, amount="100", output_index=0), sqid)
        tx1 = repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="1" * 64, block_height=2,
                                      confirmations=49, finality_status="final"), sqid, authoritative=True)
        repo.upsert_tx_input(c, TxInput(transaction_id=tx1, prev_output_id=o0, address_id=a,
                                        amount="100", input_index=0), sqid)
        repo.upsert_tx_output(c, TxOutput(transaction_id=tx1, address_id=a, amount="90", output_index=0), sqid)
        ids["tx1"] = tx1

    write_with_provenance(conn, sq, write)
    conn.close()
    yield TestClient(app), ids
    cases.clear_active_case()


def test_build_trace_through_route_and_fifo_is_idempotent(client_with_case):
    client, ids = client_with_case

    # 0. No traces yet.
    assert client.get("/api/traces").json()["traces"] == []

    # 1. Create a trace.
    r = client.post("/api/trace", json={"name": "Stolen BTC path"})
    assert r.status_code == 200
    trace_id = r.json()["trace_id"]

    # 2. FIFO-apportion tx1 into the trace (BTC convention links).
    r = client.post(f"/api/trace/{trace_id}/fifo", json={"transaction_id": ids["tx1"]})
    assert r.status_code == 200 and r.json()["links_written"] == 1

    # 3. Re-run FIFO on the SAME trace/tx — must NOT append duplicate rows (LOG-07 idempotency).
    r2 = client.post(f"/api/trace/{trace_id}/fifo", json={"transaction_id": ids["tx1"]})
    assert r2.status_code == 200

    # 4. The trace lists with exactly ONE btc link (rendered), not two.
    traces = {t["id"]: t for t in client.get("/api/traces").json()["traces"]}
    assert traces[trace_id]["btc_link_count"] == 1, "re-running FIFO duplicated the link (LOG-07)"


def test_trace_routes_validate(client_with_case):
    client, _ = client_with_case
    # A missing trace → 404.
    assert client.post("/api/trace/nope/fifo", json={"transaction_id": "x"}).status_code == 404
    # An empty name → 400.
    assert client.post("/api/trace", json={"name": "  "}).status_code == 400
