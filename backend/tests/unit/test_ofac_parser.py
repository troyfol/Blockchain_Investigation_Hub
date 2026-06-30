"""OFAC SDN sanctions import — the free risk pillar (Phase A: get_risk).

OFAC lists crypto addresses on SDN entries as "Digital Currency Address - <TICKER>" ids
(docs/findings/ofac_sanctions_reconciliation.md). This connector writes a CATEGORICAL
risk_assessment(category='sanctioned', score=None) per BTC/EVM address, with rationale carrying the
entity name + program. Covered: ticker→chain mapping (incl. ERC-20 → ethereum), unsupported-ticker
skip+report (never canonicalized), entity+program rationale, individual "LAST, First" naming, idempotent
re-ingest, publication-date provenance, all-or-nothing on a malformed address, and delisting reporting.

XML-format note: implemented against the standard `sdn.xml` (faithfully modellable), not the spec's
`sdn_advanced.xml` (reference-value indirection unconfirmable offline) — see the adapter docstring.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.audits.runner import run_audits
from backend.app.connectors.base import ConnectorError
from backend.app.connectors.imports.ofac import OfacSdnImporter
from backend.app.normalization.ofac_adapter import TICKER_TO_CHAIN, adapt_sdn_xml
from backend.tests.integration._helpers import new_case

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "imports"
XBT_ADDR = "1HQ3Go3ggs8pFnXuHVHRytPCq5fGG8Hbhx"
ETH_DISPLAY = "0x8589427373D6D84E98730D7795D8f6f8731FDA16"


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="OFAC")
    yield conn, db
    conn.close()


# --- contract tests over the SDN fixture -----------------------------------------------------

def test_real_sdn_maps_to_sanctioned_risk(case):
    conn, db = case
    res = OfacSdnImporter().get_risk(conn, FIX / "ofac_sdn.xml")
    # XBT, ETH, USDT supported -> 3 sanctioned rows; XMR skipped + reported.
    assert res["risks"] == 3
    assert res["skipped_unsupported"] == 1 and res["unsupported_tickers"] == ["XMR"]
    assert res["delisted"] == []  # first fetch — nothing delisted yet
    assert conn.execute("SELECT COUNT(*) FROM risk_assessment WHERE source='ofac-sdn'").fetchone()[0] == 3

    # Categorical: score/score_scale NULL; category 'sanctioned'; rationale = entity + program.
    btc = conn.execute(
        """SELECT r.score, r.score_scale, r.category, r.rationale, r.source, a.chain, a.address
           FROM risk_assessment r JOIN address a ON a.id=r.address_id WHERE a.chain='bitcoin'""").fetchone()
    assert btc["score"] is None and btc["score_scale"] is None
    assert btc["category"] == "sanctioned" and btc["source"] == "ofac-sdn"
    assert btc["rationale"] == "OFAC SDN: EVIL MIXER LLC (CYBER2)"
    assert btc["address"] == XBT_ADDR  # base58 untouched

    # Individual name "LAST, First"; multiple programs joined.
    eth = conn.execute(
        """SELECT r.rationale, a.address FROM risk_assessment r JOIN address a ON a.id=r.address_id
           WHERE r.rationale LIKE '%DOE%'""").fetchone()
    assert eth["rationale"] == "OFAC SDN: DOE, John (SDGT, CYBER2)"
    assert eth["address"] == ETH_DISPLAY.lower()  # EVM canonicalized (lowercased)

    # ERC-20 ticker USDT maps to the ethereum chain (not a 'usdt' chain).
    chains = {r[0] for r in conn.execute(
        "SELECT DISTINCT a.chain FROM risk_assessment r JOIN address a ON a.id=r.address_id").fetchall()}
    assert chains == {"bitcoin", "ethereum"}
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_unsupported_ticker_never_canonicalized(case):
    conn, db = case
    OfacSdnImporter().get_risk(conn, FIX / "ofac_sdn.xml")
    # The XMR address must never have been created (skipped before canonicalization).
    assert conn.execute(
        "SELECT COUNT(*) FROM address WHERE chain NOT IN ('bitcoin','ethereum')").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM address").fetchone()[0] == 3  # XBT + ETH + USDT only


def test_reingest_is_idempotent(case):
    conn, db = case
    OfacSdnImporter().get_risk(conn, FIX / "ofac_sdn.xml")
    OfacSdnImporter().get_risk(conn, FIX / "ofac_sdn.xml")  # same edition
    assert conn.execute("SELECT COUNT(*) FROM risk_assessment WHERE source='ofac-sdn'").fetchone()[0] == 3
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_provenance_endpoint_is_publish_date(case):
    conn, db = case
    OfacSdnImporter().get_risk(conn, FIX / "ofac_sdn.xml")
    row = conn.execute(
        "SELECT endpoint, params FROM source_query WHERE capability='get_risk'").fetchone()
    assert "06/26/2026" in row["endpoint"]   # the SDN publication date is the provenance endpoint
    assert "sdn_publish_date" in row["params"] and "treasury.gov" in row["params"]


def test_delisted_address_reported_and_retained_on_reingest(case):
    """Sanctions are mutable: a delisted address is REPORTED (absent from the new fetch) but its prior
    claim is RETAINED (append-only — 'X was sanctioned as of <date>' stays true; deleting a claim would
    break the append-only invariant + audit). The current list is the latest fetch."""
    conn, db = case
    OfacSdnImporter().get_risk(conn, FIX / "ofac_sdn.xml")
    res2 = OfacSdnImporter().get_risk(conn, FIX / "ofac_sdn_delisted.xml")

    # EVIL MIXER (XBT) was removed in the new edition -> reported as delisted.
    assert any(XBT_ADDR in d for d in res2["delisted"])
    assert res2["delisted"] == [f"bitcoin:{XBT_ADDR}"]
    # Still-listed DOE/STABLE not duplicated (idempotent); NEW BAD ACTOR (ARB) added -> 4 total.
    assert conn.execute("SELECT COUNT(*) FROM risk_assessment WHERE source='ofac-sdn'").fetchone()[0] == 4
    # The delisted XBT claim is retained (not deleted), with its original provenance.
    assert conn.execute(
        "SELECT COUNT(*) FROM risk_assessment r JOIN address a ON a.id=r.address_id "
        "WHERE a.address=?", (XBT_ADDR,)).fetchone()[0] == 1
    # The new ARB address is on the arbitrum chain.
    assert conn.execute("SELECT COUNT(*) FROM address WHERE chain='arbitrum'").fetchone()[0] == 1
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_malformed_supported_address_is_all_or_nothing(case, tmp_path):
    conn, db = case
    bad = tmp_path / "bad.xml"
    bad.write_text(
        '<?xml version="1.0"?><sdnList><sdnEntry><uid>1</uid><lastName>X</lastName>'
        '<sdnType>Entity</sdnType><idList><id>'
        '<idType>Digital Currency Address - ETH</idType><idNumber>0xNOTHEX</idNumber>'
        '</id></idList></sdnEntry></sdnList>', encoding="utf-8")
    with pytest.raises(ConnectorError) as exc:
        OfacSdnImporter().get_risk(conn, bad)
    assert "unparseable" in str(exc.value)
    for table in ("risk_assessment", "address", "source_query"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # nothing written


def test_doctype_is_rejected(case, tmp_path):
    conn, db = case
    bad = tmp_path / "doctype.xml"
    bad.write_text(
        '<?xml version="1.0"?><!DOCTYPE sdnList [<!ENTITY x "y">]>'
        '<sdnList><sdnEntry><uid>1</uid><lastName>X</lastName></sdnEntry></sdnList>', encoding="utf-8")
    with pytest.raises(ConnectorError) as exc:
        OfacSdnImporter().get_risk(conn, bad)
    assert "DOCTYPE" in str(exc.value)  # entity-expansion vector refused


# --- adversarial-review regression tests (2026-06-28) ----------------------------------------

def _xml(tmp_path, name, body):
    f = tmp_path / name
    f.write_text(body, encoding="utf-8")
    return f


def test_get_attributions_malformed_is_all_or_nothing(case, tmp_path):
    conn, db = case
    bad = _xml(tmp_path, "bad.xml",
               '<?xml version="1.0"?><sdnList><sdnEntry><uid>1</uid><lastName>X</lastName>'
               '<sdnType>Entity</sdnType><idList><id>'
               '<idType>Digital Currency Address - ETH</idType><idNumber>0xNOTHEX</idNumber>'
               '</id></idList></sdnEntry></sdnList>')
    with pytest.raises(ConnectorError) as exc:
        OfacSdnImporter().get_attributions(conn, bad)
    assert "unparseable" in str(exc.value)
    for table in ("attribution", "address", "source_query"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # nothing written


def test_nameless_entry_gets_risk_but_no_synthesized_attribution(case, tmp_path):
    """A sanctioned address whose SDN entry has no entity name still gets a risk row, but NO attribution
    is synthesized (Invariant #4 — don't fabricate a claim from a missing name)."""
    conn, db = case
    f = _xml(tmp_path, "noname.xml",
             '<?xml version="1.0"?><sdnList><sdnEntry><uid>1</uid><sdnType>Entity</sdnType>'
             '<programList><program>CYBER2</program></programList><idList><id>'
             f'<idType>Digital Currency Address - XBT</idType><idNumber>{XBT_ADDR}</idNumber>'
             '</id></idList></sdnEntry></sdnList>')
    risk = OfacSdnImporter().get_risk(conn, f)
    attr = OfacSdnImporter().get_attributions(conn, f)
    assert risk["risks"] == 1                                  # the address IS sanctioned -> risk row
    assert attr["attributions"] == 0                           # but no name -> no attribution
    assert conn.execute("SELECT COUNT(*) FROM attribution").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM attribution WHERE label='(unknown)'").fetchone()[0] == 0  # nothing fabricated
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_multi_ticker_same_address_counts_one_row(case, tmp_path):
    """One SDN entry listing the same ETH address under USDC + USDT -> one ethereum address, one risk row;
    the reported count must not overstate (both ERC-20 tickers collapse)."""
    conn, db = case
    f = _xml(tmp_path, "dup.xml",
             '<?xml version="1.0"?><sdnList><sdnEntry><uid>1</uid><lastName>DUP CO</lastName>'
             '<sdnType>Entity</sdnType><programList><program>CYBER2</program></programList><idList>'
             f'<id><idType>Digital Currency Address - USDC</idType><idNumber>{ETH_DISPLAY}</idNumber></id>'
             f'<id><idType>Digital Currency Address - USDT</idType><idNumber>{ETH_DISPLAY}</idNumber></id>'
             '</idList></sdnEntry></sdnList>')
    res = OfacSdnImporter().get_risk(conn, f)
    assert res["risks"] == 1
    assert conn.execute("SELECT COUNT(*) FROM risk_assessment WHERE source='ofac-sdn'").fetchone()[0] == 1
    attr = OfacSdnImporter().get_attributions(conn, f)
    assert attr["attributions"] == 1
    assert conn.execute("SELECT COUNT(*) FROM attribution WHERE source='ofac-sdn'").fetchone()[0] == 1
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_out_of_order_edition_does_not_false_delist(case):
    """Re-ingesting an OLDER SDN edition after a newer one is detected as stale, so still-current
    addresses (e.g. the ARB entry only in the newer file) are NOT mislabeled as delisted."""
    conn, db = case
    OfacSdnImporter().get_risk(conn, FIX / "ofac_sdn_delisted.xml")     # newer (06/27): DOE, STABLE, ARB
    res_old = OfacSdnImporter().get_risk(conn, FIX / "ofac_sdn.xml")    # older (06/26): MIXER, DOE, STABLE
    assert res_old["stale_edition"] is True and res_old["delisted"] == []
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_no_publish_date_endpoint_fallback(case, tmp_path):
    conn, db = case
    f = _xml(tmp_path, "nodate.xml",
             '<?xml version="1.0"?><sdnList><sdnEntry><uid>1</uid><lastName>X CO</lastName>'
             '<sdnType>Entity</sdnType><idList><id>'
             f'<idType>Digital Currency Address - XBT</idType><idNumber>{XBT_ADDR}</idNumber>'
             '</id></idList></sdnEntry></sdnList>')
    res = OfacSdnImporter().get_risk(conn, f)
    assert res["stale_edition"] is False
    row = conn.execute("SELECT endpoint, params FROM source_query WHERE capability='get_risk'").fetchone()
    assert row["endpoint"] == "sdn.xml"  # fallback when the file carries no Publish_Date
    import json
    assert json.loads(row["params"])["sdn_publish_date"] is None
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_adapter_entity_name_gated_on_sdn_type():
    # An Entity that erroneously carries a firstName must render the org name, not 'ACME, Inc'.
    entity = (b'<sdnList><sdnEntry><uid>1</uid><firstName>Inc</firstName><lastName>ACME</lastName>'
              b'<sdnType>Entity</sdnType><idList><id><idType>Digital Currency Address - ETH</idType>'
              b'<idNumber>0x8589427373D6D84E98730D7795D8f6f8731FDA16</idNumber></id></idList></sdnEntry></sdnList>')
    s_entity, _ = adapt_sdn_xml(entity)
    assert s_entity[0].entity_name == "ACME"  # Entity -> lastName only
    s_indiv, _ = adapt_sdn_xml(entity.replace(b"<sdnType>Entity</sdnType>", b"<sdnType>Individual</sdnType>"))
    assert s_indiv[0].entity_name == "ACME, Inc"  # Individual -> LAST, First


# --- Phase B: sanctioned-entity attribution --------------------------------------------------

def test_phase_b_emits_sanctioned_entity_attribution(case):
    conn, db = case
    res = OfacSdnImporter().get_attributions(conn, FIX / "ofac_sdn.xml")
    assert res["attributions"] == 3  # one per sanctioned BTC/EVM address with an entity name
    row = conn.execute(
        """SELECT at.label, at.category, at.source, at.note, at.confidence, a.chain
           FROM attribution at JOIN address a ON a.id=at.address_id WHERE at.label='EVIL MIXER LLC'""").fetchone()
    assert row["category"] == "sanctioned_entity" and row["source"] == "ofac-sdn"
    assert row["chain"] == "bitcoin" and row["confidence"] is None  # authoritative, no invented score
    assert "CYBER2" in row["note"]
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_phase_b_attribution_idempotent(case):
    conn, db = case
    OfacSdnImporter().get_attributions(conn, FIX / "ofac_sdn.xml")
    OfacSdnImporter().get_attributions(conn, FIX / "ofac_sdn.xml")
    assert conn.execute("SELECT COUNT(*) FROM attribution WHERE source='ofac-sdn'").fetchone()[0] == 3
    assert all(r.passed for r in run_audits(db_path=str(db)))


# --- pure-adapter unit tests -----------------------------------------------------------------

def test_adapter_ticker_map_and_skip():
    sanctions, notes = adapt_sdn_xml((FIX / "ofac_sdn.xml").read_bytes())
    assert notes["entries"] == 4 and notes["digital_currency_ids"] == 4  # XBT, ETH, USDT, XMR
    assert notes["sanctions"] == 3 and len(notes["skipped_unsupported"]) == 1
    assert notes["skipped_unsupported"][0]["ticker"] == "XMR"
    assert notes["publish_date"] == "06/26/2026" and notes["errors"] == []
    # ERC-20 tickers resolve to ethereum.
    assert TICKER_TO_CHAIN["USDT"] == "ethereum" and TICKER_TO_CHAIN["XBT"] == "bitcoin"
    assert {s.chain for s in sanctions} == {"bitcoin", "ethereum"}


def test_adapter_skips_unsupported_before_canonicalizing():
    # An invalid non-EVM/BTC address on an unsupported ticker must be skipped, not raise.
    xml = (b'<sdnList><sdnEntry><uid>1</uid><lastName>Z</lastName><idList>'
           b'<id><idType>Digital Currency Address - LTC</idType><idNumber>not!a!valid!addr</idNumber></id>'
           b'</idList></sdnEntry></sdnList>')
    sanctions, notes = adapt_sdn_xml(xml)
    assert sanctions == [] and notes["errors"] == []
    assert notes["skipped_unsupported"][0]["ticker"] == "LTC"
