"""P8.7.1 regression tests — the 6 vitalik-review fixes (report-view #2, PDF robustness #3, valuation #4,
entities table #5). Issue #1 (intel false-positive) lives in test_intel.py; #6 (risk halo z-lift) in the
frontend theme tests + test_report_twin below."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from backend.app.db import repository as repo
from backend.app.models import Address, Asset, SourceQuery, Transaction, Transfer, Valuation
from backend.app.provenance.atomic import write_with_provenance
from backend.tests.integration._helpers import new_case

FOCUS = "0x" + "f" * 40


def _seed_dense(conn):
    """A focus with 3 tiny UNPRICED dust counterparties + 1 large PRICED counterparty (a flagged-by-value
    significant node) so build_view folds the dust and the report can prove it rendered the bounded view."""
    sq = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="txlist",
                     params={"address": "focus", "bounds": "default"}, requested_at="2026-01-01T00:00:00Z",
                     status="ok")
    ids = {"focus": None, "big": None, "dust": []}

    def write(c, sqid):
        eth = repo.upsert_asset(c, Asset(chain="ethereum", symbol="ETH", decimals=18), sqid)
        focus = repo.upsert_address(c, Address(chain="ethereum", address_display=FOCUS), sqid)
        ids["focus"] = focus
        big_cp = repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + "a" * 40), sqid)
        tx = repo.upsert_transaction(c, Transaction(chain="ethereum", tx_hash="0x" + "1" * 64,
            block_height=100, block_ts="2026-01-01T00:00:00Z", confirmations=100, finality_status="final"), sqid)
        big_tr = repo.upsert_transfer(c, Transfer(transaction_id=tx, chain="ethereum", from_address_id=big_cp,
            to_address_id=focus, asset_id=eth, amount=str(5 * 10**18), transfer_type="native", position=0), sqid)
        ids["big"] = {"cp": big_cp, "tr": big_tr}
        for i in range(3):
            cp = repo.upsert_address(c, Address(chain="ethereum", address_display="0x" + format(i + 2, "040x")), sqid)
            tx2 = repo.upsert_transaction(c, Transaction(chain="ethereum", tx_hash="0x" + format(i + 2, "064x"),
                block_height=100 + i, block_ts="2026-01-01T00:00:00Z", confirmations=100, finality_status="final"), sqid)
            repo.upsert_transfer(c, Transfer(transaction_id=tx2, chain="ethereum", from_address_id=cp,
                to_address_id=focus, asset_id=eth, amount=str(10**12), transfer_type="native", position=0), sqid)  # tiny dust
            ids["dust"].append(cp)

    write_with_provenance(conn, sq, write)
    sq2 = SourceQuery(connector="defillama", capability="get_price", endpoint="p", params={},
                      requested_at="2026-01-01T00:00:00Z", status="ok")

    def write2(c, sqid):
        repo.insert_valuation(c, Valuation(subject_type="transfer", subject_id=ids["big"]["tr"], currency="USD",
            unit_price="5000", value="5000", price_timestamp="2026-01-01T00:00:00Z", confidence=0.9,
            source="defillama", retrieved_at="2026-01-01T00:00:00Z"), sqid)

    write_with_provenance(conn, sq2, write2)
    return ids


# --------------------------------------------------------------------------- #2 report renders the view

def test_report_renders_current_view_with_honest_scope_spec(tmp_path):
    from backend.app.services.reporting import generate_report

    conn, _db = new_case(tmp_path, title="Dense")
    ids = _seed_dense(conn)
    case_dir = tmp_path / "case_dir"
    fnid = f"addr:{ids['focus']}"
    res = generate_report(conn, case_dir=case_dir, title="Bounded", render_pdf=False,
                          view_params={"focus": fnid, "hops": 1, "group_dust": True})
    html = Path(res["html_path"]).read_text(encoding="utf-8")
    # the bounded view rendered: the big counterparty is present, the dust is folded (not individually shown)
    assert ("a" * 40)[:8] in html                      # big counterparty alias shows
    scope = json.loads(conn.execute("SELECT scope_spec FROM report WHERE id=?", (res["report_id"],)).fetchone()[0])
    assert scope["selection"] == "current-view"
    assert scope["hidden"]["dust_folded"] >= 3         # the 3 dust counterparties were folded (honest count)
    assert scope["displayed"] < scope["total"]          # bounded: shows fewer than the whole case

    # a control with NO view_params still renders the full case + the 'full-case' marker (back-compat)
    full = generate_report(conn, case_dir=case_dir, title="Full", render_pdf=False)
    fscope = json.loads(conn.execute("SELECT scope_spec FROM report WHERE id=?", (full["report_id"],)).fetchone()[0])
    assert fscope["selection"] == "full-case"


# --------------------------------------------------------------------------- #5 entities table

def test_report_entities_table_dedups_by_address_and_uses_display_name(tmp_path):
    from backend.app.connectors.imports.graphsense import GraphSenseImporter
    from backend.app.services import reporting
    from backend.tests.integration._helpers import seed_btc_custom

    conn, _db = new_case(tmp_path, title="Hydra")
    h1, h2 = "16ZSAEfYpPCj3D94fsNt2okYj9Ue8mxy6T", "1MQBDeRWsiJBf7K1VGjJ7PWEL6GJXMfmLg"
    seed_btc_custom(conn, txid="a" * 64, input_addrs=[h1, h2], output_amounts=[10_000])
    # TagPack-only ingest (no ActorPack) -> the entity name is the slug 'hydramarket'. Use the Hydra-only
    # TEST fixture (P8.7.3 keeps the app's now-multi-entity BUNDLED intel separate from test fixtures).
    tagpack = Path(__file__).resolve().parent.parent / "fixtures" / "validation" / "hydra_tagpack.yaml"
    gs = GraphSenseImporter()
    gs.get_attributions(conn, str(tagpack))   # writes attribution.label='Hydra Market'
    gs.get_entities(conn, str(tagpack))        # entity name = slug 'hydramarket' + 2 memberships

    ents = reporting._collect_entities(conn)
    assert len(ents) == 1
    # #5a — display name falls back to the attribution label, NOT the raw slug
    assert ents[0]["name"] == "Hydra Market"
    assert ents[0]["external_id"] == "hydramarket"
    # #5b / P8.8.1 — the cluster renders as a SUMMARY (count + both member addresses in a compact list),
    # not one repeated row per member; the two distinct addresses are both present.
    section = reporting._entities_section(ents)
    assert "2 addresses" in section            # the summary states the member count once
    assert h1[:8] in section and h2[:8] in section  # both distinct members listed (compact)
    assert "<th>method</th>" not in section     # no per-member method table (P8.8.1 summarization)


# --------------------------------------------------------------------------- #4 valuation trigger

@respx.mock
def test_valuation_runs_as_a_background_job_with_progress_and_offline_skips(tmp_path, monkeypatch):
    """P8.7.2 — /api/valuation/run starts a BACKGROUND job (returns immediately); valuation runs as a
    separate pass that fills USD in; offline returns 409 with no network."""
    import time

    from backend.app.main import app, get_case_db_path
    from backend.app.services import jobs, settings_store
    from backend.tests.integration.test_seeded_case import seed_evm_transfer

    monkeypatch.setenv("BIH_APP_DATA_DIR", str(tmp_path / "appdata"))
    jobs.clear()
    conn, db = new_case(tmp_path, title="Value Me")
    seed_evm_transfer(conn)
    cass = Path(__file__).resolve().parent.parent / "cassettes" / "defillama"
    eth_price = json.loads((cass / "eth_price.json").read_text())
    respx.route(host="coins.llama.fi").mock(
        side_effect=lambda r: httpx.Response(200, json=eth_price if "coingecko:ethereum" in r.url.path
                                             else {"coins": {}}))
    app.dependency_overrides[get_case_db_path] = lambda: str(db)
    try:
        client = TestClient(app)
        assert conn.execute("SELECT COUNT(*) FROM valuation").fetchone()[0] == 0
        r = client.post("/api/valuation/run")
        assert r.status_code == 200 and r.json()["started"] is True   # non-blocking — returns immediately

        # the background pass fills USD in; poll the job until it completes (bounded)
        for _ in range(200):
            j = client.get("/api/jobs/active").json()["job"]
            if j and j["state"] != "running":
                break
            time.sleep(0.02)
        assert j["state"] == "done" and j["valued"] >= 1           # progress reported, valued > 0
        assert conn.execute("SELECT COUNT(*) FROM valuation").fetchone()[0] >= 1   # USD now populated

        # offline -> 409, no background job started
        settings_store.set_offline(True)
        assert client.post("/api/valuation/run").status_code == 409
        settings_store.set_offline(False)
    finally:
        app.dependency_overrides.clear()
        jobs.clear()
        conn.close()


# --------------------------------------------------------------------------- #3 PDF render robustness

def test_render_engine_retries_then_raises_dense_error_on_exit0_no_pdf(tmp_path, monkeypatch):
    import sys

    from backend.app.services import report_render

    html = tmp_path / "r.html"
    html.write_text("<html><body>x</body></html>", encoding="utf-8")
    pdf = tmp_path / "r.pdf"

    calls = {"n": 0}

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        calls["n"] += 1
        return _Proc()  # exit 0, writes NO pdf -> the exit-0-but-no-PDF dense case

    monkeypatch.setattr(report_render.subprocess, "run", fake_run)

    # direct: exit-0-no-PDF -> a DISTINCT DenseRenderError (not the generic no-engine NoRendererError)
    with pytest.raises(report_render.DenseRenderError):
        report_render._render_with_engine("fake-engine", html, pdf, budget_ms=12000)

    # render_pdf: force an engine, then assert it RETRIES (2 subprocess calls) before raising DenseRenderError
    calls["n"] = 0
    monkeypatch.setenv("BIH_REPORT_RENDERER", sys.executable)  # an existing path -> find_engine returns it
    with pytest.raises(report_render.DenseRenderError):
        report_render.render_pdf(html, pdf)
    assert calls["n"] == 2     # tried, then retried with a bigger budget


def test_report_html_has_cy_ready_timeout_fallback():
    """The report's inlined JS must arm a hard-timeout fallback BEFORE the blocking cose run + set ready
    in a finally, so a dense graph still prints something (P8.7.1 #3)."""
    from backend.app.services.reporting import render_html

    ctx = {"title": "t", "case": {"title": "c"}, "generated_at": "2026-01-01T00:00:00Z",
           "scope_spec": {"selection": "full-case"}, "graph": {"nodes": [], "edges": []},
           "traces": [], "findings": [], "notes": [], "risk": [], "entities": [],
           "valuation": {"movements": 0, "valued": 0, "missing": 0, "multi_source": 0}}
    page = render_html(ctx)
    assert "setTimeout(done, 8000)" in page            # hard-timeout fallback armed
    assert "finally { done(); }" in page                # ready set after the synchronous run, always


# --------------------------------------------------------------------------- #6 report twin z-lift

def test_report_twin_lifts_risk_node_above_group():
    """The report's Python cytoscape twin must carry the same z-compound-depth lift so a sanctioned node
    keeps its halo inside a denomination group in the PDF (lockstep with theme.ts)."""
    from backend.app.theme import cytoscape_style

    style = cytoscape_style()
    san = next(r for r in style if r["selector"] == 'node[risk_level="sanctioned"]')
    assert san["style"].get("z-compound-depth") == "top"
    attr = next(r for r in style if r["selector"] == "node[?has_attribution]")
    assert attr["style"].get("z-compound-depth") == "top"
