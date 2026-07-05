"""Live-drift tests (docs/testing.md §1/§5) — opt-in, never block CI.

Re-hit the real Etherscan V2 API to confirm the response SHAPE the adapter relies on hasn't
changed. Run only with ``RUN_LIVE=1`` and an ``etherscan`` key in the keyring (or the plaintext
opt-in). A failure here means "refresh the cassettes / re-confirm docs", not a build break.
"""

from __future__ import annotations

import os

import httpx
import pytest

from backend.app.config import get_settings
from backend.app.secrets import get_secret

RUN_LIVE = os.environ.get("RUN_LIVE") == "1"
pytestmark = pytest.mark.skipif(not RUN_LIVE, reason="set RUN_LIVE=1 (needs an Etherscan key) to run")

# A long-lived, high-activity address (stable shape).
PROBE_ADDRESS = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
TXLIST_FIELDS = {"blockNumber", "timeStamp", "hash", "from", "to", "value", "gasUsed", "gasPrice",
                 "isError", "confirmations"}
TOKENTX_FIELDS = {"blockNumber", "timeStamp", "hash", "from", "to", "contractAddress", "value",
                  "tokenSymbol", "tokenDecimal", "confirmations"}


def _key():
    key = get_secret("etherscan")
    if not key:
        pytest.skip("no 'etherscan' key in keyring")
    return key


def _get(action, **extra):
    params = {"chainid": 1, "module": "account", "action": action, "address": PROBE_ADDRESS,
              "page": 1, "offset": 5, "sort": "desc", "apikey": _key(), **extra}
    return httpx.get(get_settings().etherscan_base_url, params=params, timeout=30).json()


def test_envelope_shape():
    payload = _get("txlist")
    assert set(payload) >= {"status", "message", "result"}
    assert str(payload["status"]) in ("0", "1")


@pytest.mark.parametrize("action,fields", [("txlist", TXLIST_FIELDS), ("tokentx", TOKENTX_FIELDS)])
def test_row_fields_present(action, fields):
    payload = _get(action)
    if str(payload["status"]) != "1" or not payload["result"]:
        pytest.skip(f"{action}: no rows to check shape")
    missing = fields - set(payload["result"][0])
    assert not missing, f"{action} drifted; missing fields: {missing}"


def test_balance_returns_wei_string():
    payload = _get("balance", tag="latest")
    assert str(payload["status"]) == "1"
    assert isinstance(payload["result"], str) and payload["result"].isdigit()


# --- Esplora (Bitcoin) — keyless, only needs network -------------------------------------

ESPLORA_BASE = get_settings().esplora_base_url
# A long-lived address with confirmed history (Satoshi's genesis-coinbase recipient).
BTC_PROBE = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"


def test_esplora_tip_height_is_integer():
    r = httpx.get(f"{ESPLORA_BASE}/blocks/tip/height", timeout=30)
    assert r.text.strip().isdigit()


def test_esplora_tx_shape():
    txs = httpx.get(f"{ESPLORA_BASE}/address/{BTC_PROBE}/txs", timeout=30).json()
    if not txs:
        pytest.skip("no txs to check shape")
    tx = txs[0]
    assert {"txid", "status", "vin", "vout"} <= set(tx)
    assert {"confirmed"} <= set(tx["status"])
    assert {"scriptpubkey_address", "value"} <= set(tx["vout"][0]) or "value" in tx["vout"][0]


def test_esplora_address_stats_shape():
    payload = httpx.get(f"{ESPLORA_BASE}/address/{BTC_PROBE}", timeout=30).json()
    cs = payload.get("chain_stats", {})
    assert {"funded_txo_sum", "spent_txo_sum"} <= set(cs)


# --- Chainalysis sanctions API (needs a free 'chainalysis_api_key' in the keyring) --------

def test_chainalysis_sanctions_shape():
    """Confirm the Chainalysis sanctions response carries an ``identifications`` array whose entries
    have the fields the connector maps (category/name/description). Refresh path for the TODO: confirm."""
    key = get_secret("chainalysis_api_key")
    if not key:
        pytest.skip("no 'chainalysis_api_key' in keyring")
    # A known OFAC-sanctioned ETH address (Tornado Cash router) — stable for shape checks.
    probe = "0x8589427373d6d84e98730d7795d8f6f8731fda16"
    r = httpx.get(f"https://public.chainalysis.com/api/v1/address/{probe}",
                  headers={"X-API-Key": key}, timeout=30)
    payload = r.json()
    assert "identifications" in payload
    if payload["identifications"]:
        ident = payload["identifications"][0]
        missing = {"category", "name", "description"} - set(ident)
        assert not missing, f"chainalysis identification drifted; missing: {missing}"


# --- Optional PAID connectors (each needs its keyring key; skipped otherwise) -------------

ARKHAM_PROBE = "0x52908400098527886e0f7030069857d2e4169ee7"


def test_arkham_risk_shape():
    """Confirm RiskScoreResponse carries max_score + greatest_risk_category (the fields adapt_risk uses)."""
    key = get_secret("arkham_api_key")
    if not key:
        pytest.skip("no 'arkham_api_key' in keyring")
    r = httpx.get(f"https://api.arkm.com/risk/address/{ARKHAM_PROBE}",
                  headers={"API-Key": key}, timeout=30)
    payload = r.json()
    assert isinstance(payload, dict)
    assert {"max_score", "greatest_risk_category"} <= set(payload), f"arkham risk drifted: {set(payload)}"


def test_arkham_intelligence_shape():
    key = get_secret("arkham_api_key")
    if not key:
        pytest.skip("no 'arkham_api_key' in keyring")
    r = httpx.get(f"https://api.arkm.com/intelligence/address/{ARKHAM_PROBE}",
                  headers={"API-Key": key}, params={"chain": "ethereum"}, timeout=30)
    payload = r.json()
    assert isinstance(payload, dict)  # Address schema; adapter reads arkhamEntity/predictedEntity/arkhamLabel


def test_misttrack_risk_shape():
    key = get_secret("misttrack_api_key")
    if not key:
        pytest.skip("no 'misttrack_api_key' in keyring")
    r = httpx.get("https://openapi.misttrack.io/v2/risk_score",
                  params={"coin": "ETH", "address": ARKHAM_PROBE, "api_key": key}, timeout=30)
    payload = r.json()
    assert isinstance(payload, dict) and "data" in payload
    data = payload.get("data") or {}
    assert "score" in data, f"misttrack risk drifted; data keys: {set(data)}"
    # FN-26 (P21): confirm the risk_detail[] sub-fields P20's `risk_detail` mapping reads (signal=risk_type,
    # score=percent). Only asserts when the live payload carries a breakdown; surfaces drift in these names so
    # the `score=percent` vs `volume` TODO can be resolved against the real V2/V3 envelope.
    for d in (data.get("risk_detail") or []):
        assert isinstance(d, dict) and "risk_type" in d, \
            f"misttrack risk_detail[] drifted; entry: {d if isinstance(d, dict) else type(d).__name__}"
        assert "percent" in d or "volume" in d, f"misttrack risk_detail[] has no percent/volume; keys: {set(d)}"


def test_bitquery_graphql_responds():
    """Confirm the V2 GraphQL endpoint accepts the Bearer token + the Transfers query (shape TODO)."""
    token = get_secret("bitquery_token")
    if not token:
        pytest.skip("no 'bitquery_token' in keyring")
    from backend.app.normalization.bitquery_adapter import TRANSFERS_QUERY
    r = httpx.post("https://streaming.bitquery.io/graphql",
                   headers={"Authorization": f"Bearer {token}"},
                   json={"query": TRANSFERS_QUERY,
                         "variables": {"network": "eth", "address": ARKHAM_PROBE, "limit": 1}}, timeout=30)
    payload = r.json()
    assert isinstance(payload, dict)
    assert not payload.get("errors"), f"bitquery query rejected (field paths TODO): {payload.get('errors')}"
    assert "data" in payload
