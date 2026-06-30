"""Hydra / Garantex (OFAC 2022-04-05) — real dual-listed case recreated in BIH: the FIRST validation to
exercise POSITIVE GraphSense attribution and the MULTI-SOURCE, never-merge model (Invariant #4). Every
prior validation (Ronin, Colonial, Bitfinex) had GraphSense attribution gracefully ABSENT; this one drives
the positive path — a real public GraphSense TagPack resolving an entity — AND proves two independent
sources speak about the SAME address side-by-side without collapsing.

Spec + comparison checklist: docs/validation/hydra_garantex_attribution_case.md (the "Results" section
records the actual-vs-expected). Fixtures (`tests/fixtures/validation/hydra_*`):
  - `hydra_anchor_txs.json` / `hydra_anchor_stats.json` / `hydra_tip.txt`: RAW Blockstream Esplora
    responses recorded once (keyless public API), replayed offline.
  - `hydra_tagpack.yaml` / `hydra_actorpack.yaml`: a BOUNDED SLICE of the REAL public GraphSense
    TagPack `packs/hydra.yaml` + ActorPack `actors/graphsense.actorpack.yaml`
    (github.com/graphsense/graphsense-tagpacks) — header/actor preserved verbatim, tags trimmed to the
    anchor. Structured import of public data (Invariant #1).
  - `hydra_ofac_sdn.xml`: a small OFAC SDN snapshot of the real HYDRA MARKET designation (Entity, program
    CYBER2 — name/type/program VERIFIED against the OFAC Sanctions List Search id=36216), bounded to the
    anchor's XBT address.

ANCHOR (confirmed dual-listed empirically, STEP 0 — NOT guessed): 16ZSAEfYpPCj3D94fsNt2okYj9Ue8mxy6T.
A Hydra Market deposit address present in BOTH the GraphSense `hydra.yaml` TagPack (entity "Hydra Market")
AND the OFAC SDN crypto-address extract (all 117 hydra.yaml addresses intersect the OFAC XBT list 117/117;
the 3 Garantex anchors also confirmed in OFAC). Clean bounded 2-tx history: a 0.0115 BTC deposit
(e5015b6e, h=644385) forwarded into the market's consolidation (c41de249, h=645827).

Independence caveat (honest, documented — see the dossier Results): `hydra.yaml`'s `source:` backlink is the
OFAC Treasury action page, so the GraphSense and OFAC claims share a root EVENT (the 2022-04-05 designation).
They remain two DISTINCT connectors producing DISTINCT representations (GraphSense: "Hydra Market", a darknet
market, cluster-definer; OFAC: "HYDRA MARKET", sanctioned) — which is exactly the Invariant #4 behavior under
test. We do not over-claim full provenance independence.

Find-the-gaps, not pass-the-test: divergences (GraphSense risk absent — the real pack has no `abuse`; the
two labels differ only in case + taxonomy) are documented in the dossier, never tuned away.
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
from backend.app.connectors.imports.graphsense import GraphSenseImporter
from backend.app.connectors.imports.ofac import OfacSdnImporter
from backend.app.services.claims_display import address_claims
from backend.app.services.export import export_case, verify_casefile
from backend.tests.integration._helpers import new_case

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "validation"
TIP = (FIX / "hydra_tip.txt").read_text().strip()
ANCHOR = "16ZSAEfYpPCj3D94fsNt2okYj9Ue8mxy6T"             # Hydra Market deposit addr (dual-listed)
DEPOSIT_TXID = "e5015b6ed1d35e511ef22ca0e52e0bb90a33943852706bcddb37956437cf68d8"   # 0.0115 BTC in
FORWARD_TXID = "c41de2491b8349988da08c637c2fa6ce19c56bb44c14377aaed59d14072a4234"   # forwarded out
DEPOSIT_SAT = "1152977"                                  # 0.01152977 BTC received by the anchor
GS_LABEL = "Hydra Market"                                # GraphSense TagPack label
OFAC_LABEL = "HYDRA MARKET"                              # OFAC SDN primary name (verified id=36216)


def _esplora_router(request):
    p = request.url.path
    if p.endswith("/blocks/tip/height"):
        return httpx.Response(200, text=TIP)
    if p.endswith(f"/address/{ANCHOR}/txs"):
        return httpx.Response(200, json=json.loads((FIX / "hydra_anchor_txs.json").read_text()))
    if p.endswith(f"/address/{ANCHOR}"):
        return httpx.Response(200, json=json.loads((FIX / "hydra_anchor_stats.json").read_text()))
    return httpx.Response(404)


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="Hydra / Garantex (OFAC 2022-04-05)")
    yield conn, db
    conn.close()


@respx.mock
@pytest.mark.smoke
def test_hydra_garantex_validation(case):
    conn, db = case
    respx.route(host="blockstream.info").mock(side_effect=_esplora_router)
    btc = EsploraConnector(settings=get_settings(), rate_limiter=RateLimiter(0, enabled=False),
                           sleep=lambda _s: None)
    btc.get_transactions(conn, "bitcoin", ANCHOR)
    btc.get_balance(conn, "bitcoin", ANCHOR)
    btc.close()
    # GraphSense (free attribution pillar): ActorPack -> entity "Hydra Market"; TagPack -> attribution +
    # membership; the same TagPack's get_risk is the (honestly empty) abuse->risk pass.
    GraphSenseImporter().get_entities(conn, FIX / "hydra_actorpack.yaml")
    GraphSenseImporter().get_attributions(conn, FIX / "hydra_tagpack.yaml")
    GraphSenseImporter().get_risk(conn, FIX / "hydra_tagpack.yaml")
    GraphSenseImporter().get_entities(conn, FIX / "hydra_tagpack.yaml")
    # OFAC (free risk pillar): the SAME anchor, independently, from the SDN list.
    OfacSdnImporter().get_risk(conn, FIX / "hydra_ofac_sdn.xml")
    OfacSdnImporter().get_attributions(conn, FIX / "hydra_ofac_sdn.xml")

    anchor_id = conn.execute("SELECT id FROM address WHERE address=?", (ANCHOR,)).fetchone()["id"]

    # ===================== 1. FACTS (Esplora/UTXO) — inputs/outputs, never a transfer =====================
    assert conn.execute("SELECT COUNT(*) FROM transfer").fetchone()[0] == 0          # Invariant #5
    deposit = conn.execute(
        """SELECT o.id, o.spent, o.spending_tx_id FROM tx_output o JOIN address a ON a.id=o.address_id
           WHERE a.address=? AND o.amount=?""", (ANCHOR, DEPOSIT_SAT)).fetchone()
    assert deposit is not None                                                        # the 0.0115 BTC deposit is a tx_output fact
    # Provenance on every fact (Inv #3).
    for tbl in ("transaction_", "tx_input", "tx_output"):
        assert conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE source_query_id IS NULL").fetchone()[0] == 0
    # Find-the-gap (live state, like Colonial/Bitfinex): the deposit did NOT sit — it was FORWARDED into
    # Hydra's consolidation. The output is marked spent within the same sync (the spend tx is in history),
    # and the anchor's balance is 0. BIH shows the funds moved on; we assert that, not a held balance.
    forward_tx_id = conn.execute("SELECT id FROM transaction_ WHERE tx_hash=?", (FORWARD_TXID,)).fetchone()["id"]
    assert deposit["spent"] == 1 and deposit["spending_tx_id"] == forward_tx_id
    bal = conn.execute(
        """SELECT b.amount FROM balance_snapshot b JOIN address a ON a.id=b.address_id
           WHERE a.address=?""", (ANCHOR,)).fetchone()
    assert bal is not None and bal["amount"] == "0"
    # Invariant #5 audit hook: every UTXO movement has NULL src.
    utxo = conn.execute("SELECT src_address_id FROM v_value_movement WHERE paradigm='utxo'").fetchall()
    assert len(utxo) > 0 and all(r["src_address_id"] is None for r in utxo)

    # ============ 2. ATTRIBUTION — the headline (FIRST positive validation) ============
    # The path all prior cases tested only in the NEGATIVE: the anchor resolves via the GraphSense importer
    # to a real attribution AND an entity + membership. Assert these rows EXIST.
    gs_attr = conn.execute(
        """SELECT at.label, at.category, at.confidence, at.note FROM attribution at
           WHERE at.address_id=? AND at.source='graphsense'""", (anchor_id,)).fetchone()
    assert gs_attr is not None and gs_attr["label"] == GS_LABEL and gs_attr["category"] == "market"
    assert gs_attr["confidence"] == pytest.approx(0.60)            # authority_data -> 60 -> 0.60
    assert "graphsense-tagpacks" not in (gs_attr["note"] or "")    # note carries the real source backlink, not a path
    # The GraphSense entity materialized from the ActorPack, and the anchor is a member of it.
    membership = conn.execute(
        """SELECT m.source, m.method, m.confidence, m.flags, e.name, e.origin, e.external_id, e.entity_type
           FROM entity_membership m JOIN entity e ON e.id=m.entity_id
           WHERE m.address_id=? AND m.source='graphsense'""", (anchor_id,)).fetchone()
    assert membership is not None, "POSITIVE attribution gap: the anchor did not resolve to a GraphSense entity"
    assert membership["name"] == GS_LABEL and membership["origin"] == "source"        # a real sourced entity, not fabricated
    assert membership["external_id"] == "hydramarket" and membership["method"] == "tagpack-actor"
    assert membership["flags"] == "cluster-definer"                                   # is_cluster_definer: true

    # ============ 3. RISK — OFAC independently flags the SAME address sanctioned ============
    ofac_risk = conn.execute(
        """SELECT r.category, r.source, r.score, r.score_scale, r.rationale FROM risk_assessment r
           WHERE r.address_id=? AND r.source='ofac-sdn' AND r.category='sanctioned'""", (anchor_id,)).fetchone()
    assert ofac_risk is not None and OFAC_LABEL in ofac_risk["rationale"] and "CYBER2" in ofac_risk["rationale"]
    assert ofac_risk["score"] is None and ofac_risk["score_scale"] is None            # categorical, never a synthesized score
    # Find-the-gap: the REAL hydra.yaml carries NO `abuse` type, so the GraphSense side yields NO categorical
    # risk row — risk on the anchor is single-source (OFAC). We assert that honestly (the real pack is
    # attribution-only); BIH invents no abuse risk to manufacture a second risk source.
    assert conn.execute(
        "SELECT COUNT(*) FROM risk_assessment WHERE address_id=? AND source='graphsense'",
        (anchor_id,)).fetchone()[0] == 0

    # ============ 4. MULTI-SOURCE, never merged (Invariant #4) — the SECOND headline ============
    # The anchor carries attribution claims from >=2 DISTINCT sources, stored side-by-side, each with its
    # own provenance — and BIH does NOT collapse them into one synthesized label/score.
    attr_sources = {r["source"] for r in conn.execute(
        "SELECT DISTINCT source FROM attribution WHERE address_id=?", (anchor_id,)).fetchall()}
    assert attr_sources == {"graphsense", "ofac-sdn"}             # >= 2 distinct sources, side-by-side
    # The GraphSense label and the OFAC entity name DIFFER (case + taxonomy) — BOTH are kept, not merged
    # into one canonical "Hydra Market". This disagreement surviving is the never-collapse principle.
    labels = {(r["source"], r["label"], r["category"]) for r in conn.execute(
        "SELECT source, label, category FROM attribution WHERE address_id=?", (anchor_id,)).fetchall()}
    assert ("graphsense", GS_LABEL, "market") in labels
    assert ("ofac-sdn", OFAC_LABEL, "sanctioned_entity") in labels
    assert GS_LABEL != OFAC_LABEL                                 # genuinely distinct stored strings, both retained
    # The side-by-side display surfaces both sources and has NO averaged/combined/synthesized key.
    d = address_claims(conn, anchor_id)
    assert set(d["attributions_by_source"]) == {"graphsense", "ofac-sdn"}
    assert set(d["risks_by_source"]) == {"ofac-sdn"}
    assert "combined" not in d and "averaged" not in d
    # No synthesized source value anywhere in the case (a merged "answer" would be the failure to catch).
    used = {r[0] for r in conn.execute(
        "SELECT source FROM attribution UNION SELECT source FROM risk_assessment").fetchall()}
    assert not (used & {"combined", "averaged", "synthetic", "aggregate", "merged", "blended"})

    # ============ 5. REPORT / EXPORT ============
    results = run_audits(db_path=str(db))
    assert all(r.passed for r in results), [(r.name, r.offending) for r in results if not r.passed]
    assert any(r.name == "no-fabricated-utxo-edge" and r.passed for r in results)     # Invariant #5
    assert any(r.name == "append-only-claims" and r.passed for r in results)          # claims never rewritten/merged (Inv #4)
    # Every sourced claim (both pillars) carries its provenance.
    for tbl in ("attribution", "risk_assessment", "entity_membership"):
        assert conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE source_query_id IS NULL").fetchone()[0] == 0
    conn.close()
    bundle = export_case(db.parent, out_path=db.parent.parent / "hydra.casefile")
    report = verify_casefile(bundle, extract_to=db.parent.parent / "hydra_extracted")
    assert report["ok"] is True, report
