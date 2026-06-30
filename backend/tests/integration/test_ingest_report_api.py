"""P8.5 — make the app self-sufficient: ingest from the UI, Etherscan key, report button.

End-to-end through the API (TestClient), with HTTP mocked (respx) where a connector reaches out:
  * a BITCOIN address ingests through the REAL Esplora connector (keyless) and an empty case becomes
    populated via POST /api/graph/expand;
  * EVM ingest is gated on the free Etherscan key — without it the expand returns a clear "add a key"
    error (no doomed network call); WITH it the orchestrator builds the connector and attempts a fetch;
  * offline mode blocks ingest up-front with a clear message (connector-independent);
  * POST /api/report returns the immutable content_hash and cleanly SKIPS the PDF when no engine is
    forced (BIH_REPORT_RENDERER=none) — never an error;
  * the free-pillar key endpoint stores/clears the Etherscan key write-only (presence only, never a value);
  * GET /api/chains + /api/settings.evm_chains expose the ingestable EVM chains (sourced from the
    connector map, so the UI can't offer a chain that fails).

All keyring access uses an in-memory backend so the real OS Credential Manager is never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import keyring
import pytest
import respx
from fastapi.testclient import TestClient
from keyring.backend import KeyringBackend

from backend.app.config import get_settings
from backend.app.connectors.base import RateLimiter
from backend.app.connectors.esplora import EsploraConnector
from backend.app.db import apply_migrations, get_connection
from backend.app.db import repository as repo
from backend.app.main import app, get_case_db_path, get_orchestrator
from backend.app.services.orchestrator import Orchestrator

CASSETTES = Path(__file__).resolve().parent.parent / "cassettes" / "esplora"
BTC_ADDR = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
TIP = "800010"
EVM_ADDR = "0x" + "a" * 40


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self._store.get((service, username))

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def delete_password(self, service, username):
        self._store.pop((service, username), None)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("BIH_APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("BIH_CASES_ROOT", str(tmp_path / "cases_root"))
    monkeypatch.delenv("BIH_ALLOW_PLAINTEXT_KEYS", raising=False)
    monkeypatch.delenv("BIH_CASE_DB", raising=False)
    prev = keyring.get_keyring()
    keyring.set_keyring(MemoryKeyring())
    from backend.app.services import cases, settings_store

    cases.clear_active_case()
    settings_store.set_offline(False)
    yield
    cases.clear_active_case()
    settings_store.set_offline(False)
    keyring.set_keyring(prev)


@pytest.fixture
def client():
    return TestClient(app)


def _empty_case(tmp_path) -> Path:
    db = tmp_path / "case" / "case.db"
    apply_migrations(db)
    conn = get_connection(db)
    repo.init_case(conn, title="Ingest Case")
    conn.close()
    return db


# --------------------------------------------------------------------------- Esplora HTTP mock

def _esplora_router(request):
    p = request.url.path
    if p.endswith("/blocks/tip/height"):
        return httpx.Response(200, text=TIP)
    if "/address/" in p and p.endswith("/txs"):
        return httpx.Response(200, json=json.loads((CASSETTES / "address_txs.json").read_text()))
    if "/address/" in p and "/txs/chain/" in p:
        return httpx.Response(200, json=[])  # no further pages
    if "/address/" in p:
        return httpx.Response(200, json=json.loads((CASSETTES / "address_stats.json").read_text()))
    return httpx.Response(404)


# --------------------------------------------------------------------------- BTC ingest (e2e, keyless)

@respx.mock
def test_btc_ingest_populates_an_empty_case(tmp_path):
    """A brand-new empty case is SEEDABLE: ingest a Bitcoin address (keyless Esplora, HTTP mocked) via
    /api/graph/expand and the graph goes from empty to populated."""
    db = _empty_case(tmp_path)
    respx.route(host="blockstream.info").mock(side_effect=_esplora_router)
    connector = EsploraConnector(settings=get_settings(), rate_limiter=RateLimiter(0, enabled=False),
                                 sleep=lambda _s: None)
    app.dependency_overrides[get_case_db_path] = lambda: str(db)
    app.dependency_overrides[get_orchestrator] = lambda: Orchestrator([connector])
    try:
        c = TestClient(app)
        before = c.get("/api/graph").json()
        assert len(before["nodes"]) == 0  # empty to start

        resp = c.post("/api/graph/expand", json={"chain": "bitcoin", "address": BTC_ADDR})
        assert resp.status_code == 200
        body = resp.json()
        assert "error" not in body, body
        assert len(body["graph"]["nodes"]) > 0  # the empty case is now populated
        # The UTXO shape arrived (a transaction node + addresses), not a fabricated transfer (Inv #5).
        kinds = {n["kind"] for n in body["graph"]["nodes"]}
        assert "transaction" in kinds and "address" in kinds
    finally:
        app.dependency_overrides.clear()
        connector.close()


# --------------------------------------------------------------------------- EVM ingest needs a key

def test_evm_ingest_without_key_returns_clear_guidance(client, tmp_path):
    """No Etherscan key -> the orchestrator builds NO EVM connector and the expand returns a clear
    'add a free Etherscan key' error (with needs_key) instead of a doomed fetch / raw upstream message."""
    db = _empty_case(tmp_path)
    app.dependency_overrides[get_case_db_path] = lambda: str(db)
    try:
        resp = client.post("/api/graph/expand", json={"chain": "ethereum", "address": EVM_ADDR})
        assert resp.status_code == 200
        body = resp.json()
        assert "error" in body and "etherscan" in body["error"].lower()
        assert body.get("needs_key") == "etherscan"
    finally:
        app.dependency_overrides.clear()


@respx.mock
def test_setting_etherscan_key_flips_evm_ingest_to_a_fetch(client, tmp_path):
    """Setting the key flips EVM ingest from 'missing key' to actually ATTEMPTING a fetch: with a key the
    orchestrator builds the Etherscan connector and calls it (HTTP mocked to an empty result), so there is
    no 'needs key' error anymore."""
    from backend.app import secrets

    db = _empty_case(tmp_path)
    secrets.set_secret("etherscan", "FREE-ETHERSCAN-KEY")  # in-memory keyring
    # Etherscan's "no records" envelope for every account endpoint (txlist/internal/tokentx).
    respx.route(host="api.etherscan.io").mock(
        return_value=httpx.Response(200, json={"status": "0", "message": "No transactions found",
                                               "result": []}))
    app.dependency_overrides[get_case_db_path] = lambda: str(db)
    try:
        resp = client.post("/api/graph/expand", json={"chain": "ethereum", "address": EVM_ADDR})
        assert resp.status_code == 200
        body = resp.json()
        assert "needs_key" not in body            # the key is set — no longer a missing-key error
        assert "error" not in body, body          # it attempted the (mocked, empty) fetch cleanly
        assert respx.calls.call_count >= 1         # a fetch was actually attempted
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- offline blocks ingest

def test_offline_blocks_ingest_even_for_evm_without_key(client, tmp_path):
    """Offline mode short-circuits ingest up-front with a clear message — and it WINS over the
    missing-key path (offline is the real reason)."""
    from backend.app.services import settings_store

    db = _empty_case(tmp_path)
    app.dependency_overrides[get_case_db_path] = lambda: str(db)
    settings_store.set_offline(True)
    try:
        resp = client.post("/api/graph/expand", json={"chain": "ethereum", "address": EVM_ADDR})
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("offline") is True
        assert "offline" in body["error"].lower()
        assert "needs_key" not in body  # offline wins over the missing-key guidance
    finally:
        settings_store.set_offline(False)
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- report from the UI

def test_report_endpoint_returns_hash_and_skips_pdf_cleanly(client, tmp_path, monkeypatch):
    """POST /api/report returns the immutable content_hash (over the HTML) and, with no engine forced,
    cleanly SKIPS the PDF (pdf_path null + a skip reason) — never an error. The HTML lands under the case
    dir (writable user data, never the bundle)."""
    db = _empty_case(tmp_path)
    monkeypatch.setenv("BIH_CASE_DB", str(db))          # active_case_path() -> this case
    monkeypatch.setenv("BIH_REPORT_RENDERER", "none")   # force the clean no-PDF skip (deterministic)
    try:
        resp = client.post("/api/report", json={"title": "Test Report"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert isinstance(body["content_hash"], str) and len(body["content_hash"]) == 64
        assert body["pdf_path"] is None                 # skipped, not failed
        assert body["pdf_skip_reason"]                  # a clean reason is surfaced
        html = Path(body["html_path"])
        assert html.exists() and html.is_relative_to(db.parent)  # under the case dir
    finally:
        monkeypatch.delenv("BIH_REPORT_RENDERER", raising=False)


def test_report_open_rejects_paths_outside_the_case(client, tmp_path, monkeypatch):
    """The open-file endpoint only opens files UNDER the active case dir (never an arbitrary path)."""
    db = _empty_case(tmp_path)
    monkeypatch.setenv("BIH_CASE_DB", str(db))
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("secret", encoding="utf-8")
    resp = client.post("/api/report/open", json={"path": str(outside)})
    assert resp.status_code == 400  # refused: outside the active case


# --------------------------------------------------------------------------- free-pillar key + chains

def test_free_pillar_key_set_and_clear_for_etherscan(client):
    """The Etherscan (free pillar) key uses the same write-only keyring endpoint as paid connectors:
    presence flips in /api/settings, and the value is NEVER serialized."""
    # No key yet -> requires_key True, key_present False.
    free = {c["name"]: c for c in client.get("/api/settings").json()["connectors"]["free"]}
    assert free["etherscan"]["requires_key"] is True and free["etherscan"]["key_present"] is False

    r = client.post("/api/settings/keys/etherscan", json={"key": "FREE-KEY-XYZ"})
    assert r.status_code == 200 and r.json()["key_present"] is True

    settings = client.get("/api/settings")
    assert "FREE-KEY-XYZ" not in settings.text  # the value is never returned
    free = {c["name"]: c for c in settings.json()["connectors"]["free"]}
    assert free["etherscan"]["key_present"] is True

    assert client.delete("/api/settings/keys/etherscan").json()["key_present"] is False
    free = {c["name"]: c for c in client.get("/api/settings").json()["connectors"]["free"]}
    assert free["etherscan"]["key_present"] is False


def test_non_keyable_free_pillar_rejects_a_key(client):
    """A free pillar that takes no key (Esplora) is rejected by the key endpoint (400)."""
    assert client.post("/api/settings/keys/esplora", json={"key": "x"}).status_code == 400


def test_chains_and_settings_expose_ingestable_evm_chains(client):
    chains = client.get("/api/chains").json()
    assert "ethereum" in chains["evm"] and chains["btc"] == ["bitcoin"]
    # /api/settings carries the same list so the add-address control needs no extra round-trip.
    assert "ethereum" in client.get("/api/settings").json()["evm_chains"]
