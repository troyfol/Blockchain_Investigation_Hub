"""Bitquery connector — gating + map + graceful-empty (paid; docs/findings/paid_api_integrations.md §1).
NO live token / NO fabricated GraphQL cassette; the query body + mapping are TODO: confirm and validated
by the RUN_LIVE drift test. Tested here: the no-key guard, the network map, and that the (unconfirmed)
adapter degrades to no-rows on a missing/empty shape instead of crashing.
"""

from __future__ import annotations

import pytest

from backend.app.connectors.base import ConnectorError
from backend.app.connectors.bitquery import BitqueryConnector
from backend.app.normalization.bitquery_adapter import adapt_transfers, network_for
from backend.tests.integration._helpers import new_case


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="Bitquery")
    yield conn, db
    conn.close()


def test_no_key_raises_naming_the_keyring_entry(case):
    conn, _ = case
    c = BitqueryConnector(token="")
    with pytest.raises(ConnectorError) as exc:
        c.get_transactions(conn, "ethereum", "0x52908400098527886e0f7030069857d2e4169ee7")
    c.close()
    assert "bitquery_token" in str(exc.value)


def test_v2_bearer_vs_v1_apikey_header():
    v2 = BitqueryConnector(token="tok", use_v1=False)
    assert v2._client.headers.get("Authorization") == "Bearer tok"
    v2.close()
    v1 = BitqueryConnector(token="tok", use_v1=True)
    assert v1._client.headers.get("X-API-KEY") == "tok"
    v1.close()


def test_network_map_and_supported_chains():
    assert network_for("ethereum") == "eth" and network_for("polygon") == "matic"
    assert network_for("nope") is None
    c = BitqueryConnector(token="")
    assert {"ethereum", "bsc", "polygon"} <= c.supported_chains()
    c.close()


def test_adapter_degrades_on_missing_shape():
    # Unconfirmed schema -> a missing/empty structure must yield no rows, never crash.
    for payload in ({}, {"data": {}}, {"data": {"EVM": {"Transfers": []}}}, "nope", []):
        bundles, notes = adapt_transfers(payload if isinstance(payload, dict) else {}, chain="ethereum")
        assert bundles == [] and notes["transfers"] == 0


def test_to_base_units_decimal_scaling():
    """The deterministic display->base-unit scaling (Decimal, half-even) — path-independent of the
    TODO:confirm field names; mis-scaling would mis-state on-chain amounts as facts (Inv #5)."""
    from backend.app.normalization.bitquery_adapter import _to_base_units
    assert _to_base_units("1.5", 18) == "1500000000000000000"
    assert _to_base_units("0", 8) == "0"
    assert _to_base_units("1.23456789", 6) == "1234568"     # ROUND_HALF_EVEN
    assert _to_base_units(2, 0) == "2"
    assert _to_base_units("not-a-number", 18) == "0"         # defensive: unparseable -> 0, no crash
