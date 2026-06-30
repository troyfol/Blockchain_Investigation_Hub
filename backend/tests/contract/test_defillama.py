"""Contract tests for the DeFiLlama price connector (phase_05 step 4). Offline, from cassettes."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from backend.app.config import get_settings
from backend.app.connectors.base import RateLimiter
from backend.app.connectors.defillama import DeFiLlamaConnector
from backend.app.models import Asset

CASS = Path(__file__).resolve().parent.parent / "cassettes" / "defillama"
PRICE_URL = r".*/prices/historical/.*"
pytestmark = pytest.mark.contract


def _payload(name):
    return json.loads((CASS / name).read_text())


@pytest.fixture
def connector():
    c = DeFiLlamaConnector(settings=get_settings(), rate_limiter=RateLimiter(0, enabled=False))
    yield c
    c.close()


def test_coin_key_native_and_erc20(connector):
    assert connector.coin_key("ethereum", Asset(chain="ethereum", decimals=18)) == "coingecko:ethereum"
    assert connector.coin_key("bitcoin", Asset(chain="bitcoin", decimals=8)) == "coingecko:bitcoin"
    assert connector.coin_key("arbitrum", Asset(chain="arbitrum", decimals=18)) == "coingecko:ethereum"
    assert connector.coin_key(
        "ethereum", Asset(chain="ethereum", contract_address="0xabc", decimals=6)) == "ethereum:0xabc"


@respx.mock
def test_get_price_native_eth(connector):
    respx.get(url__regex=PRICE_URL).mock(return_value=httpx.Response(200, json=_payload("eth_price.json")))
    pr = connector.get_price("ethereum", Asset(chain="ethereum", decimals=18), 1735689600)
    assert pr is not None
    assert pr.price == "2000.5" and pr.symbol == "ETH" and pr.confidence == 0.99
    assert pr.price_timestamp == 1735689600 and pr.key == "coingecko:ethereum"


@respx.mock
def test_get_price_missing_returns_none(connector):
    respx.get(url__regex=PRICE_URL).mock(return_value=httpx.Response(200, json={"coins": {}}))
    assert connector.get_price("ethereum", Asset(chain="ethereum", decimals=18), 1) is None


# --- bsc support (Gap A + Gap B; confirmed live 2026-06-28) -----------------------------------

BSC_USDT = "0x55d398326f99059ff775485246999027b3197955"  # lowercase (as canonical_address produces)


def test_coin_key_bsc_native_and_token(connector):
    # GAP A: bsc native used to raise UpstreamError; now resolves the BNB coingecko key + is supported.
    assert connector.coin_key("bsc", Asset(chain="bsc", decimals=18)) == "coingecko:binancecoin"
    assert "bsc" in connector.supported_chains()
    # GAP B: the token key uses the contract VERBATIM (no lowercasing here) — production canonical_address
    # supplies a lowercase contract, which is what DeFiLlama wants under the `bsc:` prefix.
    assert connector.coin_key("bsc", Asset(chain="bsc", contract_address=BSC_USDT, decimals=18)) == f"bsc:{BSC_USDT}"
    assert connector.coin_key("bsc", Asset(chain="bsc", contract_address="0xABCdef", decimals=18)) == "bsc:0xABCdef"


@respx.mock
def test_get_price_bsc_native_bnb(connector):
    respx.get(url__regex=PRICE_URL).mock(return_value=httpx.Response(200, json=_payload("bnb_price.json")))
    pr = connector.get_price("bsc", Asset(chain="bsc", decimals=18), 1700000000)
    assert pr is not None and pr.key == "coingecko:binancecoin" and pr.symbol == "BNB"
    assert pr.price == "241.98" and pr.confidence == 0.99
    assert pr.decimals is None  # coingecko: keys don't return decimals — tolerated, not assumed


@respx.mock
def test_get_price_bsc_token_lowercase_key_resolves(connector):
    respx.get(url__regex=PRICE_URL).mock(return_value=httpx.Response(200, json=_payload("bsc_usdt_price.json")))
    pr = connector.get_price("bsc", Asset(chain="bsc", contract_address=BSC_USDT, decimals=18), 1700000000)
    assert pr is not None and pr.key == f"bsc:{BSC_USDT}" and pr.price == "1.0"
