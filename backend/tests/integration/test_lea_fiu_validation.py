"""LEA/FIU end-to-end validation — the court-ready DELIVERABLE chain (P28 / FN-07).

Unlike the six real-world golden cases (Colonial, Ronin, Bitfinex, Hydra/Garantex, CoinJoin, Genesis),
which assert BIH's invariant-honoring behavior against MESSY real data, this case validates the
court-ready OUTPUT chain that Tracks D (reporting — P13–P17) and F (scale/lifecycle — P25) added, against a
SYNTHETIC, known-count scenario so the assertions are exact and deterministic:

  * ingest/import via a REAL importer (the P22 Etherscan CSV export path);
  * a court-ready report showing **chain-of-custody** (P2) + **methodology** (P13) + **numbered exhibits**
    (P15), rendered deterministically (fixed ``generated_at`` → identical ``content_hash``);
  * **graph scope/pagination** (P25 — ``bound_subgraph`` + ``focus_incident``) for an LEA-scale payload;
  * a **second idempotent ingest** (zero dupes — Invariant #7);
  * an **export round-trip** (export → re-import → audit) that also carries the P27 in-DB anchor.

**The data is SYNTHETIC and illustrative** — the addresses/labels/designations are NOT real and make no
claim about any real person or entity. It is built entirely from a public structured-import format
(the Etherscan UI "Download CSV Export") + investigator constructions (trace/entity/exhibit/finding/
annotation) — no fabricated API cassette, no scraping (Invariants #1/#3). Scenario ("Operation Ledger"):
a subject S receives 10 ETH from a victim V, deposits 4 ETH to an exchange X, sends 3 ETH to a sanctioned
mixer M (+ one reverted and one zero-value contract call), atop a separately-confirmed 5 ETH theft inflow.

Spec + expected counts: docs/validation/lea_fiu_case.md.
"""

from __future__ import annotations

import pytest

from backend.app.audits.runner import run_audits
from backend.app.connectors.imports.etherscan_csv import EtherscanCsvImporter
from backend.app.db import repository as repo
from backend.app.models import (
    Address,
    Annotation,
    Asset,
    Attribution,
    Entity,
    EntityMembership,
    RiskAssessment,
    SourceQuery,
    Transaction,
    Transfer,
    Valuation,
)
from backend.app.provenance.atomic import write_with_provenance
from backend.app.services.exhibits import attach_screenshot, numbered_exhibits
from backend.app.services.export import export_case, verify_casefile
from backend.app.services.graph import bound_subgraph, build_graph
from backend.app.services.investigator import add_finding_ref, create_finding
from backend.app.services.tracing import add_trace_transfer, create_trace
from backend.tests.integration._helpers import new_case

from pathlib import Path

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "validation" / "lea_fiu_etherscan.csv"
S = "0x52908400098527886E0F7030069857D2E4169EE7"  # subject of investigation
V = "0x8617E340B3D01FA5F11F306F4090FD50E238070D"  # victim (theft source)
X = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"  # exchange deposit address
M = "0xdAC17F958D2ee523a2206206994597C13D831ec7"  # (synthetic) sanctioned mixer
GEN_AT = "2026-07-04T12:00:00Z"  # a FIXED report timestamp so two renders hash identically (determinism)


def _addr_id(conn, display: str) -> str:
    return conn.execute("SELECT id FROM address WHERE address=?", (display.lower(),)).fetchone()["id"]


def _with_prov(conn, connector, capability, write_fn, raw="synthetic,structured,import"):
    """Write one sourced claim inside a single provenance transaction (Invariant #3), like the real
    importers do. Returns whatever ``write_fn`` returns."""
    sq = SourceQuery(connector=connector, capability=capability, endpoint="import",
                     params={"bounds": "default"}, requested_at="2026-03-15T00:00:00Z", status="ok")
    out = {}

    def write(c, sqid):
        out["r"] = write_fn(c, sqid)

    write_with_provenance(conn, sq, write, raw_response=raw)
    return out["r"]


def _seed_confirmed_theft(conn) -> str:
    """A separately-CONFIRMED (final) 5 ETH inflow V→S — the theft transaction as pulled via the Etherscan
    API — giving the case final facts (the CSV export carries no confirmations, so its rows are provisional
    per Inv #6). Returns the transfer id (the valuation subject)."""
    sq = SourceQuery(connector="etherscan", capability="get_transactions", endpoint="account/txlist",
                     params={"address": S, "bounds": "confirmed"},
                     requested_at="2026-03-01T00:00:00Z", completed_at="2026-03-01T00:00:01Z", status="ok")
    out = {}

    def write(c, sqid):
        asset = repo.upsert_asset(c, Asset(chain="ethereum", symbol="ETH", decimals=18), sqid)
        v = repo.upsert_address(c, Address(chain="ethereum", address_display=V), sqid)
        s = repo.upsert_address(c, Address(chain="ethereum", address_display=S), sqid)
        tx = repo.upsert_transaction(c, Transaction(
            chain="ethereum", tx_hash="0x6a" + "6" * 62, block_height=18999000,
            block_ts="2024-03-08T00:00:00Z", fee="210000000000000", status="1",
            confirmations=200, finality_status="final"), sqid)
        out["tr"] = repo.upsert_transfer(c, Transfer(
            transaction_id=tx, chain="ethereum", from_address_id=v, to_address_id=s,
            asset_id=asset, amount="5000000000000000000", transfer_type="native", position=0), sqid)

    write_with_provenance(conn, sq, write, raw_response={"status": "1", "result": [{"hash": "0x6a"}]})
    return out["tr"]


@pytest.fixture
def built_case(tmp_path):
    """Rebuild the full 'Operation Ledger' case through the normal pipeline: import → confirmed-fact seed →
    claims (attribution/risk/valuation/entity) → trace → exhibit → finding → annotation."""
    conn, db = new_case(tmp_path, title="Operation Ledger — synthetic LEA/FIU validation")

    # 1. INGEST/IMPORT — S's on-chain history via the real P22 Etherscan CSV importer (provisional facts).
    imp = EtherscanCsvImporter().get_transactions(conn, FIX, chain="ethereum")

    # 2. A separately-confirmed FINAL theft inflow (gives the case final facts + a valuation subject).
    theft_tr = _seed_confirmed_theft(conn)

    x_id, m_id = _addr_id(conn, X), _addr_id(conn, M)

    # 3. CLAIMS (structured, each with provenance) — attribution + sanctioned risk + valuation + entity.
    _with_prov(conn, "arkham-import", "get_attributions", lambda c, sq: repo.insert_attribution(
        c, Attribution(address_id=x_id, label="Acme Exchange", category="exchange", source="arkham",
                       confidence=0.9, retrieved_at="2026-03-15T00:00:00Z"), sq))
    _with_prov(conn, "ofac-sdn", "get_risk", lambda c, sq: repo.insert_risk_assessment(
        c, RiskAssessment(address_id=m_id, category="sanctioned", source="ofac-sdn",
                          rationale="OFAC SDN (synthetic/illustrative): sanctioned mixer for validation",
                          retrieved_at="2026-03-15T00:00:00Z"), sq))
    _with_prov(conn, "defillama", "get_prices", lambda c, sq: repo.insert_valuation(
        c, Valuation(subject_type="transfer", subject_id=theft_tr, currency="USD", unit_price="2500",
                     value="12500", price_timestamp="2024-03-08T00:00:00Z", confidence=0.95,
                     source="defillama", retrieved_at="2026-03-15T00:00:00Z"), sq))
    ent = repo.insert_entity(conn, Entity(origin="source", name="Acme Exchange", entity_type="exchange"))
    _with_prov(conn, "arkham-import", "get_attributions", lambda c, sq: repo.insert_entity_membership(
        c, EntityMembership(entity_id=ent, address_id=x_id, source="arkham", method="shared-label"), sq))

    # 4. TRACE over the subject's outbound transfers (the 4 ETH → exchange + 3 ETH → mixer).
    trace = create_trace(conn, name="Operation Ledger — subject outbound trace")
    for row in conn.execute(
        "SELECT t.id FROM transfer t JOIN address a ON a.id=t.from_address_id WHERE a.address=?",
        (S.lower(),),
    ).fetchall():
        add_trace_transfer(conn, trace_id=trace, transfer_id=row["id"])

    # 5. EXHIBIT — a screenshot artifact (numbered in the report), + 6. a FINDING citing it + the mixer,
    #    + 7. an ANNOTATION on the trace.
    shot = tmp_path / "src" / "mixer_deposit_screenshot.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"synthetic exhibit bytes for LEA/FIU validation")
    ex = attach_screenshot(conn, file_path=shot, source="etherscan",
                           description="Etherscan screenshot: subject -> sanctioned mixer (3 ETH)")
    finding = create_finding(conn, statement="Subject moved 3 ETH to a sanctioned mixer", assessment="high")
    add_finding_ref(conn, finding_id=finding, ref_type="address", ref_id=m_id)
    add_finding_ref(conn, finding_id=finding, ref_type="exhibit", ref_id=ex)
    repo.insert_annotation(conn, Annotation(
        target_type="trace", target_id=trace,
        content="Subject's laundering path: victim inflow -> exchange deposit + sanctioned mixer."))

    yield conn, db, tmp_path, {"import": imp, "trace": trace, "exhibit": ex}
    conn.close()


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


@pytest.mark.smoke
def test_rebuild_and_audit(built_case):
    """The case rebuilds deterministically with EXACT expected counts, audits pass, and a 2nd ingest of the
    same export adds zero duplicate facts (Invariant #7)."""
    conn, db, _tmp, meta = built_case

    # Deterministic import result (5 rows → 5 tx; 3 value transfers; 1 reverted + 1 zero-value carry no transfer).
    assert (meta["import"]["transactions"], meta["import"]["transfers"]) == (5, 3)
    assert meta["import"]["failed"] == 1 and meta["import"]["skipped"] == 1

    # Exact case counts (5 CSV tx + 1 confirmed theft tx = 6; 3 CSV transfers + 1 theft = 4).
    assert _count(conn, "transaction_") == 6
    assert _count(conn, "transfer") == 4
    assert _count(conn, "attribution") == 1
    assert _count(conn, "risk_assessment") == 1
    assert _count(conn, "valuation") == 1
    assert _count(conn, "entity") == 1 and _count(conn, "entity_membership") == 1
    assert _count(conn, "exhibit") == 1
    assert _count(conn, "finding") == 1 and _count(conn, "annotation") == 1
    # One final fact (the confirmed theft) + three provisional (the CSV export carries no confirmations).
    assert _count(conn, "transaction_ WHERE finality_status='final'") == 1
    assert _count(conn, "transaction_ WHERE finality_status='provisional'") == 5

    # Every invariant audit passes on the rebuilt case.
    results = run_audits(db_path=str(db))
    assert all(r.passed for r in results), [(r.name, r.offending) for r in results if not r.passed]

    # 2ND IDEMPOTENT INGEST — re-import the identical export → zero new tx/transfer rows (occurrence dedup).
    before = (_count(conn, "transaction_"), _count(conn, "transfer"))
    EtherscanCsvImporter().get_transactions(conn, FIX, chain="ethereum")
    assert (_count(conn, "transaction_"), _count(conn, "transfer")) == before
    assert all(r.passed for r in run_audits(db_path=str(db)))  # still green after the re-ingest


@pytest.mark.smoke
def test_report_is_court_ready(built_case):
    """The report carries the court-readiness sections (chain-of-custody + methodology + numbered exhibits)
    and renders deterministically (a fixed ``generated_at`` → identical ``content_hash``)."""
    from backend.app.services.reporting import generate_report

    conn, _db, tmp_path, _meta = built_case
    r1 = generate_report(conn, case_dir=tmp_path, title="Operation Ledger — Report",
                         generated_at=GEN_AT, render_pdf=False)
    html = r1["html_path"].read_text(encoding="utf-8")

    assert "Chain of custody" in html          # P2 — every source_query listed
    assert "Methodology" in html               # P13 — how to read the report + finality thresholds
    assert "List of Exhibits" in html          # P15 — numbered exhibits
    assert "Exhibit 1" in html                 # the one screenshot, numbered
    assert numbered_exhibits(conn)[0]["label"] == "Exhibit 1"

    # Determinism: re-rendering the unchanged case with the same fixed timestamp yields an identical hash.
    r2 = generate_report(conn, case_dir=tmp_path, title="Operation Ledger — Report",
                         generated_at=GEN_AT, render_pdf=False)
    assert r2["content_hash"] == r1["content_hash"]


@pytest.mark.smoke
def test_graph_scope_and_pagination(built_case):
    """P25: a bounded subgraph reports honest truncation meta and an address-scoped build returns a
    non-empty neighborhood — the LEA-scale payload controls this case validates."""
    conn, _db, _tmp, _meta = built_case

    full = bound_subgraph(build_graph(conn), limit=10_000)
    total = full["meta"]["total_nodes"]
    assert total > 1 and full["meta"]["truncated"] is False

    small = bound_subgraph(build_graph(conn), limit=total - 1)
    assert small["meta"]["truncated"] is True
    assert small["meta"]["returned_nodes"] <= total - 1

    # focus_incident (the ?address_id path) bounds the scan to the subject's neighborhood and still builds.
    scoped = build_graph(conn, focus_incident="addr:" + _addr_id(conn, S))
    assert scoped["nodes"], "an address-scoped build must return the subject's neighborhood"


@pytest.mark.smoke
def test_export_roundtrips_with_anchor(built_case):
    """Acceptance: export → re-import → audit round-trips, and the P27 in-DB final-immutability anchor
    travels inside the bundle (tamper-evident)."""
    conn, db, _tmp, _meta = built_case
    assert all(r.passed for r in run_audits(db_path=str(db)))  # establishes the baseline that must travel
    conn.close()  # checkpoint the WAL into case.db before zipping (export robustness)

    bundle = export_case(db.parent, out_path=db.parent.parent / "lea_fiu.casefile")
    report = verify_casefile(bundle, extract_to=db.parent.parent / "lea_fiu_extracted")
    assert report["ok"] is True, report
    assert report["self_contained"]["audits_passed"] is True
    assert report["self_contained"]["final_anchor_present"] is True  # P27 anchor rode along in case.db
