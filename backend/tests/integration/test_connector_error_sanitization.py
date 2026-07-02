"""Batch 5 (SEC-03): a persistent 4xx from a query-string-key connector must surface as a SANITIZED
``UpstreamError`` — never an ``httpx.HTTPStatusError`` whose message embeds the key-bearing request URL
(Etherscan/MisTrack put ``apikey`` in the query string), which would leak the key into the 500 logger.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.app.config import get_settings
from backend.app.connectors.base import RateLimiter, UpstreamError, _redact_url
from backend.app.connectors.etherscan import EtherscanConnector
from backend.app.db import repository as repo
from backend.tests.integration._helpers import new_case

BASE = get_settings().etherscan_base_url
SECRET = "SECRETKEY123abcXYZ"


@respx.mock
def test_4xx_raises_sanitized_upstream_error(tmp_path):
    conn, db = new_case(tmp_path)
    respx.get(BASE).mock(return_value=httpx.Response(403, text="Forbidden"))
    c = EtherscanConnector(api_key=SECRET, settings=get_settings(),
                           rate_limiter=RateLimiter(0, enabled=False), sleep=lambda _s: None)
    try:
        with pytest.raises(UpstreamError) as ei:
            c.get_transactions(conn, "ethereum", "0x" + "11" * 20)
    finally:
        c.close()
    msg = str(ei.value)
    assert "403" in msg
    assert SECRET not in msg, "the API key leaked into the error message (SEC-03)"
    assert "apikey" not in msg.lower(), "the apikey param leaked into the error message (SEC-03)"


def test_redact_url_strips_query():
    assert _redact_url("https://api.etherscan.io/v2/api?module=account&apikey=SECRET") \
        == "https://api.etherscan.io/v2/api"
    assert _redact_url("https://blockstream.info/api/address/bc1qx/txs") \
        == "https://blockstream.info/api/address/bc1qx/txs"
