"""Golden valuation ingest (phase_05). Values seeded EVM + BTC movements via mocked DeFiLlama."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from backend.app.audits.runner import run_audits
from backend.app.config import get_settings
from backend.app.connectors.base import RateLimiter, RateLimitError
from backend.app.connectors.defillama import DeFiLlamaConnector, PriceRecord
from backend.app.db import apply_migrations, get_connection
from backend.app.db import repository as repo
from backend.app.models import Address, Asset, SourceQuery, Transaction, Transfer, Valuation
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.valuation import value_movements
from backend.tests.integration._helpers import new_case
from backend.tests.integration.test_seeded_case import seed_btc_tx, seed_evm_transfer

CASS = Path(__file__).resolve().parent.parent / "cassettes" / "defillama"
ETH = json.loads((CASS / "eth_price.json").read_text())
BTC = json.loads((CASS / "btc_price.json").read_text())


def _router(request):
    p = request.url.path
    if "coingecko:ethereum" in p:
        return httpx.Response(200, json=ETH)
    if "coingecko:bitcoin" in p:
        return httpx.Response(200, json=BTC)
    return httpx.Response(200, json={"coins": {}})


@pytest.fixture
def case(tmp_path):
    db = tmp_path / "case.db"
    apply_migrations(db)
    conn = get_connection(db)
    repo.init_case(conn, title="Valuation Case")
    seed_evm_transfer(conn)
    seed_btc_tx(conn)
    yield conn, db
    conn.close()


@pytest.fixture
def connector():
    c = DeFiLlamaConnector(settings=get_settings(), rate_limiter=RateLimiter(0, enabled=False),
                           sleep=lambda _s: None)
    yield c
    c.close()


@respx.mock
@pytest.mark.smoke
def test_value_movements_golden(case, connector):
    conn, db = case
    respx.route(host="coins.llama.fi").mock(side_effect=_router)

    result = value_movements(conn, connector)
    assert result["valued"] == 3  # 1 EVM transfer + 2 BTC outputs

    # EVM transfer: 1 ETH @ $2000.5, with confidence + price_timestamp + provenance.
    tr = conn.execute("SELECT id FROM transfer").fetchone()["id"]
    v = conn.execute("SELECT * FROM valuation WHERE subject_type='transfer' AND subject_id=?", (tr,)).fetchone()
    assert v["value"] == "2000.500000000000000000" and v["unit_price"] == "2000.5"
    assert v["confidence"] == 0.99 and v["currency"] == "USD" and v["source"] == "defillama"
    assert v["price_timestamp"] and v["source_query_id"]  # valued-at-time + provenance

    # BTC output 120000 sats @ $60000 = $72.
    out = conn.execute("SELECT id FROM tx_output WHERE amount='120000'").fetchone()["id"]
    bv = conn.execute("SELECT value FROM valuation WHERE subject_type='tx_output' AND subject_id=?", (out,)).fetchone()
    assert bv["value"] == "72.000000000000000000"

    results = run_audits(db_path=str(db))
    assert all(r.passed for r in results)
    assert any(r.name == "valuation-subject-validity" and r.passed for r in results)


@respx.mock
def test_missing_price_writes_no_row(case, connector):
    conn, db = case
    respx.route(host="coins.llama.fi").mock(return_value=httpx.Response(200, json={"coins": {}}))
    result = value_movements(conn, connector)
    assert result["valued"] == 0 and result["skipped"] == 3
    assert conn.execute("SELECT COUNT(*) FROM valuation").fetchone()[0] == 0  # honest gap, no zero


class _StubPriceConnector:
    """Counts get_prices (per-timestamp BATCH) calls; raises RateLimitError when ``raise_always``."""

    def __init__(self, *, raise_always: bool = False):
        self.calls = 0
        self.raise_always = raise_always

    def coin_key(self, chain, asset):
        return f"{chain}:{asset.contract_address or 'native'}"

    def get_prices(self, items, timestamp):
        self.calls += 1
        if self.raise_always:
            raise RateLimitError("HTTP 429")
        out = {}
        for chain, asset in items:
            k = self.coin_key(chain, asset)
            out[k] = PriceRecord(key=k, price="2000.5", symbol=None, decimals=None,
                                 price_timestamp=timestamp, confidence=0.99, raw={"coins": {}})
        return out, {"coins": {}}


def test_rate_limit_skips_group_not_aborts_run(case):
    # A price-source error must be an honest gap (NO rows) — never abort the whole pass.
    conn, db = case
    stub = _StubPriceConnector(raise_always=True)
    result = value_movements(conn, stub)  # would previously raise UpstreamError and abort
    assert result["valued"] == 0 and result["errors"] >= 1
    assert conn.execute("SELECT COUNT(*) FROM valuation").fetchone()[0] == 0  # no fabricated rows
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_circuit_breaker_stops_hammering_after_consecutive_errors(case):
    # max=1: the first timestamp's batch fails → breaker opens → the second timestamp is NOT called.
    conn, db = case
    stub = _StubPriceConnector(raise_always=True)
    result = value_movements(conn, stub, max_consecutive_errors=1)
    assert result["price_source_unavailable"] is True
    assert stub.calls == 1  # 2 timestamps seeded; the 2nd batch was skipped by the breaker


def test_movements_are_batched_by_timestamp(case):
    # 3 movements at 2 distinct block timestamps (1 EVM tx + 1 BTC tx with 2 outputs) -> 2 batch calls;
    # the 2 BTC outputs share a timestamp so ONE call values both.
    conn, db = case
    stub = _StubPriceConnector()
    result = value_movements(conn, stub)
    assert result["valued"] == 3
    assert stub.calls == 2  # one /prices/historical call per timestamp, not per movement


USDC = "0x" + "a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"[2:]  # ethereum USDC-style contract (lowercase)
BSC_TS = "2022-06-01T00:00:00Z"
BSC_USDC_BODY = {"coins": {
    "coingecko:binancecoin": {"symbol": "BNB", "price": 600.0, "timestamp": 1654041600, "confidence": 0.99},
    f"ethereum:{USDC}": {"symbol": "USDC", "price": 1.0, "timestamp": 1654041600, "confidence": 0.99,
                         "decimals": 6}}}


def _seed_transfer(conn, *, chain, txh, frm, to, amount, contract, symbol, decimals):
    sq = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="txlist",
                     params={"address": "p", "bounds": "default"},
                     requested_at="2026-01-01T00:00:00Z", status="ok")

    def w(c, sqid):
        tx_id = repo.upsert_transaction(c, Transaction(
            chain=chain, tx_hash=txh, block_height=1, block_ts=BSC_TS, finality_status="provisional"), sqid)
        fid = repo.upsert_address(c, Address(chain=chain, address_display=frm), sqid)
        tid = repo.upsert_address(c, Address(chain=chain, address_display=to), sqid)
        aid = repo.upsert_asset(c, Asset(chain=chain, contract_address=contract, symbol=symbol,
                                         decimals=decimals), sqid)
        repo.upsert_transfer(c, Transfer(
            transaction_id=tx_id, chain=chain, from_address_id=fid, to_address_id=tid, asset_id=aid,
            amount=amount, transfer_type=("native" if contract is None else "erc20"), position=0), sqid)

    write_with_provenance(conn, sq, w)


@respx.mock
def test_bsc_native_and_token_key_valued_in_one_batch(tmp_path):
    """Exercises the bsc native-key fix (coingecko:binancecoin) AND the token `{chain}:{contract}` join,
    both valued at the SAME timestamp in ONE batched DeFiLlama call."""
    conn, db = new_case(tmp_path, title="bsc valuation")
    _seed_transfer(conn, chain="bsc", txh="0x" + "1" * 64, frm="0x" + "1" * 40, to="0x" + "2" * 40,
                   amount="2000000000000000000", contract=None, symbol="BNB", decimals=18)   # 2 BNB
    _seed_transfer(conn, chain="ethereum", txh="0x" + "3" * 64, frm="0x" + "3" * 40, to="0x" + "4" * 40,
                   amount="50000000", contract=USDC, symbol="USDC", decimals=6)              # 50 USDC

    captured = {}

    def router(request):
        captured["path"] = request.url.path  # the comma-joined keys are in the path
        return httpx.Response(200, json=BSC_USDC_BODY)

    respx.route(host="coins.llama.fi").mock(side_effect=router)
    connector = DeFiLlamaConnector(settings=get_settings(), rate_limiter=RateLimiter(0, enabled=False),
                                   sleep=lambda _s: None)
    result = value_movements(conn, connector)
    connector.close()

    assert result["valued"] == 2
    # Both coin keys were requested in ONE call (one source_query, batched): the bsc native coingecko
    # slug + the ethereum token contract key.
    assert "coingecko:binancecoin" in captured["path"] and f"ethereum:{USDC}" in captured["path"]
    assert conn.execute(
        "SELECT COUNT(*) FROM source_query WHERE connector='defillama'").fetchone()[0] == 1

    bnb = conn.execute(
        """SELECT v.value, v.unit_price FROM valuation v JOIN transfer t ON t.id=v.subject_id
           JOIN transaction_ x ON x.id=t.transaction_id WHERE x.chain='bsc'""").fetchone()
    assert bnb["unit_price"] == "600.0" and bnb["value"] == "1200.000000000000000000"  # 2 BNB * $600
    usdc = conn.execute(
        """SELECT v.value FROM valuation v JOIN transfer t ON t.id=v.subject_id
           JOIN transaction_ x ON x.id=t.transaction_id WHERE x.chain='ethereum'""").fetchone()
    assert usdc["value"] == "50.000000000000000000"  # 50 USDC * $1
    assert all(r.passed for r in run_audits(db_path=str(db)))
    conn.close()


@respx.mock
def test_partial_miss_in_batch_values_only_the_priced_coin(tmp_path):
    """A batch (one timestamp) where ONE coin is priced and ANOTHER is missing: the priced movement is
    valued, the missing one is an honest gap (NO row — never a fabricated zero)."""
    conn, db = new_case(tmp_path, title="partial miss")
    _seed_transfer(conn, chain="bsc", txh="0x" + "1" * 64, frm="0x" + "1" * 40, to="0x" + "2" * 40,
                   amount="2000000000000000000", contract=None, symbol="BNB", decimals=18)
    _seed_transfer(conn, chain="ethereum", txh="0x" + "3" * 64, frm="0x" + "3" * 40, to="0x" + "4" * 40,
                   amount="50000000", contract=USDC, symbol="USDC", decimals=6)
    # Same timestamp -> one batch; the body prices ONLY the bsc native coin (USDC omitted).
    body = {"coins": {"coingecko:binancecoin": {"symbol": "BNB", "price": 600.0,
                                                "timestamp": 1654041600, "confidence": 0.99}}}
    respx.route(host="coins.llama.fi").mock(return_value=httpx.Response(200, json=body))
    connector = DeFiLlamaConnector(settings=get_settings(), rate_limiter=RateLimiter(0, enabled=False),
                                   sleep=lambda _s: None)
    result = value_movements(conn, connector)
    connector.close()

    assert result["valued"] == 1 and result["skipped"] == 1
    assert conn.execute("SELECT COUNT(*) FROM valuation").fetchone()[0] == 1  # no fabricated zero for USDC
    row = conn.execute(
        """SELECT x.chain FROM valuation v JOIN transfer t ON t.id=v.subject_id
           JOIN transaction_ x ON x.id=t.transaction_id""").fetchone()
    assert row["chain"] == "bsc"  # only the priced coin was valued
    assert all(r.passed for r in run_audits(db_path=str(db)))
    conn.close()


@respx.mock
def test_revalue_is_idempotent_at_service_but_claims_append_only(case, connector):
    conn, db = case
    respx.route(host="coins.llama.fi").mock(side_effect=_router)
    value_movements(conn, connector)
    n1 = conn.execute("SELECT COUNT(*) FROM valuation").fetchone()[0]
    value_movements(conn, connector)  # already valued -> nothing new
    assert conn.execute("SELECT COUNT(*) FROM valuation").fetchone()[0] == n1

    # But a forced re-valuation APPENDS (claims never overwrite — Invariant #4).
    tr = conn.execute("SELECT id FROM transfer").fetchone()["id"]
    sq = conn.execute("SELECT id FROM source_query LIMIT 1").fetchone()["id"]
    repo.insert_valuation(conn, Valuation(subject_type="transfer", subject_id=tr, unit_price="2100",
                                          value="2100.000000000000000000", price_timestamp="2026-01-01T00:00:00Z",
                                          confidence=0.97, source="defillama",
                                          retrieved_at="2026-01-02T00:00:00Z"), sq)
    assert conn.execute("SELECT COUNT(*) FROM valuation WHERE subject_type='transfer' AND subject_id=?",
                        (tr,)).fetchone()[0] == 2
    assert all(r.passed for r in run_audits(db_path=str(db)))
