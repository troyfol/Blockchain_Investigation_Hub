"""P8.7.2 — responsive ingest: valuation decoupled from the ingest response + progress/cancel jobs.

  * ingesting a busy address returns quickly and does NOT value inline (the fast path);
  * the fetch is an observable, cancelable job; a canceled run leaves a CONSISTENT case (no partial rows);
  * offline ingest skips valuation cleanly.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from backend.app.audits.runner import run_audits
from backend.app.config import get_settings
from backend.app.connectors.base import BaseHttpConnector, RateLimiter
from backend.app.connectors.esplora import EsploraConnector
from backend.app.main import app, get_case_db_path, get_orchestrator
from backend.app.services import jobs
from backend.app.services.orchestrator import Orchestrator
from backend.tests.integration._helpers import new_case

from fastapi.testclient import TestClient

CASS = Path(__file__).resolve().parent.parent / "cassettes" / "esplora"
BTC = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"


def _router(request):
    p = request.url.path
    if p.endswith("/blocks/tip/height"):
        return httpx.Response(200, text="800010")
    if "/address/" in p and p.endswith("/txs"):
        return httpx.Response(200, json=json.loads((CASS / "address_txs.json").read_text()))
    if "/address/" in p and "/txs/chain/" in p:
        return httpx.Response(200, json=[])
    return httpx.Response(200, json=json.loads((CASS / "address_stats.json").read_text()))


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("BIH_APP_DATA_DIR", str(tmp_path / "appdata"))
    jobs.clear()
    yield
    from backend.app.services import settings_store
    settings_store.set_offline(False)
    jobs.clear()


# --------------------------------------------------------------------------- the jobs registry (unit)

def test_jobs_registry_supersede_cancel_and_check():
    a = jobs.start("ingest")
    assert jobs.active() is a and a.state == "running"
    b = jobs.start("valuation")               # a new job supersedes (cancels) the running one
    assert a.state == "canceled" and jobs.active() is b
    jobs.note_request()                        # progress + honor cancel (b still running -> ok)
    assert b.requests == 1
    assert jobs.cancel_active() is True
    with pytest.raises(jobs.JobCancelled):
        b.check_cancel()


# --------------------------------------------------------------------------- ingest is fast + unvalued

@respx.mock
def test_ingest_returns_facts_without_valuing_inline(tmp_path):
    conn, db = new_case(tmp_path, title="Busy")
    respx.route(host="blockstream.info").mock(side_effect=_router)
    connector = EsploraConnector(settings=get_settings(), rate_limiter=RateLimiter(0, enabled=False),
                                 sleep=lambda _s: None)
    app.dependency_overrides[get_case_db_path] = lambda: str(db)
    app.dependency_overrides[get_orchestrator] = lambda: Orchestrator([connector])
    try:
        c = TestClient(app)
        resp = c.post("/api/graph/expand", json={"chain": "bitcoin", "address": BTC})
        body = resp.json()
        assert "error" not in body and len(body["graph"]["nodes"]) > 0       # facts ingested
        # P8.7.2 — api_expand must NOT value inline: no valuation rows; no DeFiLlama host was even called.
        assert conn.execute("SELECT COUNT(*) FROM valuation").fetchone()[0] == 0
        assert not any("llama.fi" in str(c.request.url) for c in respx.calls)
    finally:
        app.dependency_overrides.clear()
        connector.close()


# --------------------------------------------------------------------------- cancel -> consistent case

@respx.mock
def test_canceled_ingest_leaves_a_consistent_case(tmp_path):
    """A cancel is honored at a page boundary BEFORE any write, so nothing partial is written and the
    case stays consistent (audits pass)."""
    conn, db = new_case(tmp_path, title="Cancel")
    respx.route(host="blockstream.info").mock(side_effect=_router)
    connector = EsploraConnector(settings=get_settings(), rate_limiter=RateLimiter(0, enabled=False),
                                 sleep=lambda _s: None)
    job = jobs.start("ingest")
    job.cancel()                       # cancel BEFORE the fetch -> the first request() raises
    try:
        with pytest.raises(jobs.JobCancelled):
            connector.get_transactions(conn, "bitcoin", BTC)
    finally:
        connector.close()
    # nothing was written; the case is consistent
    assert conn.execute("SELECT COUNT(*) FROM transaction_").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM source_query").fetchone()[0] == 0
    assert all(r.passed for r in run_audits(db_path=str(db)))


@respx.mock
def test_cancel_midway_writes_only_complete_source_queries(tmp_path):
    """Cancel DURING the fetch (after the first page) still leaves complete source_queries only — the
    write happens after collection, so an interrupted collection writes nothing for that action."""
    conn, db = new_case(tmp_path, title="Midway")

    def cancelling_router(request):
        if "/address/" in request.url.path and request.url.path.endswith("/txs"):
            jobs.cancel_active()          # cancel right as the tx page returns
        return _router(request)

    respx.route(host="blockstream.info").mock(side_effect=cancelling_router)
    connector = EsploraConnector(settings=get_settings(), rate_limiter=RateLimiter(0, enabled=False),
                                 sleep=lambda _s: None)
    jobs.start("ingest")
    try:
        with pytest.raises(jobs.JobCancelled):
            connector.get_transactions(conn, "bitcoin", BTC)
    finally:
        connector.close()
    # the cancel fired before _write_btc ran -> no partial rows, audits pass
    assert conn.execute("SELECT COUNT(*) FROM transaction_").fetchone()[0] == 0
    assert all(r.passed for r in run_audits(db_path=str(db)))


# --------------------------------------------------------------------------- cancel during backoff (review #4/#5)

@respx.mock
def test_cancel_during_backoff_is_honored_before_the_next_request():
    """Review #4/#5: a cancel arriving during a 429 backoff sleep is honored at the TOP of the retry loop
    — before another full network round-trip — and surfaces as JobCancelled, never an UpstreamError from
    exhausted retries."""
    jobs.start("ingest")
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return httpx.Response(429)

    respx.route(host="rate.test").mock(side_effect=handler)
    # the backoff sleep simulates a cancel arriving while we're backing off
    c = BaseHttpConnector(base_url="https://rate.test/", rate_limiter=RateLimiter(0, enabled=False),
                          sleep=lambda _s: jobs.cancel_active())
    try:
        with pytest.raises(jobs.JobCancelled):
            c.request(path="x")
    finally:
        c.close()
    assert calls["n"] == 1   # the loop-top check fired after the first backoff -> no second network attempt


# --------------------------------------------------------------------------- offline skips valuation

def test_offline_ingest_skips_fetch_and_valuation(tmp_path):
    from backend.app.services import settings_store

    conn, db = new_case(tmp_path, title="Offline")
    settings_store.set_offline(True)
    app.dependency_overrides[get_case_db_path] = lambda: str(db)
    try:
        c = TestClient(app)
        body = c.post("/api/graph/expand", json={"chain": "bitcoin", "address": BTC}).json()
        assert body.get("offline") is True
        assert conn.execute("SELECT COUNT(*) FROM valuation").fetchone()[0] == 0   # no valuation
        assert conn.execute("SELECT COUNT(*) FROM transaction_").fetchone()[0] == 0  # no fetch either
        # /api/valuation/run is also offline-guarded
        assert c.post("/api/valuation/run").status_code == 409
    finally:
        app.dependency_overrides.clear()
