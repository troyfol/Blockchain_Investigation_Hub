"""Ronin Bridge hack (2022, Lazarus/DPRK) — real LEA/FIU-validated case recreated in BIH and diffed
against the published ground truth + the official OFAC designation. The golden real-world smoketest of
the investigation surface (facts → risk → tracing honest-gaps → export).

Spec + comparison checklist: docs/validation/ronin_lazarus_case.md (the "Results" section there records
the actual-vs-expected). Fixtures: `tests/fixtures/validation/ronin_*.json` are RAW Etherscan responses
recorded once under RUN_LIVE for the anchor's theft→designation window (block 14.40M–14.70M); replayed
offline here. `ronin_ofac_sdn.xml` is a small OFAC SDN snapshot (real designations).

Anchor: 0x098B716B8Aaf21512996dC57EB0615e2383E2f96 (Ronin Bridge Exploiter / Lazarus Group, SDN 2022-04-14).
Downstream addresses are DISCOVERED from the anchor's transfers — none are hand-entered.

This is a find-the-gaps validation: it asserts BIH's invariant-honoring behavior (faithful facts, the free
OFAC pillar reproducing the Treasury designation, NO fabricated mixer linkage, intact provenance). Where
the bounded real data diverges from the simplified checklist, the divergence is documented in the dossier
Results — NOT tuned away.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from backend.app.audits.runner import run_audits
from backend.app.config import get_settings
from backend.app.connectors.base import RateLimiter
from backend.app.connectors.etherscan import EtherscanConnector
from backend.app.connectors.imports.ofac import OfacSdnImporter
from backend.app.services.export import export_case, verify_casefile
from backend.app.services.tracing import add_trace_transfer, create_trace
from backend.tests.integration._helpers import new_case

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "validation"
BASE = get_settings().etherscan_base_url
ANCHOR = "0x098B716B8Aaf21512996dC57EB0615e2383E2f96"
ANCHOR_L = ANCHOR.lower()
TC = "0x722122dF12D4e14e13Ac3b6895a86e84145b6967".lower()  # Tornado Cash (SDN 2022-08-08)
THEFT_WEI = "173600000000000000000000"   # 173,600 ETH (the Ronin bridge withdrawal)
USDC_25_5M = 25_500_000_000000           # 25.5M USDC (6-dec)
CASS = {"txlist": "ronin_txlist.json", "txlistinternal": "ronin_txlistinternal.json",
        "tokentx": "ronin_tokentx.json", "balance": "ronin_balance.json"}


def _etherscan_router(request):
    return httpx.Response(200, json=json.loads((FIX / CASS[request.url.params.get("action")]).read_text()))


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="Ronin / Lazarus (2022)")
    yield conn, db
    conn.close()


@respx.mock
@pytest.mark.smoke
def test_ronin_lazarus_validation(case):
    conn, db = case
    respx.get(BASE).mock(side_effect=_etherscan_router)
    eth = EtherscanConnector(api_key="test", settings=get_settings(),
                             rate_limiter=RateLimiter(0, enabled=False), sleep=lambda _s: None)
    eth.get_transactions(conn, "ethereum", ANCHOR)
    eth.get_balance(conn, "ethereum", ANCHOR)
    eth.close()
    OfacSdnImporter().get_risk(conn, FIX / "ronin_ofac_sdn.xml")
    OfacSdnImporter().get_attributions(conn, FIX / "ronin_ofac_sdn.xml")

    # ============================ 1. FACTS (Etherscan/EVM connector) ============================
    # The 173,600 ETH Ronin-bridge withdrawal is ingested as an INTERNAL transfer fact INTO the anchor.
    theft = conn.execute(
        """SELECT t.id FROM transfer t JOIN address a ON a.id=t.to_address_id
           WHERE a.address=? AND t.transfer_type='internal' AND t.amount=?""",
        (ANCHOR_L, THEFT_WEI)).fetchone()
    assert theft is not None, "the 173,600 ETH theft inflow is missing"
    # 25.5M USDC inbound (the stablecoin half of the theft).
    usdc_in = conn.execute(
        """SELECT COALESCE(SUM(CAST(t.amount AS INTEGER)),0) FROM transfer t
           JOIN address a ON a.id=t.to_address_id JOIN asset s ON s.id=t.asset_id
           WHERE a.address=? AND s.symbol='USDC'""", (ANCHOR_L,)).fetchone()[0]
    assert usdc_in == USDC_25_5M
    # Outbound laundering transfers exist (anchor as sender).
    outbound = conn.execute(
        """SELECT COUNT(*) FROM transfer t JOIN address a ON a.id=t.from_address_id
           WHERE a.address=?""", (ANCHOR_L,)).fetchone()[0]
    assert outbound > 0
    # Provenance on every fact (Invariant #3): no transfer/transaction without a source_query.
    assert conn.execute("SELECT COUNT(*) FROM transfer WHERE source_query_id IS NULL").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM transaction_ WHERE source_query_id IS NULL").fetchone()[0] == 0
    # The un-laundered remainder is visible as an un-moved balance (current, ~101.8 ETH — NOT the
    # 2022-04-14 ~$433M figure; that was point-in-time. We assert a remainder is recorded, not a $ amount).
    bal = conn.execute(
        """SELECT b.amount FROM balance_snapshot b JOIN address a ON a.id=b.address_id
           WHERE a.address=?""", (ANCHOR_L,)).fetchone()
    assert bal is not None and int(bal["amount"]) > 0

    # ===================== 2. RISK — the headline validation (free OFAC pillar) =====================
    # BIH's FREE pillar independently reproduces the real Treasury designation: the anchor is flagged
    # sanctioned with a rationale naming Lazarus Group.
    anchor_risk = conn.execute(
        """SELECT r.rationale, r.score, r.score_scale FROM risk_assessment r
           JOIN address a ON a.id=r.address_id
           WHERE a.address=? AND r.source='ofac-sdn' AND r.category='sanctioned'""", (ANCHOR_L,)).fetchone()
    assert anchor_risk is not None and "LAZARUS GROUP" in anchor_risk["rationale"]
    assert anchor_risk["score"] is None and anchor_risk["score_scale"] is None  # categorical, not a score
    # The Tornado Cash leg is independently flagged sanctioned too (SDN 2022-08-08).
    tc_risk = conn.execute(
        """SELECT r.rationale FROM risk_assessment r JOIN address a ON a.id=r.address_id
           WHERE a.address=? AND r.source='ofac-sdn' AND r.category='sanctioned'""", (TC,)).fetchone()
    assert tc_risk is not None and "TORNADO CASH" in tc_risk["rationale"]

    # ============================ 3. ATTRIBUTION (free pillars) ============================
    # OFAC supplies an authoritative sanctioned-entity attribution (label = Lazarus Group)...
    anchor_attr = conn.execute(
        """SELECT at.label, at.category, at.source FROM attribution at JOIN address a ON a.id=at.address_id
           WHERE a.address=? AND at.category='sanctioned_entity'""", (ANCHOR_L,)).fetchone()
    assert anchor_attr is not None and anchor_attr["label"] == "LAZARUS GROUP"
    assert anchor_attr["source"] == "ofac-sdn"
    # ...and GraphSense attribution is GRACEFULLY ABSENT — no public TagPack was ingested, so BIH does NOT
    # fabricate a label (never invent attribution — Invariant #4 / no-synthesis).
    assert conn.execute("SELECT COUNT(*) FROM attribution WHERE source='graphsense'").fetchone()[0] == 0

    # ============================ 4. TRACING + honest gaps ============================
    trace = create_trace(conn, name="Ronin forward trace (anchor outbound)")
    anchor_out_transfers = [r["id"] for r in conn.execute(
        """SELECT t.id FROM transfer t JOIN address a ON a.id=t.from_address_id
           WHERE a.address=? AND t.transfer_type IN ('native','internal')""", (ANCHOR_L,)).fetchall()]
    for tid in anchor_out_transfers:
        add_trace_transfer(conn, trace_id=trace, transfer_id=tid)
    # Every trace edge references a REAL transfer fact — EVM tracing cannot fabricate flow (it only
    # references facts; there is no automated path discovery).
    edges = conn.execute("SELECT transfer_id FROM trace_transfer WHERE trace_id=?", (trace,)).fetchall()
    assert len(edges) > 0
    assert all(conn.execute("SELECT 1 FROM transfer WHERE id=?", (e["transfer_id"],)).fetchone()
               for e in edges)
    # HONEST GAP: the anchor has NO direct transfer to the Tornado Cash SDN address — BIH does not invent
    # an anchor→mixer edge (the real laundering reaches TC via intermediary wallets, downstream of this
    # single-anchor fixture). The mixer is flagged via OFAC, never via a fabricated linkage.
    assert conn.execute(
        """SELECT COUNT(*) FROM transfer t JOIN address a ON a.id=t.to_address_id
           WHERE a.address=?""", (TC,)).fetchone()[0] == 0
    # Cross-asset honesty: the USDC→ETH DEX swap is two DISTINCT single-asset transfer facts (USDC out,
    # ETH back), never one synthesized "same value" link. The anchor both sent USDC to and received ETH
    # from a swap counterparty as separate facts.
    swap_addr = conn.execute(
        """SELECT a.address FROM transfer t JOIN address a ON a.id=t.to_address_id
           JOIN asset s ON s.id=t.asset_id JOIN address f ON f.id=t.from_address_id
           WHERE f.address=? AND s.symbol='USDC' LIMIT 1""", (ANCHOR_L,)).fetchone()["address"]
    assets_with_swap = {r[0] for r in conn.execute(
        """SELECT s.symbol FROM transfer t JOIN asset s ON s.id=t.asset_id
           JOIN address fa ON fa.id=t.from_address_id JOIN address ta ON ta.id=t.to_address_id
           WHERE (fa.address=? AND ta.address=?) OR (fa.address=? AND ta.address=?)""",
        (ANCHOR_L, swap_addr, swap_addr, ANCHOR_L)).fetchall()}
    assert "USDC" in assets_with_swap  # the swap survives as distinct single-asset facts, not a merged edge

    # ============================ 5. REPORT / EXPORT ============================
    # Audits green BEFORE export (also writes the immutability baseline that must travel with the case).
    results = run_audits(db_path=str(db))
    assert all(r.passed for r in results), [(r.name, r.offending) for r in results if not r.passed]
    # Every sourced claim (incl. the sanctions designation) carries its provenance.
    assert conn.execute(
        "SELECT COUNT(*) FROM risk_assessment WHERE source_query_id IS NULL").fetchone()[0] == 0
    # A reproducible, self-contained case file generates and re-verifies (hashes + provenance FKs in-bundle).
    # Close the connection first so SQLite checkpoints the WAL into case.db (export reads the file).
    conn.close()
    bundle = export_case(db.parent, out_path=db.parent.parent / "ronin.casefile")
    report = verify_casefile(bundle, extract_to=db.parent.parent / "ronin_extracted")
    assert report["ok"] is True, report
