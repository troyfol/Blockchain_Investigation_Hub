"""Deferred LOG-06: populate `erc20_approval` from Etherscan getLogs (Approval events) so the EVM
self-authorization clustering heuristic can actually FIRE (it previously always no-op'd on an empty
table). Confirms: the getLogs decode → rows written, the heuristic clusters owner↔spender, and re-fetch
is idempotent (insert-once)."""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.app.config import get_settings
from backend.app.connectors.base import RateLimiter
from backend.app.connectors.etherscan import EtherscanConnector
from backend.app.normalization.etherscan_adapter import APPROVAL_TOPIC0
from backend.app.services.clustering.evm import preview_self_authorization
from backend.tests.integration._helpers import new_case

BASE = get_settings().etherscan_base_url
O1 = "0x" + "11" * 20
S1 = "0x" + "22" * 20
S2 = "0x" + "33" * 20
TOKEN = "0x" + "ab" * 20


def _topic(addr: str) -> str:
    return "0x" + addr[2:].rjust(64, "0")


def _log(owner, spender, *, tx, block="0xc48174", amount="0x64"):
    return {"address": TOKEN, "topics": [APPROVAL_TOPIC0, _topic(owner), _topic(spender)],
            "data": amount, "blockNumber": block, "timeStamp": "0x60f9ce56", "transactionHash": tx}


def _connector():
    return EtherscanConnector(api_key="test", settings=get_settings(),
                              rate_limiter=RateLimiter(0, enabled=False), sleep=lambda _s: None)


@respx.mock
def test_getlogs_populates_approvals_and_heuristic_fires(tmp_path):
    conn, db = new_case(tmp_path)
    logs = [_log(O1, S1, tx="0x" + "a1" * 32), _log(O1, S2, tx="0x" + "a2" * 32)]
    respx.get(BASE).mock(return_value=httpx.Response(200, json={"status": "1", "message": "OK", "result": logs}))

    c = _connector()
    try:
        res = c.get_erc20_approvals(conn, "ethereum", O1)
    finally:
        c.close()
    assert res["approvals"] == 2
    assert conn.execute("SELECT COUNT(*) FROM erc20_approval").fetchone()[0] == 2

    # The self-authorization heuristic now clusters owner↔spender (was a permanent no-op on empty data).
    prev = preview_self_authorization(conn)
    assert prev["n_clusters"] == 1
    cluster_addrs = set(prev["clusters"][0])
    ids = {a: r[0] for a, r in [(O1, conn.execute("SELECT id FROM address WHERE address=?", (O1,)).fetchone()),
                                (S1, conn.execute("SELECT id FROM address WHERE address=?", (S1,)).fetchone()),
                                (S2, conn.execute("SELECT id FROM address WHERE address=?", (S2,)).fetchone())]}
    assert cluster_addrs == {ids[O1], ids[S1], ids[S2]}, "self-authorization did not cluster owner↔spenders"
    conn.close()


@respx.mock
def test_getlogs_refetch_is_idempotent(tmp_path):
    conn, db = new_case(tmp_path)
    logs = [_log(O1, S1, tx="0x" + "a1" * 32)]
    respx.get(BASE).mock(return_value=httpx.Response(200, json={"status": "1", "message": "OK", "result": logs}))
    c = _connector()
    try:
        c.get_erc20_approvals(conn, "ethereum", O1)
        c.get_erc20_approvals(conn, "ethereum", O1)  # re-fetch the SAME approval
    finally:
        c.close()
    assert conn.execute("SELECT COUNT(*) FROM erc20_approval").fetchone()[0] == 1, "re-fetch duplicated (LOG-06/Inv#7)"
    conn.close()
