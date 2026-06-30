"""Bitfinex 2016 hack → 2022 DOJ seizure — real case recreated in BIH, the heavy-UTXO BTC validation.
Where Colonial Pipeline is a clean short flow, this is the **co-spend clustering at scale** stress test
(the capability Colonial doesn't exercise) plus FIFO tracing, over the famous DOJ "Wallet 1CGA4s" cluster.

Spec + comparison checklist: docs/validation/bitfinex_2016_case.md (the "Results" section there records
the actual-vs-expected). Fixtures: `tests/fixtures/validation/bitfinex_*` are RAW Blockstream Esplora
responses recorded once (keyless public API), replayed offline.

ANCHOR (confirmed empirically, NOT guessed — STEP 0): 1CGA4srJbPWhtJb7ezgY6GQf4PKhFuzD9w. The DOJ Statement
of Facts shorthand "Wallet 1CGA4s"; verified on-chain: prefix 1CGA4s, received 567.48 BTC in one Aug-2016
theft tx (block 423297), dormant, then a co-spent input of the Feb-2022 seizure consolidation tx
`c49ff6bd` (block 721287) that swept the cluster into the government wallet bc1qazcm…. (Find-the-gap: the
"~94,636 BTC single wallet" of the affidavit is the CLUSTER total across ~2,000 co-spent addresses, not one
address — see the dossier Results.)

Bounded scope (per spec — "scope tightly"): the anchor's Aug-2016 theft tx (consolidation INTO it) + the
Feb-2022 seizure consolidation tx (166 distinct co-spent addresses), ingested by txid — NOT the dust-heavy
full history. Find-the-gaps: divergences are documented in the dossier, never tuned away.
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
from backend.app.connectors.esplora import EsploraConnector
from backend.app.services.entities import cluster_cospend
from backend.app.services.export import export_case, verify_casefile
from backend.app.services.tracing import create_trace, fifo_trace_transaction, trace_btc_links
from backend.tests.integration._helpers import new_case

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "validation"
TIP = (FIX / "bitfinex_tip.txt").read_text().strip()
ANCHOR = "1CGA4srJbPWhtJb7ezgY6GQf4PKhFuzD9w"           # DOJ "Wallet 1CGA4s" (confirmed)
THEFT_TXID = "5c7134d4dd030402a9d1315e2f34f46a7e6ace6bbfb572974a83359e3b8fa700"    # Aug 2016, → anchor
SEIZURE_TXID = "c49ff6bd054fb386cd02fc94ca34b8773229ed8a5538e023ef7bea772d70c17a"  # Feb 2022 consolidation
CONSOLIDATION = "bc1qazcm763858nkj2dj986etajv6wquslv8uxwczt"  # the government seizure wallet
ANCHOR_THEFT_SAT = "56748055857"     # 567.48055857 BTC into the anchor (the theft)
CONSOLIDATION_SAT = "1500000000000"  # 15,000 BTC seizure-consolidation output to bc1qazcm
TXS = {THEFT_TXID: "theft", SEIZURE_TXID: "seizure"}


def _esplora_router(request):
    p = request.url.path
    if p.endswith("/blocks/tip/height"):
        return httpx.Response(200, text=TIP)
    for txid, name in TXS.items():
        if p.endswith(f"/tx/{txid}"):
            return httpx.Response(200, json=json.loads((FIX / f"bitfinex_{name}_tx.json").read_text()))
    if p.endswith(f"/address/{ANCHOR}"):
        return httpx.Response(200, json=json.loads((FIX / "bitfinex_anchor_stats.json").read_text()))
    return httpx.Response(404)


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="Bitfinex 2016 hack → 2022 seizure")
    yield conn, db
    conn.close()


@respx.mock
@pytest.mark.smoke
def test_bitfinex_2016_validation(case):
    conn, db = case
    respx.route(host="blockstream.info").mock(side_effect=_esplora_router)
    btc = EsploraConnector(settings=get_settings(), rate_limiter=RateLimiter(0, enabled=False),
                           sleep=lambda _s: None)
    # Ingest the theft tx FIRST (writes the anchor's funding output) so the seizure tx's input that spends
    # it resolves its prev_output (lets the FIFO hop anchor to a real fact).
    btc.get_transfers(conn, "bitcoin", THEFT_TXID)
    btc.get_transfers(conn, "bitcoin", SEIZURE_TXID)
    btc.get_balance(conn, "bitcoin", ANCHOR)
    btc.close()

    # ===================== 1. FACTS (Esplora/UTXO) — inputs/outputs, never a transfer =====================
    assert conn.execute("SELECT COUNT(*) FROM transfer").fetchone()[0] == 0  # Invariant #5
    # The Aug-2016 theft consolidated 567.48 BTC INTO the anchor (a tx_output fact).
    anchor_out = conn.execute(
        """SELECT o.id FROM tx_output o JOIN address a ON a.id=o.address_id
           WHERE a.address=? AND o.amount=?""", (ANCHOR, ANCHOR_THEFT_SAT)).fetchone()
    assert anchor_out is not None
    # The Feb-2022 seizure consolidation output (15,000 BTC to the government wallet).
    assert conn.execute(
        """SELECT COUNT(*) FROM tx_output o JOIN address a ON a.id=o.address_id
           WHERE a.address=? AND o.amount=?""", (CONSOLIDATION, CONSOLIDATION_SAT)).fetchone()[0] == 1
    # Provenance on every fact (Inv #3).
    for tbl in ("transaction_", "tx_input", "tx_output"):
        assert conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE source_query_id IS NULL").fetchone()[0] == 0
    # Find-the-gap: the anchor's "large held balance" is now ZERO — its 567 BTC was swept in the 2022
    # seizure (the ~94,636 BTC is the CLUSTER total, now in government custody at bc1qazcm). BIH shows live
    # state; we assert the actual swept balance and document the point-in-time divergence (Results).
    bal = conn.execute(
        """SELECT b.amount FROM balance_snapshot b JOIN address a ON a.id=b.address_id
           WHERE a.address=?""", (ANCHOR,)).fetchone()
    assert bal is not None and bal["amount"] == "0"

    # ===================== 2. INVARIANT #5 — no fabricated input→output edge =====================
    utxo = conn.execute("SELECT src_address_id FROM v_value_movement WHERE paradigm='utxo'").fetchall()
    assert len(utxo) > 0 and all(r["src_address_id"] is None for r in utxo)

    # ===================== 3. CO-SPEND CLUSTERING — the headline Bitfinex validation =====================
    # The seizure consolidation co-spends 166 distinct theft-cluster addresses (incl. the anchor) in one tx
    # → the co-spend heuristic resolves them to ONE entity. This is the BTC capability Colonial doesn't
    # stress (Colonial's txs had ≤3 inputs).
    stats = cluster_cospend(conn)
    assert stats["clusters"] >= 1 and stats["entities_created"] >= 1
    anchor_id = conn.execute("SELECT id FROM address WHERE address=?", (ANCHOR,)).fetchone()["id"]
    mem = conn.execute(
        """SELECT entity_id FROM entity_membership
           WHERE address_id=? AND source='cospend-heuristic' AND method='co-spend'""",
        (anchor_id,)).fetchone()
    assert mem is not None  # the anchor resolved into a co-spend cluster
    entity_id = mem["entity_id"]
    assert conn.execute("SELECT origin FROM entity WHERE id=?", (entity_id,)).fetchone()["origin"] \
        == "cospend-cluster"  # a DERIVED cluster, not a fabricated identity
    cluster_size = conn.execute(
        """SELECT COUNT(DISTINCT address_id) FROM entity_membership
           WHERE entity_id=? AND method='co-spend'""", (entity_id,)).fetchone()[0]
    assert cluster_size >= 100  # at scale: the ~166-address Bitfinex-hack consolidation cluster
    # Every co-spend membership carries the clustering run's provenance (Inv #3) — a derived claim, sourced.
    assert conn.execute(
        """SELECT COUNT(*) FROM entity_membership
           WHERE source='cospend-heuristic' AND source_query_id IS NULL""").fetchone()[0] == 0

    # ===================== 4. TRACING (FIFO) — the hop lives ONLY in a trace =====================
    # The anchor's theft output is spent by the seizure tx; FIFO-apportion that tx → the anchor's 567 BTC
    # is linked into the consolidation as a basis='fifo' CLAIM (never a transfer fact — Inv #5).
    seizure_tx = conn.execute("SELECT id FROM transaction_ WHERE tx_hash=?", (SEIZURE_TXID,)).fetchone()["id"]
    trace = create_trace(conn, name="Bitfinex seizure — FIFO hop from the anchor")
    res = fifo_trace_transaction(conn, trace_id=trace, transaction_id=seizure_tx)
    assert res["links_written"] >= 1  # the resolved anchor input produces a fifo link
    links = trace_btc_links(conn, trace)
    assert links and all(link["basis"] == "fifo" and link["is_convention"] for link in links)
    fifo_from_anchor = conn.execute(
        """SELECT id FROM trace_btc_link WHERE trace_id=? AND source_output_id=? AND basis='fifo'""",
        (trace, anchor_out["id"])).fetchone()
    assert fifo_from_anchor is not None  # the anchor→consolidation hop exists ONLY as a fifo trace claim

    # ===================== 5. ATTRIBUTION — gracefully absent (no fabrication) =====================
    # No public GraphSense TagPack was ingested for these addresses, so AlphaBay/Hydra/"Bitfinex Hack"
    # attribution is honestly ABSENT — BIH invents no label (Inv #4 / no-synthesis).
    assert conn.execute("SELECT COUNT(*) FROM attribution").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM risk_assessment").fetchone()[0] == 0

    # ===================== 6. REPORT / EXPORT =====================
    results = run_audits(db_path=str(db))
    assert all(r.passed for r in results), [(r.name, r.offending) for r in results if not r.passed]
    assert any(r.name == "no-fabricated-utxo-edge" and r.passed for r in results)      # Invariant #5
    assert any(r.name == "entity-resolution-sanity" and r.passed for r in results)     # the cluster is sane
    conn.close()
    bundle = export_case(db.parent, out_path=db.parent.parent / "bitfinex.casefile")
    report = verify_casefile(bundle, extract_to=db.parent.parent / "bitfinex_extracted")
    assert report["ok"] is True, report
