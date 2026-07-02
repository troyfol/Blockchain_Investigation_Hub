"""Batch 3 (LOG-12 / LOG-01 / LOG-11 / LOG-13): re-fetch/upsert edge semantics must not silently lose
or corrupt facts. Each test exercises a routine re-fetch that the last-writer-wins conflict clauses got
wrong — invisible to `make audit` (the immutability audit excludes spent/status/spending columns), so
these assert the DB state directly.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from backend.app.config import get_settings
from backend.app.connectors.base import RateLimiter
from backend.app.connectors.etherscan import EtherscanConnector
from backend.app.db import repository as repo
from backend.app.models import Asset, SourceQuery, Transaction, TxInput, TxOutput
from backend.app.provenance.atomic import write_with_provenance
from backend.tests.integration._helpers import new_case

BASE = get_settings().etherscan_base_url


def _sq(endpoint="address-txs", connector="esplora"):
    return SourceQuery(connector=connector, capability="get_transactions", endpoint=endpoint,
                       params={"address": "probe", "bounds": "default"},
                       requested_at="2026-01-01T00:00:00Z", status="ok")


# --- LOG-01: a shared BTC funding tx re-fetch must not reset spent outputs to unspent ----------------

def test_log01_refetch_preserves_spent(tmp_path):
    conn, db = new_case(tmp_path)

    # Ingest funding tx F (output O) + spending tx S; S spends O so O.spent -> 1.
    def write1(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)
        f = repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="F" * 64,
                                    block_height=800000, confirmations=20, finality_status="final"), sqid)
        o = repo.upsert_tx_output(c, TxOutput(transaction_id=f, amount="100", output_index=0), sqid)
        s = repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="5" * 64,
                                    block_height=800001, confirmations=19, finality_status="final"), sqid)
        repo.upsert_tx_input(c, TxInput(transaction_id=s, prev_output_id=o, amount="100", input_index=0), sqid)
        # simulate the spend-linkage pass: mark O spent by S
        c.execute("UPDATE tx_output SET spent=1, spending_tx_id=? WHERE id=?", (s, o))

    write_with_provenance(conn, _sq(), write1)
    o_row = conn.execute("SELECT id, spent, spending_tx_id FROM tx_output WHERE output_index=0").fetchone()
    assert o_row["spent"] == 1 and o_row["spending_tx_id"] is not None

    # Re-fetch a DIFFERENT address that also touches F: F's outputs are re-upserted (spent=0 defaults),
    # S is NOT in this batch. O.spent must STAY 1 (monotonic refresh, never 1->0).
    def write2(c, sqid):
        f = repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="F" * 64,
                                    block_height=800000, confirmations=20, finality_status="final"), sqid,
                                    authoritative=True)
        repo.upsert_tx_output(c, TxOutput(transaction_id=f, amount="100", output_index=0), sqid)

    write_with_provenance(conn, _sq(), write2)
    o2 = conn.execute("SELECT spent, spending_tx_id FROM tx_output WHERE output_index=0").fetchone()
    assert o2["spent"] == 1, "re-fetching a shared funding tx wiped the spend (LOG-01)"
    assert o2["spending_tx_id"] == o_row["spending_tx_id"], "the known spender was dropped (LOG-01)"
    conn.close()


# --- LOG-12: a low-fidelity source must not clobber a chain-reported token decimals ------------------

def test_log12_decimals_not_downgraded(tmp_path):
    conn, db = new_case(tmp_path)
    contract = "0x" + "ab" * 20

    def write1(c, sqid):
        repo.upsert_asset(c, Asset(chain="ethereum", contract_address=contract, symbol="USDC",
                                   decimals=6), sqid)

    write_with_provenance(conn, _sq(connector="etherscan"), write1)

    # A later low-fidelity source lacks decimals (defaults to 0) — must NOT overwrite the chain value 6.
    def write2(c, sqid):
        repo.upsert_asset(c, Asset(chain="ethereum", contract_address=contract, symbol="USDC",
                                   decimals=0), sqid)

    write_with_provenance(conn, _sq(connector="arkham-import"), write2)
    d = conn.execute("SELECT decimals FROM asset WHERE contract_address=?", (contract,)).fetchone()[0]
    assert d == 6, "a defaulted decimals=0 clobbered the chain-reported 6 (LOG-12)"

    # …and a placeholder-0 asset CAN still be filled in by a real later value (the reverse direction).
    contract2 = "0x" + "cd" * 20

    def write3(c, sqid):
        repo.upsert_asset(c, Asset(chain="ethereum", contract_address=contract2, decimals=0), sqid)

    def write4(c, sqid):
        repo.upsert_asset(c, Asset(chain="ethereum", contract_address=contract2, decimals=18), sqid)

    write_with_provenance(conn, _sq(connector="arkham-import"), write3)
    write_with_provenance(conn, _sq(connector="etherscan"), write4)
    d2 = conn.execute("SELECT decimals FROM asset WHERE contract_address=?", (contract2,)).fetchone()[0]
    assert d2 == 18, "a placeholder-0 decimals was not filled from a real later value (LOG-12)"
    conn.close()


# --- LOG-13: reorg->mempool re-fetch must not leave a confirmed/mempool hybrid + must re-attribute ---

def test_log13_reorg_to_mempool_no_hybrid(tmp_path):
    conn, db = new_case(tmp_path)

    def write1(c, sqid):
        repo.upsert_asset(c, Asset(chain="bitcoin", symbol="BTC", decimals=8), sqid)
        repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="D" * 64, block_height=800000,
                                block_ts="2026-01-01T00:00:00Z", confirmations=1, status="confirmed",
                                finality_status="provisional"), sqid, authoritative=True)

    write_with_provenance(conn, _sq(), write1)
    first_sqid = conn.execute("SELECT source_query_id FROM transaction_ WHERE tx_hash=?", ("D" * 64,)).fetchone()[0]

    # The tx is reorged out to the mempool: block_height None, confirmations 0, status mempool.
    def write2(c, sqid):
        repo.upsert_transaction(c, Transaction(chain="bitcoin", tx_hash="D" * 64, block_height=None,
                                block_ts=None, confirmations=0, status="mempool",
                                finality_status="provisional"), sqid, authoritative=True)

    _, _ = write_with_provenance(conn, _sq(), write2)
    row = conn.execute("SELECT block_height, block_ts, status, confirmations, source_query_id "
                       "FROM transaction_ WHERE tx_hash=?", ("D" * 64,)).fetchone()
    assert row["block_height"] is None, "stale confirmed block_height kept on a mempool re-fetch (LOG-13)"
    assert row["confirmations"] == 0
    assert row["status"] == "mempool"
    assert row["source_query_id"] != first_sqid, "refreshed row still cites the first fetch (LOG-13)"
    conn.close()


def test_log13_partial_import_does_not_wipe_block_fields(tmp_path):
    """A non-authoritative import (Arkham — carries no block_height/confirmations) must FILL gaps, never
    wipe a chain source's block fields on a provisional row."""
    conn, db = new_case(tmp_path)

    def write1(c, sqid):
        repo.upsert_transaction(c, Transaction(chain="ethereum", tx_hash="0x" + "ee" * 32,
                                block_height=900, confirmations=5, status="1",
                                finality_status="provisional"), sqid, authoritative=True)

    write_with_provenance(conn, _sq(connector="etherscan"), write1)

    def write2(c, sqid):  # Arkham re-import: no block_height/confirmations/status
        repo.upsert_transaction(c, Transaction(chain="ethereum", tx_hash="0x" + "ee" * 32,
                                block_height=None, confirmations=None, status=None,
                                finality_status="provisional"), sqid)  # authoritative defaults False

    write_with_provenance(conn, _sq(connector="arkham-import"), write2)
    row = conn.execute("SELECT block_height, confirmations, status FROM transaction_ "
                       "WHERE tx_hash=?", ("0x" + "ee" * 32,)).fetchone()
    assert row["block_height"] == 900 and row["confirmations"] == 5 and row["status"] == "1", \
        "a partial import wiped a chain source's block fields (LOG-13)"
    conn.close()


# --- LOG-11: txlist status is authoritative over txlistinternal/tokentx write order ------------------

def _evm_router(txlist_rows, internal_rows, token_rows):
    payloads = {"txlist": txlist_rows, "txlistinternal": internal_rows, "tokentx": token_rows}

    def router(request):
        action = request.url.params.get("action")
        return httpx.Response(200, json={"status": "1", "message": "OK", "result": payloads.get(action, [])})

    return router


@respx.mock
def test_log11_txlist_status_wins(tmp_path):
    conn, db = new_case(tmp_path)
    G = "0x4e83362442b8d1bec281594cea3050c8eb01311c"
    H = "0x642ae78fafbb8032da552d619ad43f1d81e4dd7c"
    tx = "0x" + "1a" * 32
    # The tx is PROVISIONAL (confirmations below the finality threshold) — a final row is frozen and the
    # clobber can't occur, so the bug only manifests here.
    txlist = [{"blockNumber": "100", "timeStamp": "1693200000", "hash": tx, "from": G, "to": H,
               "value": "1000000000000000000", "gasUsed": "21000", "gasPrice": "1",
               "confirmations": "2", "isError": "0"}]  # SUCCEEDED at the top level (isError=0)
    # txlistinternal: the FIRST internal call REVERTED (isError=1) — must NOT flip the tx to failed.
    internal = [{"blockNumber": "100", "timeStamp": "1693200000", "hash": tx, "from": H, "to": G,
                 "value": "0", "traceId": "0", "confirmations": "2", "isError": "1"}]
    respx.get(BASE).mock(side_effect=_evm_router(txlist, internal, []))

    c = EtherscanConnector(api_key="test", settings=get_settings(),
                           rate_limiter=RateLimiter(0, enabled=False), sleep=lambda _s: None)
    try:
        c.get_transactions(conn, "ethereum", G)
    finally:
        c.close()
    status = conn.execute("SELECT status FROM transaction_ WHERE tx_hash=?", (tx,)).fetchone()[0]
    assert status == "success", "an internal-call revert clobbered txlist's authoritative success (LOG-11)"
    conn.close()
