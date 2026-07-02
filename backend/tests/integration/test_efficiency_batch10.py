"""Batch 10 (EFF-02 / EFF-01 / COR-03): behavior-preserving efficiency + Decimal display totals.

- EFF-02: `_resolve_prev_output` / `_seed_address_id` must use INDEXED SEEKS (add the constant chain), not
  full table SCANs (EXPLAIN QUERY PLAN).
- EFF-01: the label endpoints must not build + return a full graph the client discards.
- COR-03: graph display totals must match a Decimal sum exactly (no float sub-cent drift).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from backend.app.db import repository as repo
from backend.app.main import app
from backend.app.models import (Address, Asset, SourceQuery, Transaction, Transfer, TxInput, TxOutput,
                                Valuation)
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services import cases
from backend.tests.integration._helpers import new_case


def _plan(conn, sql, params) -> str:
    rows = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return " | ".join(str(r[-1]) for r in rows)


def test_eff02_resolve_prev_output_uses_index(tmp_path):
    conn, db = new_case(tmp_path)
    plan = _plan(conn,
                 "SELECT o.id FROM tx_output o JOIN transaction_ t ON t.id=o.transaction_id "
                 "WHERE t.chain='bitcoin' AND t.tx_hash=? AND o.output_index=?", ("a" * 64, 0))
    assert "SCAN o" not in plan and "SCAN tx_output" not in plan, f"tx_output still SCANned: {plan}"
    assert "ux_transaction" in plan or "SEARCH t" in plan, f"transaction_ not seeked: {plan}"
    conn.close()


def test_eff02_seed_address_uses_index(tmp_path):
    conn, db = new_case(tmp_path)
    plan = _plan(conn, "SELECT id FROM address WHERE chain=? AND address=?", ("bitcoin", "1abc"))
    assert "SCAN" not in plan, f"address still SCANned: {plan}"
    conn.close()


def test_eff01_label_endpoint_returns_no_graph(tmp_path, monkeypatch):
    monkeypatch.setenv("BIH_APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("BIH_CASES_ROOT", str(tmp_path / "cases_root"))
    cases.clear_active_case()
    cases.new_case("Label Perf")
    from backend.app.db import get_connection
    from backend.app.services.cases import active_case_path
    conn = get_connection(active_case_path())
    sq = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="txlist",
                     params={"address": "probe", "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")
    aid = {}

    def w(c, sqid):
        aid["id"] = repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + "11" * 20), sqid)

    write_with_provenance(conn, sq, w)
    conn.close()

    client = TestClient(app)
    r = client.post(f"/api/address/{aid['id']}/label", json={"label": "Exchange"})
    assert r.status_code == 200
    assert "graph" not in r.json(), "the label endpoint still returns a discarded full graph (EFF-01)"
    cases.clear_active_case()


def test_cor03_decimal_display_totals(tmp_path):
    conn, db = new_case(tmp_path)
    sq = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="txlist",
                     params={"address": "probe", "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")
    # Three ETH transfers A→B, each valued at a price that sums to a value float would drift on.
    vals = ["0.10", "0.20", "0.30"]  # exact sum 0.60; float 0.1+0.2+0.3 = 0.6000000000000001

    def w(c, sqid):
        asset = repo.upsert_asset(c, Asset(chain="ethereum", symbol="ETH", decimals=18), sqid)
        a = repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + "aa" * 20), sqid)
        b = repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + "bb" * 20), sqid)
        for i, v in enumerate(vals):
            tx = repo.upsert_transaction(c, Transaction(chain="ethereum", tx_hash="0x" + f"{i:02x}" * 32,
                                         block_height=100 + i, block_ts="2026-01-01T00:00:00Z",
                                         confirmations=100, finality_status="final"), sqid, authoritative=True)
            tr = repo.upsert_transfer(c, Transfer(transaction_id=tx, chain="ethereum", from_address_id=a,
                                      to_address_id=b, asset_id=asset, amount="1000000000000000000",
                                      transfer_type="erc20", position=0), sqid)
            repo.insert_valuation(c, Valuation(subject_type="transfer", subject_id=tr, currency="USD",
                                  unit_price=v, value=v, price_timestamp="2026-01-01T00:00:00Z",
                                  source="defillama", retrieved_at="2026-01-01T00:00:00Z"), sqid)

    write_with_provenance(conn, sq, w)

    from backend.app.services.graph import build_graph
    g = build_graph(conn)
    b_node = next(n for n in g["nodes"] if n.get("val") and n["val"].get("in_usd"))
    expected = float(sum(Decimal(v) for v in vals))  # 0.6 exactly
    assert b_node["val"]["in_usd"] == expected, "USD rollup drifted from the Decimal sum (COR-03)"
    conn.close()
