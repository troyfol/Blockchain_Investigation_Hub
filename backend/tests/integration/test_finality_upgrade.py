"""Finality upgrade mechanism (Invariant #6) — provisional→final at the confirmations threshold.

Re-fetch the tip, recompute confirmations, flip to `final` only at `confirmations >= threshold(chain)`;
final rows stay frozen; idempotent. Tests the boundary on both sides.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.app.audits.runner import run_audits
from backend.app.config import get_settings
from backend.app.connectors.base import RateLimiter
from backend.app.connectors.esplora import EsploraConnector
from backend.app.db import repository as repo
from backend.app.models import SourceQuery, Transaction
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.finality import refresh_finality, upgrade_finality
from backend.tests.integration._helpers import new_case


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="Finality")
    yield conn, db
    conn.close()


def _seed_tx(conn, *, chain, tx_hash, block_height, finality_status="provisional", confirmations=None):
    sq = SourceQuery(connector="esplora", capability="get_transactions", endpoint="address-txs",
                     params={"address": "probe", "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")

    def w(c, sqid):
        repo.upsert_transaction(c, Transaction(
            chain=chain, tx_hash=tx_hash, block_height=block_height,
            finality_status=finality_status, confirmations=confirmations), sqid)

    write_with_provenance(conn, sq, w)


def _status(conn, tx_hash):
    r = conn.execute(
        "SELECT finality_status, confirmations FROM transaction_ WHERE tx_hash=?", (tx_hash,)).fetchone()
    return r["finality_status"], r["confirmations"]


def test_flip_at_the_threshold_boundary(case):
    conn, db = case
    # bitcoin threshold = 6; a provisional tx at block 100. confirmations = tip - 100 + 1.
    _seed_tx(conn, chain="bitcoin", tx_hash="a" * 64, block_height=100)

    # tip 104 -> confirmations 5 (< 6) -> STILL provisional, but confirmations refreshed.
    res = upgrade_finality(conn, chain="bitcoin", tip_height=104, threshold=6)
    assert res == {"upgraded": 0, "refreshed": 1, "tip_height": 104}
    assert _status(conn, "a" * 64) == ("provisional", 5)
    assert all(r.passed for r in run_audits(db_path=str(db)))

    # tip 105 -> confirmations 6 (== 6) -> FLIP to final.
    res = upgrade_finality(conn, chain="bitcoin", tip_height=105, threshold=6)
    assert res == {"upgraded": 1, "refreshed": 0, "tip_height": 105}
    assert _status(conn, "a" * 64) == ("final", 6)
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_final_rows_are_frozen_and_rerun_is_idempotent(case):
    conn, db = case
    _seed_tx(conn, chain="bitcoin", tx_hash="b" * 64, block_height=100)
    _seed_tx(conn, chain="bitcoin", tx_hash="c" * 64, block_height=50,
             finality_status="final", confirmations=51)  # already final

    upgrade_finality(conn, chain="bitcoin", tip_height=105, threshold=6)  # flips b… to final
    assert _status(conn, "b" * 64) == ("final", 6)
    assert _status(conn, "c" * 64) == ("final", 51)  # frozen — a much higher tip never re-touches it

    # Idempotent: re-running with the same (or higher) tip upgrades nothing already final.
    res = upgrade_finality(conn, chain="bitcoin", tip_height=200, threshold=6)
    assert res["upgraded"] == 0 and res["refreshed"] == 0  # both rows are final now -> nothing provisional
    assert _status(conn, "b" * 64) == ("final", 6)  # confirmations frozen at the flip value, not 101
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_refresh_finality_uses_chain_threshold_from_settings(case):
    conn, db = case
    settings = get_settings()
    assert settings.finality_threshold("ethereum") == 64
    _seed_tx(conn, chain="ethereum", tx_hash="0x" + "d" * 64, block_height=1000)

    # tip = 1000 + 64 - 2 = 1062 -> confirmations 63 (< 64) -> provisional.
    res = refresh_finality(conn, "ethereum", tip_height=1062, settings=settings)
    assert res["upgraded"] == 0 and _status(conn, "0x" + "d" * 64) == ("provisional", 63)
    # tip = 1000 + 64 - 1 = 1063 -> confirmations 64 -> final.
    res = refresh_finality(conn, "ethereum", tip_height=1063, settings=settings)
    assert res["upgraded"] == 1 and _status(conn, "0x" + "d" * 64) == ("final", 64)
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_mempool_tx_has_no_block_height_stays_provisional(case):
    conn, db = case
    _seed_tx(conn, chain="bitcoin", tx_hash="e" * 64, block_height=None)  # mempool
    res = upgrade_finality(conn, chain="bitcoin", tip_height=10_000, threshold=6)
    # No block_height -> confirmations 0 -> never final (Invariant #6: tip data not frozen).
    assert res["upgraded"] == 0 and _status(conn, "e" * 64) == ("provisional", 0)
    assert all(r.passed for r in run_audits(db_path=str(db)))


@respx.mock
def test_btc_operator_flow_refresh_via_live_esplora_tip(case):
    """The BTC/UTXO operator path: a provisional tx (ingested earlier / via import) is upgraded by
    fetching the LIVE Esplora tip and re-evaluating — no full re-ingest needed."""
    conn, db = case
    _seed_tx(conn, chain="bitcoin", tx_hash="f" * 64, block_height=800000)  # provisional
    respx.get("https://blockstream.info/api/blocks/tip/height").mock(
        return_value=httpx.Response(200, text="800005"))
    esplora = EsploraConnector(settings=get_settings(), rate_limiter=RateLimiter(0, enabled=False),
                               sleep=lambda _s: None)
    res = refresh_finality(conn, "bitcoin", connector=esplora, settings=get_settings())
    esplora.close()
    # tip 800005, block 800000 -> confirmations 6 == threshold(bitcoin)=6 -> final.
    assert res["upgraded"] == 1 and res["tip_height"] == 800005
    assert _status(conn, "f" * 64) == ("final", 6)
    assert all(r.passed for r in run_audits(db_path=str(db)))
