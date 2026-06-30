"""Orchestrator dispatch must route on capability AND chain (two connectors registered)."""

from __future__ import annotations

import pytest

from backend.app.config import get_settings
from backend.app.connectors.base import RateLimiter
from backend.app.connectors.esplora import EsploraConnector
from backend.app.connectors.etherscan import EtherscanConnector
from backend.app.services.orchestrator import NoConnectorError, Orchestrator


@pytest.fixture
def connectors():
    eth = EtherscanConnector(api_key="x", settings=get_settings(),
                             rate_limiter=RateLimiter(0, enabled=False))
    btc = EsploraConnector(settings=get_settings(), rate_limiter=RateLimiter(0, enabled=False))
    yield eth, btc
    eth.close()
    btc.close()


def test_routes_by_chain_not_just_capability(connectors):
    eth, btc = connectors
    orch = Orchestrator([eth, btc])
    # Both provide get_transactions/get_balance — chain decides which connector.
    assert orch._for("get_transactions", "ethereum") is eth
    assert orch._for("get_transactions", "bitcoin") is btc
    assert orch._for("get_balance", "arbitrum") is eth
    assert orch._for("get_transfers", "bitcoin") is btc  # only Esplora has get_transfers


def test_dispatch_is_order_independent(connectors):
    eth, btc = connectors
    # Reverse registration order: still routes bitcoin->esplora (not first-match-by-capability).
    orch = Orchestrator([btc, eth])
    assert orch._for("get_transactions", "ethereum") is eth
    assert orch._for("get_transactions", "bitcoin") is btc


def test_unknown_chain_raises(connectors):
    eth, btc = connectors
    orch = Orchestrator([eth, btc])
    with pytest.raises(NoConnectorError):
        orch._for("get_transactions", "dogecoin")
