"""Golden EVM ingest (phase_02 step 5; docs/testing.md §3).

Drives the real EtherscanConnector with httpx mocked (respx) to replay cassettes, then asserts
the canonical rows, per-endpoint provenance, idempotent re-fetch, bounds recording, and that all
invariant audits (incl. #10 bounds-recorded) stay green.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from backend.app.audits.runner import run_audits
from backend.app.config import get_settings
from backend.app.connectors.base import ConnectorError, RateLimiter
from backend.app.connectors.etherscan import EtherscanConnector
from backend.app.db import apply_migrations, get_connection
from backend.app.db import repository as repo

CASSETTES = Path(__file__).resolve().parent.parent / "cassettes" / "etherscan"
G = "0x4e83362442b8d1bec281594cea3050c8eb01311c"
H = "0x642ae78fafbb8032da552d619ad43f1d81e4dd7c"
C = "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2"
BASE = get_settings().etherscan_base_url
CASSETTE = {"txlist": "txlist.json", "txlistinternal": "txlistinternal.json",
            "tokentx": "tokentx.json", "balance": "balance.json"}


def _payload(action):
    return json.loads((CASSETTES / CASSETTE[action]).read_text())


def _router(request):
    return httpx.Response(200, json=_payload(request.url.params.get("action")))


@pytest.fixture
def case(tmp_path):
    db = tmp_path / "case.db"
    apply_migrations(db)
    conn = get_connection(db)
    repo.init_case(conn, title="EVM Golden")
    yield conn, db
    conn.close()


@pytest.fixture
def connector():
    c = EtherscanConnector(api_key="test", settings=get_settings(),
                           rate_limiter=RateLimiter(0, enabled=False), sleep=lambda _s: None)
    yield c
    c.close()


def _counts(conn, tables):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


@respx.mock
@pytest.mark.smoke
def test_evm_golden_ingest(case, connector):
    conn, db = case
    respx.get(BASE).mock(side_effect=_router)

    connector.get_transactions(conn, "ethereum", G)
    connector.get_balance(conn, "ethereum", G)

    assert conn.execute("SELECT COUNT(*) FROM transaction_").fetchone()[0] == 2
    types = dict(conn.execute(
        "SELECT transfer_type, COUNT(*) FROM transfer GROUP BY transfer_type").fetchall())
    assert types == {"native": 1, "internal": 1, "erc20": 1}
    assert conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0] == 2          # native ETH + MKR
    assert conn.execute("SELECT COUNT(*) FROM address").fetchone()[0] == 3        # G, H, C
    assert conn.execute(
        "SELECT COUNT(*) FROM transaction_ WHERE finality_status='final'").fetchone()[0] == 2

    # (transaction_id, transfer_type, position) collision-free is enforced by the unique index;
    # assert the native/internal/erc20 transfers of the multi-transfer tx coexist.
    tx_b = conn.execute("SELECT id, fee FROM transaction_ WHERE tx_hash=?", ("0x" + "b" * 64,)).fetchone()
    assert conn.execute("SELECT COUNT(*) FROM transfer WHERE transaction_id=?", (tx_b["id"],)).fetchone()[0] == 2
    # The same tx is written by 3 endpoints; it is ONE row and txlist's fee survived internal's NULL.
    assert conn.execute("SELECT COUNT(*) FROM transaction_ WHERE tx_hash=?", ("0x" + "b" * 64,)).fetchone()[0] == 1
    assert tx_b["fee"] == str(120000 * 25000000000)

    # Each endpoint wrote its own source_query with a raw hash + recorded bounds.
    sqs = conn.execute(
        "SELECT endpoint, raw_response_hash, params FROM source_query ORDER BY endpoint").fetchall()
    assert len(sqs) == 4  # txlist, txlistinternal, tokentx, balance
    for s in sqs:
        assert s["raw_response_hash"]
        assert "bounds" in json.loads(s["params"])

    # Addresses stored lowercase-canonical with display retained.
    g = conn.execute("SELECT address, address_display FROM address WHERE address=?", (G,)).fetchone()
    assert g["address"] == G and g["address_display"] == G

    results = run_audits(db_path=str(db))
    failed = [(r.name, r.offending) for r in results if not r.passed]
    assert failed == [], f"audits failed: {failed}"
    assert any(r.name == "bounds-recorded" and r.passed for r in results)  # #10 truly passed


@respx.mock
def test_evm_idempotent_refetch(case, connector):
    conn, db = case
    respx.get(BASE).mock(side_effect=_router)
    tables = ("transaction_", "transfer", "asset", "address")

    connector.get_transactions(conn, "ethereum", G)
    before = _counts(conn, tables)
    connector.get_transactions(conn, "ethereum", G)  # re-fetch: upsert, no new fact rows
    after = _counts(conn, tables)
    assert before == after
    # ...but each fetch is its own provenance (3 endpoint source_queries per call).
    assert conn.execute("SELECT COUNT(*) FROM source_query").fetchone()[0] == 6
    assert all(r.passed for r in run_audits(db_path=str(db)))


@respx.mock
def test_bounds_max_pages_marks_partial_and_records(case):
    conn, db = case
    c = EtherscanConnector(api_key="test", settings=get_settings(), page_size=2,
                           rate_limiter=RateLimiter(0, enabled=False), sleep=lambda _s: None)
    two = {"status": "1", "message": "OK", "result": [
        {"blockNumber": "10", "timeStamp": "1693200000", "hash": "0x" + "c" * 64, "from": G, "to": H,
         "value": "1", "gasUsed": "21000", "gasPrice": "1", "confirmations": "5", "isError": "0"},
        {"blockNumber": "11", "timeStamp": "1693200001", "hash": "0x" + "d" * 64, "from": G, "to": H,
         "value": "1", "gasUsed": "21000", "gasPrice": "1", "confirmations": "5", "isError": "0"},
    ]}
    empty = {"status": "1", "message": "OK", "result": []}

    def router(request):
        action = request.url.params.get("action")
        return httpx.Response(200, json=two if action == "txlist" else empty)

    respx.get(BASE).mock(side_effect=router)
    try:
        c.get_transactions(conn, "ethereum", G, bounds={"max_pages": 1, "block_range": (0, 999)})
    finally:
        c.close()

    sq = conn.execute("SELECT status, params FROM source_query WHERE endpoint='txlist'").fetchone()
    assert sq["status"] == "partial"  # page_size rows on page 1 + max_pages=1 -> truncated
    bounds = json.loads(sq["params"])["bounds"]
    assert bounds["max_pages"] == 1 and bounds["block_range"] == [0, 999]
    assert all(r.passed for r in run_audits(db_path=str(db)))


@respx.mock
def test_balance_is_append_only(case, connector):
    conn, db = case
    respx.get(BASE).mock(side_effect=_router)
    connector.get_balance(conn, "ethereum", G)
    connector.get_balance(conn, "ethereum", G)  # a claim re-fetch is a NEW row, not an upsert
    assert conn.execute("SELECT COUNT(*) FROM balance_snapshot").fetchone()[0] == 2
    assert all(r.passed for r in run_audits(db_path=str(db)))


@respx.mock
def test_direction_out_filter_excludes_inbound(case, connector):
    conn, db = case
    rows = {"status": "1", "message": "OK", "result": [
        {"blockNumber": "10", "timeStamp": "1693200000", "hash": "0x" + "1" * 64, "from": G, "to": H,
         "value": "5", "gasUsed": "1", "gasPrice": "1", "confirmations": "100", "isError": "0"},
        {"blockNumber": "11", "timeStamp": "1693200001", "hash": "0x" + "2" * 64, "from": H, "to": G,
         "value": "7", "gasUsed": "1", "gasPrice": "1", "confirmations": "100", "isError": "0"},
    ]}
    empty = {"status": "1", "message": "OK", "result": []}

    def router(request):
        action = request.url.params.get("action")
        return httpx.Response(200, json=rows if action == "txlist" else empty)

    respx.get(BASE).mock(side_effect=router)
    connector.get_transactions(conn, "ethereum", G, bounds={"direction": "out"})
    hashes = {r[0] for r in conn.execute("SELECT tx_hash FROM transaction_").fetchall()}
    assert hashes == {"0x" + "1" * 64}  # only the G-as-sender tx survived the 'out' filter
    assert all(r.passed for r in run_audits(db_path=str(db)))


@respx.mock
def test_empty_results_records_provenance_only(case, connector):
    conn, db = case
    empty = {"status": "0", "message": "No transactions found", "result": []}
    respx.get(BASE).mock(return_value=httpx.Response(200, json=empty))
    connector.get_transactions(conn, "ethereum", G)
    assert conn.execute("SELECT COUNT(*) FROM transaction_").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM transfer").fetchone()[0] == 0
    sqs = conn.execute("SELECT status FROM source_query").fetchall()
    assert len(sqs) == 3 and all(s["status"] == "ok" for s in sqs)  # no-records != error
    assert all(r.passed for r in run_audits(db_path=str(db)))


@respx.mock
def test_unsupported_bounds_are_skipped_and_marked_partial(case, connector):
    """P8.6: an unsupported bound is now TOLERATED — skipped (recorded in params) + the query marked
    partial — instead of raising and aborting the ingest (so the chain-agnostic depth control can't
    hard-fail an ingest by sending a bound this connector doesn't apply)."""
    import json

    conn, _ = case
    empty = {"status": "0", "message": "No transactions found", "result": []}
    respx.get(BASE).mock(return_value=httpx.Response(200, json=empty))
    connector.get_transactions(conn, "ethereum", G, bounds={"no_such_bound": 1})  # does NOT raise
    rows = conn.execute("SELECT status, params FROM source_query").fetchall()
    assert rows and all(r["status"] == "partial" for r in rows)  # skipping a bound marks partial
    assert all(json.loads(r["params"]).get("skipped_bounds") == ["no_such_bound"] for r in rows)


K = "0x" + "1" * 40
L = "0x" + "2" * 40


def _txrow(h, frm, to, val):
    return {"blockNumber": "100", "timeStamp": "1693200000", "hash": h, "from": frm, "to": to,
            "value": val, "gasUsed": "1", "gasPrice": "1", "confirmations": "100", "isError": "0"}


@respx.mock
def test_time_window_resolves_to_block_range(case, connector):
    conn, db = case
    seen_txlist_range = []

    def router(request):
        action = request.url.params.get("action")
        if action == "getblocknobytime":
            closest = request.url.params.get("closest")
            blk = "18000000" if closest == "after" else "18000200"
            return httpx.Response(200, json={"status": "1", "message": "OK", "result": blk})
        if action == "txlist":
            seen_txlist_range.append((request.url.params.get("startblock"),
                                      request.url.params.get("endblock")))
        return httpx.Response(200, json={"status": "1", "message": "OK", "result": []})

    respx.get(BASE).mock(side_effect=router)
    connector.get_transactions(conn, "ethereum", G,
                               bounds={"time_window": ("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")})
    # the window resolved to (18000000, 18000200) and drove the actual txlist fetch
    assert seen_txlist_range[0] == ("18000000", "18000200")
    bounds = json.loads(conn.execute(
        "SELECT params FROM source_query WHERE endpoint='txlist'").fetchone()["params"])["bounds"]
    assert "time_window" in bounds  # original bound recorded for reproducibility
    assert all(r.passed for r in run_audits(db_path=str(db)))


@respx.mock
def test_top_n_counterparties_filters(case, connector):
    conn, db = case
    rows = {"status": "1", "message": "OK", "result": [
        _txrow("0x" + "1" * 64, G, H, "10"), _txrow("0x" + "2" * 64, G, H, "20"),
        _txrow("0x" + "3" * 64, G, K, "30"), _txrow("0x" + "4" * 64, G, L, "40"),
    ]}
    empty = {"status": "1", "message": "OK", "result": []}

    def router(request):
        action = request.url.params.get("action")
        return httpx.Response(200, json=rows if action == "txlist" else empty)

    respx.get(BASE).mock(side_effect=router)
    connector.get_transactions(conn, "ethereum", G, bounds={"top_n_counterparties": 1})
    # H is the top counterparty (2 transfers); K and L (1 each) are dropped.
    assert conn.execute("SELECT COUNT(*) FROM transfer").fetchone()[0] == 2
    tos = {r[0] for r in conn.execute(
        "SELECT a.address FROM transfer tr JOIN address a ON a.id=tr.to_address_id").fetchall()}
    assert tos == {H}
    bounds = json.loads(conn.execute(
        "SELECT params FROM source_query WHERE endpoint='txlist'").fetchone()["params"])["bounds"]
    assert bounds["top_n_counterparties"] == 1
    assert all(r.passed for r in run_audits(db_path=str(db)))


@respx.mock
def test_top_n_zero_keeps_no_counterparty_transfers(case, connector):
    conn, db = case
    rows = {"status": "1", "message": "OK", "result": [
        _txrow("0x" + "1" * 64, G, H, "10"), _txrow("0x" + "2" * 64, G, K, "20")]}
    empty = {"status": "1", "message": "OK", "result": []}
    respx.get(BASE).mock(side_effect=lambda r: httpx.Response(
        200, json=rows if r.url.params.get("action") == "txlist" else empty))
    connector.get_transactions(conn, "ethereum", G, bounds={"top_n_counterparties": 0})
    # No counterparty in the top-0 set -> all transfers dropped; tx nodes still recorded as facts.
    assert conn.execute("SELECT COUNT(*) FROM transfer").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM transaction_").fetchone()[0] == 2
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_inverted_block_range_raises(case, connector):
    conn, _ = case
    with pytest.raises(ConnectorError):  # raised before HTTP — don't send a silently-empty query
        connector.get_transactions(conn, "ethereum", G, bounds={"block_range": (200, 100)})
