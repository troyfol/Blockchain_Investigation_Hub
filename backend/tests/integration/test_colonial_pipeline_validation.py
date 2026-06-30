"""Colonial Pipeline / DarkSide ransom (2021) — real DOJ/FBI-validated BTC case recreated in BIH and
diffed against the published seizure ground truth. The golden real-world smoketest of the Bitcoin/UTXO
path and the clean-room test of Invariant #5 (Bitcoin stores tx_input/tx_output ONLY; an input→output
"transfer" is never a fact — that linkage exists only inside a trace as a basis='fifo' claim).

Spec + comparison checklist: docs/validation/colonial_pipeline_case.md (the "Results" section there records
the actual-vs-expected). Fixtures: `tests/fixtures/validation/colonial_*` are RAW Blockstream Esplora
responses recorded once (Esplora is a keyless public API) for the three DOJ-named addresses; replayed
offline here.

Flow (DOJ release + seizure affidavit): ransom 15JFh88… (75 BTC) → DarkSide admin → affiliate/anchor
bc1qq2euq8… (received 69.604 BTC incl. the 63.7 share) → seizure tx moves 63.69996546 BTC to the FBI
holding address bc1qpx7vyv5…. Downstream addresses are DISCOVERED from the anchors' transfers.

Find-the-gaps validation: it asserts BIH's invariant-honoring behavior (UTXO facts only, NO fabricated
transfer edge, the input→output linkage living solely in a FIFO trace, intact provenance, attribution
honestly absent). Where the bounded real data diverges from the simplified checklist (notably: the seized
63.7 BTC has since been MOVED by the government, so it is no longer "unspent"), the divergence is
documented in the dossier Results — NOT tuned away.
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
from backend.app.services.export import export_case, verify_casefile
from backend.app.services.tracing import create_trace, fifo_trace_transaction, trace_btc_links
from backend.tests.integration._helpers import new_case

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "validation"
TIP = (FIX / "colonial_tip.txt").read_text().strip()
RANSOM = "15JFh88FcE4WL6qeMLgX5VEAFCbRXjc9fr"          # Colonial → attackers (legacy P2PKH)
ANCHOR = "bc1qq2euq8pw950klpjcawuy4uj39ym43hs6cfsegq"  # affiliate / seized-share address
FBI = "bc1qpx7vyv5tp7dm0g475ev527krg764t73dh77gls"     # FBI holding address
ADDRS = {RANSOM: "ransom", ANCHOR: "anchor", FBI: "fbi"}
RANSOM_SAT = "7500030000"   # 75.0003 BTC — the ransom payment output to 15JFh88
FUNDING_SAT = "6960422177"  # 69.60422177 BTC — the affiliate's funding output at the anchor
SEIZED_SAT = "6369996546"   # 63.69996546 BTC — the seizure output to the FBI holding address


def _esplora_router(request):
    p = request.url.path
    if p.endswith("/blocks/tip/height"):
        return httpx.Response(200, text=TIP)
    for addr, name in ADDRS.items():
        if f"/address/{addr}" in p:
            if "/txs/chain/" in p:
                return httpx.Response(200, json=[])  # single page; no further confirmed txs
            kind = "txs" if p.endswith("/txs") else "stats"
            return httpx.Response(200, json=json.loads((FIX / f"colonial_{name}_{kind}.json").read_text()))
    return httpx.Response(404)


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="Colonial Pipeline / DarkSide (2021)")
    yield conn, db
    conn.close()


@respx.mock
@pytest.mark.smoke
def test_colonial_pipeline_validation(case):
    conn, db = case
    respx.route(host="blockstream.info").mock(side_effect=_esplora_router)
    btc = EsploraConnector(settings=get_settings(), rate_limiter=RateLimiter(0, enabled=False),
                           sleep=lambda _s: None)
    for addr in (RANSOM, ANCHOR, FBI):  # the three DOJ-named addresses; downstream is discovered
        btc.get_transactions(conn, "bitcoin", addr)
        btc.get_balance(conn, "bitcoin", addr)
    btc.close()

    # ===================== 1. FACTS (Esplora/UTXO) — inputs/outputs, never a transfer =====================
    # Invariant #5: Bitcoin produces transaction_ + tx_input/tx_output rows ONLY. NEVER a transfer.
    assert conn.execute("SELECT COUNT(*) FROM transfer").fetchone()[0] == 0
    # The 75 BTC ransom payment arrives as a tx_output to the ransom address.
    assert conn.execute(
        """SELECT COUNT(*) FROM tx_output o JOIN address a ON a.id=o.address_id
           WHERE a.address=? AND o.amount=?""", (RANSOM, RANSOM_SAT)).fetchone()[0] == 1
    # The affiliate's funding at the anchor (69.604 BTC incl. the 63.7 share).
    funding_out = conn.execute(
        """SELECT o.id FROM tx_output o JOIN address a ON a.id=o.address_id
           WHERE a.address=? AND o.amount=?""", (ANCHOR, FUNDING_SAT)).fetchone()
    assert funding_out is not None
    # The seizure: 63.7 BTC output to the FBI holding address.
    seizure_out = conn.execute(
        """SELECT o.id, o.spent, o.spending_tx_id, o.transaction_id FROM tx_output o
           JOIN address a ON a.id=o.address_id WHERE a.address=? AND o.amount=?""",
        (FBI, SEIZED_SAT)).fetchone()
    assert seizure_out is not None
    # Provenance on every fact (Inv #3): no UTXO row without a source_query.
    for tbl in ("transaction_", "tx_input", "tx_output"):
        assert conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE source_query_id IS NULL").fetchone()[0] == 0

    # ===================== 2. INVARIANT #5 — the headline BTC validation =====================
    # Every UTXO movement row has a NULL source address — the ledger never records which input funded
    # which output, so BIH fabricates no input→output edge as a fact.
    utxo_rows = conn.execute(
        "SELECT src_address_id FROM v_value_movement WHERE paradigm='utxo'").fetchall()
    assert len(utxo_rows) > 0 and all(r["src_address_id"] is None for r in utxo_rows)

    # ===================== 3. TRACING (FIFO) — the linkage lives ONLY in a trace =====================
    # The seizure tx's input spends the anchor's funding output — that the input spends THAT outpoint is a
    # ledger FACT (the input carries the outpoint); the input→output apportionment is not.
    seizure_tx = seizure_out["transaction_id"]
    prev = conn.execute(
        "SELECT prev_output_id FROM tx_input WHERE transaction_id=?", (seizure_tx,)).fetchone()
    assert prev["prev_output_id"] == funding_out["id"]  # input spends the anchor's funding output (fact)

    # Reconstruct the seizure hop as a FIFO trace: the 63.7 BTC to the FBI address apportioned (basis=fifo)
    # from the anchor's funding output. THIS is the ransom→affiliate→seizure linkage — a labeled claim,
    # never a transfer fact.
    trace = create_trace(conn, name="Colonial seizure — FIFO forward trace")
    res = fifo_trace_transaction(conn, trace_id=trace, transaction_id=seizure_tx)
    assert res["links_written"] >= 1
    links = trace_btc_links(conn, trace)
    assert links and all(link["basis"] == "fifo" and link["is_convention"] for link in links)
    fifo_to_fbi = conn.execute(
        """SELECT id FROM trace_btc_link WHERE trace_id=? AND source_output_id=? AND dest_output_id=?
           AND basis='fifo'""", (trace, funding_out["id"], seizure_out["id"])).fetchone()
    assert fifo_to_fbi is not None  # the 63.7 BTC linkage exists ONLY as a basis='fifo' trace claim

    # FIND-THE-GAP: the checklist expects the seized 63.7 BTC "unspent at the FBI holding address" — true
    # at seizure (2021-06-07), but in CURRENT chain data the government has since MOVED it. BIH (showing
    # live state) marks the seizure output SPENT, with the later government move as its spender. The seizure
    # FACT is intact; we assert the live state and document the point-in-time divergence (Results) — never
    # asserting a stale "unspent".
    assert seizure_out["spent"] == 1 and seizure_out["spending_tx_id"] is not None

    # ===================== 4. ATTRIBUTION / RISK — gracefully absent (no fabrication) =====================
    # No public GraphSense TagPack covers these DarkSide operational addresses, and we make NO OFAC
    # assumption (the seizure address is not necessarily SDN-listed — let the connectors speak). So BIH
    # invents nothing: attribution and risk are honestly ABSENT (Inv #4 / no-synthesis).
    assert conn.execute("SELECT COUNT(*) FROM attribution").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM risk_assessment").fetchone()[0] == 0

    # ===================== 5. REPORT / EXPORT =====================
    results = run_audits(db_path=str(db))
    assert all(r.passed for r in results), [(r.name, r.offending) for r in results if not r.passed]
    assert any(r.name == "no-fabricated-utxo-edge" and r.passed for r in results)  # Invariant #5 audit
    # A reproducible, self-contained case file generates and re-verifies. Close the connection first so
    # SQLite checkpoints the WAL into case.db (the Ronin-case export hardening).
    conn.close()
    bundle = export_case(db.parent, out_path=db.parent.parent / "colonial.casefile")
    report = verify_casefile(bundle, extract_to=db.parent.parent / "colonial_extracted")
    assert report["ok"] is True, report
