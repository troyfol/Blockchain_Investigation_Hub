"""CoinJoin detection — real Samourai Whirlpool coinjoin recreated in BIH: validate the CoinJoin
detection algorithm on a REAL mixing tx and confirm the trace treats the mix as an honest deconfusion
boundary (the BTC analogue of the Tornado Cash gap) — never fabricating a 1:1 input->output through-link.
Bitfinex tested the NEGATIVE (its consolidation was "not CoinJoin-flagged"); this drives the POSITIVE
detection path.

Spec + comparison checklist: docs/validation/coinjoin_detection_case.md (the "Results" section records
the actual-vs-expected). Fixtures (`tests/fixtures/validation/coinjoin_*`): RAW Blockstream Esplora
responses recorded once (keyless public API), replayed offline.

ANCHOR (confirmed STRUCTURALLY via Esplora — NOT guessed; STEP 0):
  - CoinJoin `323df21f…` — a real Samourai Whirlpool 0.05 BTC pool tx: **5 inputs / 5 outputs all exactly
    5,000,000 sat**, 5 distinct input addresses. Satisfies `is_probable_coinjoin` via BOTH the structural
    test (>=5 inputs AND >=5 equal outputs) and the Whirlpool-denomination test (>=5 outputs at 0.05 BTC).
  - Tx0 `333f4543…` — the Whirlpool Tx0 funding tx: 1 input / 19 outputs (off-denom 5,010,000 premix). A
    real ORDINARY tx (negative control: `is_probable_coinjoin` False — 1 input, outputs not a pool denom)
    AND a direct parent of the CoinJoin (a CJ input spends Tx0:8), giving FIFO ancestry to trace INTO the
    mix. Source of the txids (econoalchemist Whirlpool walkthrough) was used only as a candidate; the
    structure was VERIFIED on-chain before recording.

Find-the-gaps, not pass-the-test: BIH's deconfusion boundary is FLAG-BASED (the `possible-coinjoin`
membership flag + the `is_convention`/`confidence=None` FIFO labeling), not an automatic trace halt — the
FIFO convention does not itself stop at the mix (it apportions across it as a labeled non-fact, here fanning
one input across two outputs and reporting the rest unresolved, never guessed). That divergence is
documented in the dossier Results, never tuned away.
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
from backend.app.services.entities import cluster_cospend, is_probable_coinjoin
from backend.app.services.export import export_case, verify_casefile
from backend.app.services.tracing import create_trace, fifo_trace_transaction, trace_btc_links
from backend.tests.integration._helpers import new_case

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "validation"
TIP = (FIX / "coinjoin_tip.txt").read_text().strip()
CJ_TXID = "323df21f0b0756f98336437aa3d2fb87e02b59f1946b714a7b09df04d429dec2"    # Whirlpool 0.05 pool
TX0_TXID = "333f45431e47b9543772013ac83a9b33cc58dc3245ccfd48b972107bb8405c13"   # Tx0 (parent + control)
POOL_DENOM_SAT = "5000000"          # 0.05 BTC — each of the 5 equal CoinJoin outputs
TXS = {CJ_TXID: "tx", TX0_TXID: "tx0"}


def _esplora_router(request):
    p = request.url.path
    if p.endswith("/blocks/tip/height"):
        return httpx.Response(200, text=TIP)
    for txid, name in TXS.items():
        if p.endswith(f"/tx/{txid}"):
            return httpx.Response(200, json=json.loads((FIX / f"coinjoin_{name}.json").read_text()))
    return httpx.Response(404)


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="CoinJoin detection (Whirlpool)")
    yield conn, db
    conn.close()


@respx.mock
@pytest.mark.smoke
def test_coinjoin_detection_validation(case):
    conn, db = case
    respx.route(host="blockstream.info").mock(side_effect=_esplora_router)
    btc = EsploraConnector(settings=get_settings(), rate_limiter=RateLimiter(0, enabled=False),
                           sleep=lambda _s: None)
    # Ingest Tx0 FIRST so the CoinJoin input that spends Tx0:8 resolves its prev_output (FIFO ancestry
    # into the mix); then the CoinJoin itself.
    btc.get_transfers(conn, "bitcoin", TX0_TXID)
    btc.get_transfers(conn, "bitcoin", CJ_TXID)
    btc.close()

    cj_id = conn.execute("SELECT id FROM transaction_ WHERE tx_hash=?", (CJ_TXID,)).fetchone()["id"]
    tx0_id = conn.execute("SELECT id FROM transaction_ WHERE tx_hash=?", (TX0_TXID,)).fetchone()["id"]

    # ===================== 1. FACTS (Esplora/UTXO) — inputs/outputs, never a transfer =====================
    assert conn.execute("SELECT COUNT(*) FROM transfer").fetchone()[0] == 0          # Invariant #5
    # The 5 equal 0.05 BTC CoinJoin outputs ingest as tx_output facts.
    assert conn.execute(
        "SELECT COUNT(*) FROM tx_output WHERE transaction_id=? AND amount=?",
        (cj_id, POOL_DENOM_SAT)).fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM tx_input WHERE transaction_id=?", (cj_id,)).fetchone()[0] == 5
    # Provenance on every fact (Inv #3).
    for tbl in ("transaction_", "tx_input", "tx_output"):
        assert conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE source_query_id IS NULL").fetchone()[0] == 0
    # Invariant #5 view hook: every UTXO movement has NULL src (no synthesized input->output edge as a fact).
    utxo = conn.execute("SELECT src_address_id FROM v_value_movement WHERE paradigm='utxo'").fetchall()
    assert len(utxo) > 0 and all(r["src_address_id"] is None for r in utxo)

    # ===================== 2. COINJOIN DETECTION — the headline =====================
    # BIH flags the real Whirlpool tx as a CoinJoin from its structural signature; the ordinary Tx0 is not.
    assert is_probable_coinjoin(conn, cj_id) is True            # 5 inputs + 5 equal 0.05-BTC outputs
    assert is_probable_coinjoin(conn, tx0_id) is False          # NEGATIVE CONTROL: 1 input, off-denom outs
    # Co-spend clustering materializes the flag: the CoinJoin's 5 input addresses become a cluster whose
    # memberships carry flags='possible-coinjoin' + reduced confidence 0.5 (co-spend over a CoinJoin is
    # NOT trustworthy — the flag says so). docs/algorithms.md §5.
    stats = cluster_cospend(conn)
    assert stats["clusters"] == 1 and stats["memberships_created"] == 5
    flagged = conn.execute(
        """SELECT address_id, confidence FROM entity_membership
           WHERE method='co-spend' AND flags='possible-coinjoin'""").fetchall()
    assert len(flagged) == 5 and all(r["confidence"] == 0.5 for r in flagged)
    cj_input_addrs = {r["address_id"] for r in conn.execute(
        "SELECT DISTINCT address_id FROM tx_input WHERE transaction_id=?", (cj_id,)).fetchall()}
    assert {r["address_id"] for r in flagged} == cj_input_addrs  # exactly the mix participants, flagged
    # Negative control at the cluster level: the ordinary Tx0's input address is NOT in any flagged
    # membership (it is a single-input funding tx — it never even forms a co-spend cluster).
    tx0_input_addrs = {r["address_id"] for r in conn.execute(
        "SELECT DISTINCT address_id FROM tx_input WHERE transaction_id=?", (tx0_id,)).fetchall()}
    assert not (tx0_input_addrs & {r["address_id"] for r in flagged})

    # ===================== 3. TRACING — the CoinJoin is an honest deconfusion BOUNDARY =====================
    # The detection signal is the boundary marker: an honest trace consults is_probable_coinjoin and treats
    # the mix as a deconfusion boundary. We FIFO-trace INTO the mix to prove BIH never asserts a
    # deterministic 1:1 through-link.
    assert is_probable_coinjoin(conn, cj_id) is True            # the boundary is detectable
    trace = create_trace(conn, name="CoinJoin trace — deconfusion boundary")
    res = fifo_trace_transaction(conn, trace_id=trace, transaction_id=cj_id)
    # Only the input funded by the in-DB Tx0 output resolves; the other 4 inputs' ancestry is NOT in-DB and
    # is reported UNRESOLVED rather than guessed/fabricated (honest gap — never invents a source).
    assert res["links_written"] >= 1 and res["unresolved"] >= 1
    links = trace_btc_links(conn, trace)
    # EVERY link across the mix is an explicitly LABELED CONVENTION — basis='fifo', is_convention=True,
    # confidence=None. BIH does NOT assert a deterministic/confident through-link (that would be the failure
    # to catch). None is an investigator-asserted deterministic link either.
    assert links and all(
        ln["basis"] == "fifo" and ln["is_convention"] is True and ln["confidence"] is None for ln in links)
    assert not any(ln["basis"] == "investigator" for ln in links)
    # Not a 1:1 recovery: the single resolved input FANS across more than one output (here 2 links from one
    # input — 5,000,000 + 10,000 sat), so even the FIFO convention is visibly not a deterministic 1:1 map.
    src_outputs = {ln["source_output_id"] for ln in links}
    assert len(src_outputs) == 1 and len(links) > len(src_outputs)
    # And no through-link exists as a FACT (Inv #5): the mix is never collapsed into a transfer.
    assert conn.execute("SELECT COUNT(*) FROM transfer").fetchone()[0] == 0

    # ===================== 4. ATTRIBUTION — coordinator gracefully absent (no fabrication) =====================
    # No public GraphSense TagPack was ingested for the Whirlpool/Samourai coordinator addresses, so
    # attribution is honestly ABSENT — BIH invents no label (Inv #4 / no-synthesis).
    assert conn.execute("SELECT COUNT(*) FROM attribution").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM risk_assessment").fetchone()[0] == 0

    # ===================== 5. REPORT / EXPORT =====================
    results = run_audits(db_path=str(db))
    assert all(r.passed for r in results), [(r.name, r.offending) for r in results if not r.passed]
    assert any(r.name == "no-fabricated-utxo-edge" and r.passed for r in results)     # Invariant #5
    assert any(r.name == "entity-resolution-sanity" and r.passed for r in results)    # the cluster is sane
    conn.close()
    bundle = export_case(db.parent, out_path=db.parent.parent / "coinjoin.casefile")
    report = verify_casefile(bundle, extract_to=db.parent.parent / "coinjoin_extracted")
    assert report["ok"] is True, report
