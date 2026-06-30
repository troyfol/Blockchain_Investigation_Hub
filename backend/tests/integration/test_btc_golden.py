"""Golden Bitcoin ingest (phase_03 step 4/5) — the hard gate for the account/UTXO unification.

Drives the real EsploraConnector with httpx mocked, asserting the transaction-as-node + UTXO
shape, that NO transfer rows are created (Invariant #5), the v_value_movement null-src rows,
finality, balance, idempotency, and the spent-marking when a spender is ingested.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from backend.app.audits.runner import run_audits
from backend.app.config import get_settings
from backend.app.connectors.base import RateLimiter
from backend.app.connectors.esplora import EsploraConnector
from backend.app.db import apply_migrations, get_connection
from backend.app.db import repository as repo

CASSETTES = Path(__file__).resolve().parent.parent / "cassettes" / "esplora"
G = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
TIP = "800010"


def _addr_txs():
    return json.loads((CASSETTES / "address_txs.json").read_text())


def _stats():
    return json.loads((CASSETTES / "address_stats.json").read_text())


def _router(request):
    p = request.url.path
    if p.endswith("/blocks/tip/height"):
        return httpx.Response(200, text=TIP)
    if "/address/" in p and p.endswith("/txs"):
        return httpx.Response(200, json=_addr_txs())
    if "/address/" in p and "/txs/chain/" in p:
        return httpx.Response(200, json=[])  # no further pages
    if "/address/" in p:
        return httpx.Response(200, json=_stats())
    return httpx.Response(404)


@pytest.fixture
def case(tmp_path):
    db = tmp_path / "case.db"
    apply_migrations(db)
    conn = get_connection(db)
    repo.init_case(conn, title="BTC Golden")
    yield conn, db
    conn.close()


@pytest.fixture
def connector():
    c = EsploraConnector(settings=get_settings(), rate_limiter=RateLimiter(0, enabled=False),
                         sleep=lambda _s: None)
    yield c
    c.close()


@respx.mock
@pytest.mark.smoke
def test_btc_transaction_as_node(case, connector):
    conn, db = case
    respx.route(host="blockstream.info").mock(side_effect=_router)

    connector.get_transactions(conn, "bitcoin", G)
    connector.get_balance(conn, "bitcoin", G)

    # Transaction-as-node + UTXO facts only — NEVER a transfer (Invariant #5).
    assert conn.execute("SELECT COUNT(*) FROM transaction_").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM tx_input").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM tx_output").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM transfer").fetchone()[0] == 0

    assert conn.execute("SELECT finality_status FROM transaction_").fetchone()[0] == "final"

    # address -> transaction -> address shape: outputs carry addresses + the tx.
    out = conn.execute("""SELECT a.address, t.tx_hash FROM tx_output o
        JOIN address a ON a.id=o.address_id JOIN transaction_ t ON t.id=o.transaction_id
        WHERE a.address=?""", (G,)).fetchone()
    assert out is not None and out["tx_hash"]

    # v_value_movement: every UTXO row has NULL src (no fabricated edge) + native BTC asset.
    rows = conn.execute(
        "SELECT src_address_id, dst_address_id, asset_id FROM v_value_movement WHERE paradigm='utxo'"
    ).fetchall()
    assert len(rows) == 2
    assert all(r["src_address_id"] is None for r in rows)
    assert all(r["dst_address_id"] is not None and r["asset_id"] is not None for r in rows)

    assert conn.execute("SELECT amount FROM balance_snapshot").fetchone()["amount"] == "120000"

    results = run_audits(db_path=str(db))
    failed = [(r.name, r.offending) for r in results if not r.passed]
    assert failed == [], f"audits failed: {failed}"
    assert any(r.name == "no-fabricated-utxo-edge" and r.passed for r in results)  # audit #5


@respx.mock
def test_btc_idempotent_refetch(case, connector):
    conn, db = case
    respx.route(host="blockstream.info").mock(side_effect=_router)
    tables = ("transaction_", "tx_input", "tx_output", "address", "asset")
    connector.get_transactions(conn, "bitcoin", G)
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    connector.get_transactions(conn, "bitcoin", G)
    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    assert before == after
    assert all(r.passed for r in run_audits(db_path=str(db)))


@respx.mock
def test_spent_output_marked_when_spender_ingested(case, connector):
    conn, db = case
    t1, t2 = "1" * 64, "2" * 64
    addr_x = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
    tx1 = {"txid": t1, "fee": 100, "status": {"confirmed": True, "block_height": 799000,
           "block_time": 1690000000}, "vin": [{"is_coinbase": True}],
           "vout": [{"scriptpubkey_address": addr_x, "scriptpubkey_type": "v0_p2wpkh", "value": 100000}]}
    tx2 = {"txid": t2, "fee": 100, "status": {"confirmed": True, "block_height": 799001,
           "block_time": 1690000600},
           "vin": [{"is_coinbase": False, "txid": t1, "vout": 0,
                    "prevout": {"scriptpubkey_address": addr_x, "value": 100000}}],
           "vout": [{"scriptpubkey_address": "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy",
                     "scriptpubkey_type": "p2sh", "value": 99900}]}

    def router(request):
        p = request.url.path
        if p.endswith("/blocks/tip/height"):
            return httpx.Response(200, text=TIP)
        if p.endswith(f"/tx/{t1}"):
            return httpx.Response(200, json=tx1)
        if p.endswith(f"/tx/{t2}"):
            return httpx.Response(200, json=tx2)
        return httpx.Response(404)

    respx.route(host="blockstream.info").mock(side_effect=router)
    connector.get_transfers(conn, "bitcoin", t1)  # ingest tx1 (has the output)
    connector.get_transfers(conn, "bitcoin", t2)  # ingest tx2 (spends tx1:0)

    spent = conn.execute("""SELECT o.spent, st.tx_hash AS spender FROM tx_output o
        JOIN transaction_ t ON t.id=o.transaction_id
        LEFT JOIN transaction_ st ON st.id=o.spending_tx_id
        WHERE t.tx_hash=? AND o.output_index=0""", (t1,)).fetchone()
    assert spent["spent"] == 1 and spent["spender"] == t2  # output marked spent by tx2
    assert all(r.passed for r in run_audits(db_path=str(db)))


@respx.mock
def test_intra_batch_linkage_resolves_regardless_of_stream_order(case, connector):
    """A single address sync must link an input to a prev-output from the SAME batch even when Esplora
    streams the spending tx BEFORE its funding tx (newest-first order). The two-pass write (all outputs,
    then resolve inputs) guarantees it — a Colonial Pipeline validation regression guard."""
    conn, db = case
    fund, spend = "a" * 64, "b" * 64
    addr = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"

    def tx(txid, *, h, vin, vout):
        return {"txid": txid, "fee": 100,
                "status": {"confirmed": True, "block_height": h, "block_time": 1690000000},
                "vin": vin, "vout": vout}

    funding = tx(fund, h=799000, vin=[{"is_coinbase": True}],
                 vout=[{"scriptpubkey_address": addr, "value": 100000}])
    spending = tx(spend, h=799001,
                  vin=[{"is_coinbase": False, "txid": fund, "vout": 0,
                        "prevout": {"scriptpubkey_address": addr, "value": 100000}}],
                  vout=[{"scriptpubkey_address": "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy", "value": 99900}])
    page = [spending, funding]  # newest-first: the spender is streamed BEFORE its funding tx

    def router(request):
        p = request.url.path
        if p.endswith("/blocks/tip/height"):
            return httpx.Response(200, text=TIP)
        if "/txs/chain/" in p:
            return httpx.Response(200, json=[])
        if p.endswith("/txs"):
            return httpx.Response(200, json=page)
        return httpx.Response(404)

    respx.route(host="blockstream.info").mock(side_effect=router)
    connector.get_transactions(conn, "bitcoin", addr)  # ONE sync

    # The funding output is marked spent AND the spending input back-references it — resolved in one pass.
    row = conn.execute(
        """SELECT o.spent, o.spending_tx_id, i.id AS in_id FROM tx_output o
           JOIN transaction_ t ON t.id=o.transaction_id
           LEFT JOIN tx_input i ON i.prev_output_id=o.id
           WHERE t.tx_hash=? AND o.output_index=0""", (fund,)).fetchone()
    assert row["spent"] == 1 and row["spending_tx_id"] is not None and row["in_id"] is not None
    assert all(r.passed for r in run_audits(db_path=str(db)))


@respx.mock
def test_btc_cursor_pagination_across_pages(case, connector):
    conn, db = case

    def mk(i):  # a confirmed tx with one output to G
        return {"txid": f"{i:064x}", "fee": 0,
                "status": {"confirmed": True, "block_height": 800000 - i, "block_time": 1690000000},
                "vin": [{"is_coinbase": True}],
                "vout": [{"scriptpubkey_address": G, "scriptpubkey_type": "v0_p2wpkh", "value": 1000}]}

    page1 = [mk(i) for i in range(25)]   # full confirmed page -> triggers a cursor fetch
    page2 = [mk(100)]                    # one more -> stop

    def router(request):
        p = request.url.path
        if p.endswith("/blocks/tip/height"):
            return httpx.Response(200, text=TIP)
        if "/txs/chain/" in p:
            return httpx.Response(200, json=page2)
        if p.endswith("/txs"):
            return httpx.Response(200, json=page1)
        return httpx.Response(404)

    respx.route(host="blockstream.info").mock(side_effect=router)
    connector.get_transactions(conn, "bitcoin", G)
    # 25 (page 1) + 1 (page 2) distinct txs, no duplicates/misses from the cursor logic.
    assert conn.execute("SELECT COUNT(*) FROM transaction_").fetchone()[0] == 26
    assert all(r.passed for r in run_audits(db_path=str(db)))


@respx.mock
def test_page1_mempool_and_confirmed_split(case, connector):
    """Page 1 = up to 50 mempool + first 25 confirmed (RE-CONFIRMED 2026-06-28): mempool rows land
    provisional, and the pagination cursor keys off the last CONFIRMED txid, not a mempool one."""
    conn, db = case

    def tx(txid, *, confirmed, block_height=None):
        return {"txid": txid, "fee": 100,
                "status": {"confirmed": confirmed, "block_height": block_height,
                           "block_time": 1690000000 if confirmed else None},
                "vin": [{"txid": "p", "vout": 0, "prevout": {"scriptpubkey_address": "bc1qsrc", "value": 1000}}],
                "vout": [{"scriptpubkey_address": "bc1qdst", "value": 900}]}

    mempool = [tx("mempool0", confirmed=False), tx("mempool1", confirmed=False)]
    confirmed = [tx(f"conf{i:02d}", confirmed=True, block_height=800000 - i) for i in range(25)]
    page1 = mempool + confirmed  # Esplora order: mempool first, then confirmed (newest-first)
    paths = []

    def router(request):
        p = request.url.path
        paths.append(p)
        if p.endswith("/blocks/tip/height"):
            return httpx.Response(200, text=TIP)
        if "/txs/chain/" in p:
            return httpx.Response(200, json=[])  # page 2: no further confirmed
        if p.endswith("/txs"):
            return httpx.Response(200, json=page1)
        return httpx.Response(404)

    respx.route(host="blockstream.info").mock(side_effect=router)
    res = connector.get_transactions(conn, "bitcoin", G)
    assert res["transactions"] == 27  # 2 mempool + 25 confirmed, all handled

    mp = conn.execute(
        "SELECT finality_status, block_height, status FROM transaction_ WHERE tx_hash='mempool0'").fetchone()
    assert mp["finality_status"] == "provisional" and mp["block_height"] is None and mp["status"] == "mempool"
    cf = conn.execute("SELECT finality_status, status FROM transaction_ WHERE tx_hash='conf00'").fetchone()
    assert cf["status"] == "confirmed" and cf["finality_status"] == "final"  # 11 conf >= 6

    # The cursor is the last CONFIRMED txid ("conf24"), never a mempool txid.
    assert any(p.endswith("/txs/chain/conf24") for p in paths)
    assert not any("mempool" in p for p in paths if "/txs/chain/" in p)
    assert all(r.passed for r in run_audits(db_path=str(db)))
