"""P8.7 #4 — "Check intel" enriches a case with the free OFAC + GraphSense pillars from the BUNDLED
snapshots, OFFLINE, writing sourced claims (Inv #3/#4) that surface the sanctioned halo + entity ring."""

from __future__ import annotations

import keyring
import pytest
from keyring.backend import KeyringBackend

from backend.app.services.graph import build_graph
from backend.tests.integration._helpers import new_case, seed_btc_custom, seed_evm_address

# The Hydra Market validation anchor — present in the bundled OFAC SDN + GraphSense snapshots.
HYDRA = "16ZSAEfYpPCj3D94fsNt2okYj9Ue8mxy6T"
# The Tornado Cash anchor (a real OFAC SDN designation; delisted 2025-03-21 but in the dated bundled
# edition). OFAC publishes it CHECKSUMMED; a case stores it lowercase — the case-insensitive match (#2).
TORNADO_CHECKSUMMED = "0x722122dF12D4e14e13Ac3b6895a86e84145b6967"
GARANTEX_ETH = "0x7FF9cFad3877F21d41Da833E2F775dB0569eE3D9"


class _MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self._s = {}

    def get_password(self, s, u):
        return self._s.get((s, u))

    def set_password(self, s, u, p):
        self._s[(s, u)] = p

    def delete_password(self, s, u):
        self._s.pop((s, u), None)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BIH_APP_DATA_DIR", str(tmp_path / "appdata"))
    prev = keyring.get_keyring()
    keyring.set_keyring(_MemoryKeyring())  # no chainalysis key -> that pillar is skipped
    from backend.app.services import settings_store

    settings_store.set_offline(True)  # prove the BUNDLED snapshots work with NO network
    yield
    settings_store.set_offline(False)
    keyring.set_keyring(prev)


def test_check_intel_surfaces_ofac_and_graphsense_offline(tmp_path, isolated):
    from backend.app.services.intel import check_intel

    conn, _db = new_case(tmp_path, title="Hydra Case")
    # the sanctioned Hydra anchor enters the case as a real on-chain participant (a tx input)
    seed_btc_custom(conn, txid="a" * 64, input_addrs=[HYDRA], output_amounts=[50_000])

    res = check_intel(conn)  # offline — reads the bundled snapshots only
    assert "ofac-sdn" in res["sources"] and "graphsense" in res["sources"]
    assert "chainalysis" not in res["sources"]            # skipped (no key / offline)
    assert res["ofac"]["sanctioned"] >= 1
    assert res["ofac"]["snapshot_date"] and res["graphsense"]["snapshot_date"]

    # the OFAC sanctioned CLAIM attached to the Hydra address
    rk = conn.execute(
        "SELECT r.category, r.source FROM risk_assessment r JOIN address a ON a.id=r.address_id "
        "WHERE a.address=?", (HYDRA,)).fetchone()
    assert rk and rk["category"] == "sanctioned" and rk["source"] == "ofac-sdn"
    # the GraphSense entity membership (side-by-side second source — never merged)
    ent = conn.execute(
        "SELECT e.name FROM entity_membership m JOIN entity e ON e.id=m.entity_id "
        "JOIN address a ON a.id=m.address_id WHERE a.address=?", (HYDRA,)).fetchone()
    assert ent and "hydra" in ent["name"].lower()

    # the read-model now renders the overlay on the node (red sanctioned halo + entity ring/label)
    g = build_graph(conn)
    node = next(n for n in g["nodes"] if n.get("address") == HYDRA)
    assert node["risk_level"] == "sanctioned"
    assert "hydra" in (node.get("entity_label") or "").lower()


def test_check_intel_is_idempotent(tmp_path, isolated):
    from backend.app.services.intel import check_intel

    conn, _db = new_case(tmp_path, title="Hydra Case")
    seed_btc_custom(conn, txid="b" * 64, input_addrs=[HYDRA], output_amounts=[50_000])
    check_intel(conn)
    before = conn.execute("SELECT COUNT(*) FROM risk_assessment").fetchone()[0]
    check_intel(conn)  # re-run -> upsert, no duplicate sanctioned rows
    after = conn.execute("SELECT COUNT(*) FROM risk_assessment").fetchone()[0]
    assert after == before


def test_check_intel_does_not_inject_unmatched_snapshot_addresses(tmp_path, isolated):
    """P8.7.1 #1 (the false-positive guard): a case with NO snapshot address gets ZERO attribution — no
    injected snapshot addresses, no phantom 'hydramarket' entity, nothing in the report Entities table."""
    from backend.app.services import reporting
    from backend.app.services.intel import check_intel
    from backend.tests.integration._helpers import seed_btc_custom

    conn, _db = new_case(tmp_path, title="Vitalik Case")
    # a real on-chain participant that is NOT in the OFAC/GraphSense snapshots
    seed_btc_custom(conn, txid="c" * 64, input_addrs=["bc1q7cyrfmck2ffu2ud3rn5l5a8yv6f0chkp0zpemf"],
                    output_amounts=[10_000])

    res = check_intel(conn)
    assert res["graphsense"]["attributions"] == 0 and res["graphsense"]["memberships"] == 0
    assert res["ofac"]["sanctioned"] == 0 and res["ofac"]["attributions"] == 0

    # the two BUNDLED Hydra addresses were NOT injected into this case
    for hydra in (HYDRA, "1MQBDeRWsiJBf7K1VGjJ7PWEL6GJXMfmLg"):
        assert conn.execute("SELECT COUNT(*) FROM address WHERE address=?", (hydra,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM attribution WHERE source='graphsense'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM risk_assessment WHERE source='ofac-sdn'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM entity WHERE name LIKE '%hydra%'").fetchone()[0] == 0
    # ...and the report's Entities table is empty (no phantom Hydra entity)
    assert reporting._collect_entities(conn) == []


def test_snapshot_info_reports_dates(tmp_path, isolated):
    from backend.app.services.intel import snapshot_info

    info = snapshot_info()
    assert info["ofac"]["exists"] and info["ofac"]["date"]
    assert info["graphsense"]["exists"] and info["graphsense"]["date"]
    assert info["ofac"]["override"] is False  # using the bundled snapshot


# --------------------------------------------------------------------------- P8.7.3 #1 real bundled intel

@pytest.mark.smoke
def test_bundled_ofac_snapshot_is_real_and_nontrivial():
    """P8.7.3 #1 guard: the app's BUNDLED OFAC snapshot must be a real, non-trivial SDN crypto subset
    (NOT the tiny validation fixture) and contain known sanctioned addresses (Tornado + Garantex), so the
    app screens real-world designations out of the box. Case-insensitive on the EVM addresses (#2)."""
    from backend.app.normalization.ofac_adapter import adapt_sdn_xml
    from backend.app.services.intel import ofac_path

    sanctions, notes = adapt_sdn_xml(ofac_path().read_bytes())
    assert not notes["errors"]
    # non-trivial: clearly more than the old 3-address toy (multiple entities, multiple chains)
    assert len(sanctions) >= 15
    assert notes["entries"] >= 5
    chains = {s.chain for s in sanctions}
    assert "ethereum" in chains and "bitcoin" in chains
    canon = {s.address_canonical.lower() for s in sanctions}
    assert TORNADO_CHECKSUMMED.lower() in canon                     # the Tornado anchor screens
    assert GARANTEX_ETH.lower() in canon                           # Garantex screens
    names = {s.entity_name.upper() for s in sanctions}
    assert any("TORNADO" in n for n in names) and any("GARANTEX" in n for n in names)


@pytest.mark.smoke
def test_bundled_graphsense_snapshot_is_real_and_multi_entity():
    """The bundled GraphSense pack must carry real multi-entity attributions (not Hydra-only)."""
    from backend.app.connectors.imports.graphsense import GraphSenseImporter
    from backend.app.normalization.graphsense_adapter import adapt_tagpack
    from backend.app.services.intel import graphsense_path

    doc, _inc = GraphSenseImporter()._load_doc(graphsense_path())
    tags, notes = adapt_tagpack(doc)
    assert not notes["errors"] and len(tags) >= 8
    labels = {t.label for t in tags}
    assert {"Tornado Cash", "Garantex"} <= labels and len(labels) >= 5


def test_check_intel_screens_tornado_case_insensitive(tmp_path, isolated):
    """P8.7.3 #1+#2: the Tornado anchor — present in the real bundled OFAC snapshot, published CHECKSUMMED
    — screens as sanctioned for a case that stored it lowercase (canonical), proving the EVM match is
    case-insensitive on BOTH sides. The GraphSense entity label lands side-by-side."""
    from backend.app.services.intel import check_intel

    conn, _db = new_case(tmp_path, title="Tornado Case")
    # the case ingested it in CHECKSUMMED display form; address.address is canonical lowercase
    seed_evm_address(conn, TORNADO_CHECKSUMMED)
    canon = TORNADO_CHECKSUMMED.lower()

    res = check_intel(conn)
    assert res["ofac"]["sanctioned"] >= 1

    rk = conn.execute(
        "SELECT r.category, r.source FROM risk_assessment r JOIN address a ON a.id=r.address_id "
        "WHERE a.address=?", (canon,)).fetchone()
    assert rk and rk["category"] == "sanctioned" and rk["source"] == "ofac-sdn"

    g = build_graph(conn)
    node = next(n for n in g["nodes"] if n.get("address") == canon)
    assert node["risk_level"] == "sanctioned"
    assert "tornado" in (node.get("entity_label") or "").lower()   # GraphSense attribution, case-insensitive


def test_check_intel_evm_match_is_case_insensitive_both_directions(tmp_path, isolated):
    """The match canonicalizes both sides: a case address stored LOWERCASE still matches the SDN's
    CHECKSUMMED form (and a non-listed clean address still screens nothing — the false-positive guard)."""
    from backend.app.services.intel import check_intel

    conn, _db = new_case(tmp_path, title="Mixed Case")
    seed_evm_address(conn, GARANTEX_ETH.lower())                   # lowercase in the case
    seed_evm_address(conn, "0x" + "ab" * 20)                       # a clean, non-listed address

    check_intel(conn)
    listed = conn.execute(
        "SELECT category FROM risk_assessment r JOIN address a ON a.id=r.address_id WHERE a.address=?",
        (GARANTEX_ETH.lower(),)).fetchone()
    assert listed and listed["category"] == "sanctioned"
    clean = conn.execute(
        "SELECT COUNT(*) FROM risk_assessment r JOIN address a ON a.id=r.address_id WHERE a.address=?",
        ("0x" + "ab" * 20,)).fetchone()[0]
    assert clean == 0                                             # not listed -> no fabricated sanction


def test_report_risk_section_lists_sanctioned_side_by_side(tmp_path, isolated):
    """P8.7.3 retest — the report has an explicit Risk/Sanctions section: every screened sanctioned address
    + its source (OFAC SDN), with multiple sources kept SIDE-BY-SIDE (Inv #4), distinct from the Entities
    (GraphSense attribution) section. This is the headline of a sanctions screen and must appear."""
    from backend.app.db import repository as repo
    from backend.app.models import RiskAssessment, SourceQuery
    from backend.app.provenance.atomic import write_with_provenance
    from backend.app.services import reporting
    from backend.app.services.intel import check_intel

    conn, _db = new_case(tmp_path, title="Risk")
    aid = seed_evm_address(conn, TORNADO_CHECKSUMMED)
    check_intel(conn)                                            # writes the OFAC sanctioned claim
    canon = TORNADO_CHECKSUMMED.lower()

    # a SECOND sanctions source on the SAME address — must be kept side-by-side, never merged (Inv #4)
    sq = SourceQuery(connector="chainalysis", capability="get_risk", endpoint="sanctions",
                     params={"bounds": "default"}, requested_at="2026-01-01T00:00:00Z", status="ok")
    write_with_provenance(conn, sq, lambda c, sqid: repo.upsert_risk_assessment(
        c, RiskAssessment(address_id=aid, score=None, score_scale=None, category="sanctioned",
                          source="chainalysis", rationale="Chainalysis sanctions match",
                          retrieved_at="2026-01-01T00:00:00Z"), sqid))

    risk = reporting._collect_risk(conn)
    g = next(g for g in risk if g["address"] == canon)
    assert g["sanctioned"] and risk[0] is g                      # sanctioned address sorts FIRST
    sources = {c["source"] for c in g["claims"]}
    assert {"ofac-sdn", "chainalysis"} <= sources               # BOTH sources, side-by-side (Inv #4)

    section = reporting._risk_section(risk)
    assert "screened as SANCTIONED" in section
    assert "ofac-sdn" in section and "chainalysis" in section   # both sources rendered, not collapsed
    assert "pill sanctioned" in section and canon[:10] in section
    # distinct from Entities: an empty risk screen has its own explicit empty message
    assert "No sanctions or risk claims" in reporting._risk_section([])
