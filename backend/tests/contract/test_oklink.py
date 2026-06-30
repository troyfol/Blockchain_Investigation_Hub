"""OKLink connector SHELL — gating + confirmed conventions (paid; docs/findings/paid_api_integrations.md
§4). The AML endpoint paths/fields are TODO: confirm, so the capabilities raise a clear "not wired"
error rather than guess. Tested: the no-key guard, the chainShortName map, the {code,msg,data} envelope
parser, and that a keyed call surfaces the honest "not wired" message (no endpoint is invented).
"""

from __future__ import annotations

import pytest

from backend.app.connectors.base import ConnectorError, UpstreamError
from backend.app.connectors.oklink import CHAIN_TO_SHORTNAME, OkLinkConnector
from backend.tests.integration._helpers import new_case


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="OKLink")
    yield conn, db
    conn.close()


def test_no_key_raises_naming_the_keyring_entry(case):
    conn, _ = case
    c = OkLinkConnector(api_key="")
    with pytest.raises(ConnectorError) as exc:
        c.get_attributions(conn, "ethereum", "0xabc")
    c.close()
    assert "oklink_api_key" in str(exc.value)


def test_keyed_capabilities_are_honestly_not_wired(case):
    conn, _ = case
    c = OkLinkConnector(api_key="k")
    for cap in (c.get_attributions, c.get_risk):
        with pytest.raises(ConnectorError) as exc:
            cap(conn, "ethereum", "0xabc")
        assert "not wired" in str(exc.value) and "TODO: confirm" in str(exc.value)
    c.close()


def test_chain_shortname_map():
    c = OkLinkConnector(api_key="k")
    assert c.short_name("ethereum") == "ETH" and c.short_name("bsc") == "BSC"
    assert CHAIN_TO_SHORTNAME["polygon"] == "POLYGON"
    with pytest.raises(UpstreamError):
        c.short_name("no-such-chain")
    c.close()


def test_envelope_parser():
    c = OkLinkConnector(api_key="k")
    assert c._data({"code": "0", "msg": "", "data": [{"x": 1}]}) == [{"x": 1}]
    assert c._data({"code": "0", "data": None}) == []           # non-list data -> []
    with pytest.raises(UpstreamError):
        c._data({"code": "50011", "msg": "rate limited"})        # code != 0 -> error
    with pytest.raises(ConnectorError):
        c._data(["not", "a", "dict"])
    c.close()
