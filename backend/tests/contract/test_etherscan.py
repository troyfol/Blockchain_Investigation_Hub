"""Contract tests for the Etherscan adapter + connector envelope/retry (phase_02 step 5).

Replays recorded cassettes through the pure adapter and asserts the exact canonical rows;
exercises the envelope status semantics and 429 retry/backoff. All offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from backend.app.config import get_settings
from backend.app.connectors.base import RateLimiter, RateLimitError, UpstreamError
from backend.app.connectors.etherscan import EtherscanConnector
from backend.app.normalization.etherscan_adapter import (
    adapt_balance,
    adapt_tokentx,
    adapt_txlist,
    adapt_txlistinternal,
)

CASSETTES = Path(__file__).resolve().parent.parent / "cassettes" / "etherscan"
CHAIN, TIP, THR = "ethereum", 18000200, 64
G = "0x4e83362442b8d1bec281594cea3050c8eb01311c"
H = "0x642ae78fafbb8032da552d619ad43f1d81e4dd7c"
C = "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2"
TXA = "0x" + "a" * 64
TXB = "0x" + "b" * 64
pytestmark = pytest.mark.contract


def _rows(name):
    return json.loads((CASSETTES / name).read_text())["result"]


def test_adapt_txlist_native_and_finality():
    parsed = {p.transaction.tx_hash: p
              for p in adapt_txlist(_rows("txlist.json"), chain=CHAIN, tip_height=TIP, threshold=THR)}
    assert set(parsed) == {TXA, TXB}

    a = parsed[TXA]
    assert a.transaction.finality_status == "final"
    assert a.transaction.confirmations == 201  # tip - block + 1
    assert a.transaction.fee == str(21000 * 20000000000)
    assert a.transaction.status == "success"
    assert len(a.transfers) == 1
    tr = a.transfers[0]
    assert (tr.transfer_type, tr.position) == ("native", 0)
    assert tr.from_address == G and tr.to_address == H
    assert tr.amount == "1000000000000000000"
    assert tr.asset.contract_address is None and tr.asset.symbol == "ETH" and tr.asset.decimals == 18

    # value==0 contract call -> tx recorded, but NO native transfer.
    assert parsed[TXB].transfers == []


def test_adapt_internal():
    parsed = adapt_txlistinternal(_rows("txlistinternal.json"), chain=CHAIN, tip_height=TIP, threshold=THR)
    assert len(parsed) == 1
    p = parsed[0]
    assert p.transaction.tx_hash == TXB and p.transaction.fee is None  # fee left to txlist
    tr = p.transfers[0]
    assert (tr.transfer_type, tr.position) == ("internal", 0)
    assert tr.from_address == C and tr.to_address == G
    assert tr.amount == "500000000000000000" and tr.asset.contract_address is None


def test_adapt_tokentx_asset_and_amount():
    parsed = adapt_tokentx(_rows("tokentx.json"), chain=CHAIN, tip_height=TIP, threshold=THR)
    assert len(parsed) == 1
    tr = parsed[0].transfers[0]
    assert (tr.transfer_type, tr.position) == ("erc20", 0)
    assert tr.from_address == C and tr.to_address == G
    assert tr.amount == "100000000000000000000"
    assert tr.asset.contract_address == C and tr.asset.symbol == "MKR" and tr.asset.decimals == 18


def test_adapt_balance_canonicalizes_address():
    result = json.loads((CASSETTES / "balance.json").read_text())["result"]
    canonical, snap = adapt_balance(result, chain=CHAIN, address=G.upper().replace("0X", "0x"),
                                    as_of_ts="2026-06-26T00:00:00Z")
    assert canonical == G
    assert snap.amount == "5000000000000000000" and snap.asset_id is None


def test_position_assignment_multiple_transfers_per_tx():
    # Two token transfers in one tx -> positions 0,1; a second tx -> position 0.
    rows = [
        {"blockNumber": "1", "timeStamp": "1693200000", "hash": TXB, "from": C, "to": G,
         "contractAddress": C, "value": "1", "tokenSymbol": "MKR", "tokenDecimal": "18",
         "confirmations": "100"},
        {"blockNumber": "1", "timeStamp": "1693200000", "hash": TXB, "from": C, "to": H,
         "contractAddress": C, "value": "2", "tokenSymbol": "MKR", "tokenDecimal": "18",
         "confirmations": "100"},
        {"blockNumber": "2", "timeStamp": "1693200000", "hash": TXA, "from": C, "to": G,
         "contractAddress": C, "value": "3", "tokenSymbol": "MKR", "tokenDecimal": "18",
         "confirmations": "100"},
    ]
    parsed = {p.transaction.tx_hash: p
              for p in adapt_tokentx(rows, chain=CHAIN, tip_height=TIP, threshold=THR)}
    assert sorted(t.position for t in parsed[TXB].transfers) == [0, 1]
    assert [t.position for t in parsed[TXA].transfers] == [0]


def test_failed_tx_records_no_transfer():
    # A reverted tx (isError=1) moves no value: record the tx (status=failed), NO transfer.
    txlist = [{"blockNumber": "100", "timeStamp": "1693200000", "hash": TXA, "from": G, "to": H,
               "value": "1000000000000000000", "gasUsed": "21000", "gasPrice": "1",
               "confirmations": "100", "isError": "1"}]
    p = adapt_txlist(txlist, chain=CHAIN, tip_height=TIP, threshold=THR)[0]
    assert p.transaction.status == "failed" and p.transfers == []

    internal = [{"blockNumber": "100", "timeStamp": "1693200000", "hash": TXB, "from": C, "to": G,
                 "value": "500", "traceId": "0", "isError": "1"}]
    pi = adapt_txlistinternal(internal, chain=CHAIN, tip_height=TIP, threshold=THR)[0]
    assert pi.transaction.status == "failed" and pi.transfers == []


def test_internal_positions_are_traceid_deterministic():
    def mk(trace, val):
        return {"blockNumber": "100", "timeStamp": "1693200000", "hash": TXB, "from": C, "to": G,
                "value": str(val), "traceId": trace, "isError": "0"}

    def posmap(rows):
        p = adapt_txlistinternal(rows, chain=CHAIN, tip_height=TIP, threshold=THR)[0]
        return {t.amount: t.position for t in p.transfers}

    forward = posmap([mk("0", 10), mk("1", 20), mk("0_1", 30)])
    shuffled = posmap([mk("0_1", 30), mk("1", 20), mk("0", 10)])
    assert forward == shuffled  # order-independent
    assert forward == {"10": 0, "30": 1, "20": 2}  # traceId order: 0 < 0_1 < 1


# --- envelope + retry (connector) --------------------------------------------------------

@pytest.fixture
def connector():
    c = EtherscanConnector(api_key="test", settings=get_settings(),
                           rate_limiter=RateLimiter(0, enabled=False), sleep=lambda _s: None,
                           backoff_base=0.0)
    yield c
    c.close()


def test_envelope_no_records_is_empty(connector):
    assert connector._envelope_rows({"status": "0", "message": "No transactions found", "result": []}) == []


def test_envelope_error_raises(connector):
    with pytest.raises(UpstreamError):
        connector._envelope_rows({"status": "0", "message": "NOTOK", "result": "Error! Invalid address format"})


def test_envelope_rate_limit_raises(connector):
    with pytest.raises(RateLimitError):
        connector._envelope_rows({"status": "0", "message": "NOTOK", "result": "Max rate limit reached"})


@respx.mock
def test_retry_on_429_then_succeeds(connector):
    base = get_settings().etherscan_base_url
    route = respx.get(base).mock(side_effect=[
        httpx.Response(429, text="rate limited"),
        httpx.Response(200, json={"status": "1", "message": "OK", "result": []}),
    ])
    payloads, rows, partial = connector._collect("ethereum", G, "txlist", {})
    assert route.call_count == 2  # retried once
    assert rows == [] and partial is False
