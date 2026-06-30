"""GraphSense TagPack import — Phase A (attributions) contract + adapter tests.

GraphSense TagPacks are free/open YAML attribution tags (docs/findings/graphsense_tagpack_reconciliation.md).
This is the free attribution pillar that fills the `attribution` capability orphaned by the Arkham re-scope.
Covered: header→tag inheritance, a BTC + an ETH tag, confidence id→level/100 mapping (header default +
per-tag override), an unsupported-currency skip (never canonicalized), `header: !include`, idempotent
re-ingest (Invariant #7), and a malformed-tag clean error (all-or-nothing).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.audits.runner import run_audits
from backend.app.connectors.base import ConnectorError
from backend.app.connectors.imports.graphsense import GraphSenseImporter
from backend.app.normalization.graphsense_adapter import (
    CONFIDENCE_LEVELS,
    adapt_tagpack,
    confidence_to_float,
)
from backend.tests.integration._helpers import new_case

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "imports"
BTC_ADDR = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
ETH_DISPLAY = "0x52908400098527886E0F7030069857D2E4169EE7"
ETH_CANON = ETH_DISPLAY.lower()


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="GraphSense")
    yield conn, db
    conn.close()


# --- contract tests over the TagPack fixtures ------------------------------------------------

def test_real_tagpack_maps_to_attributions(case):
    conn, db = case
    res = GraphSenseImporter().get_attributions(conn, FIX / "graphsense_tagpack.yaml")

    # 2 supported tags (BTC, ETH) -> attributions; the LTC row is skipped + reported, not canonicalized.
    assert res["attributions"] == 2
    assert res["skipped_unsupported"] == 1 and res["unsupported_currencies"] == ["LTC"]
    assert res["abuse_tags"] == 1 and res["actor_tags"] == 1
    assert conn.execute("SELECT COUNT(*) FROM attribution").fetchone()[0] == 2

    # BTC tag inherits header source/confidence/category. confidence forensic_investigation -> 70 -> 0.70.
    btc = conn.execute(
        """SELECT at.label, at.category, at.confidence, at.note, a.address, a.chain
           FROM attribution at JOIN address a ON a.id=at.address_id WHERE a.chain='bitcoin'""").fetchone()
    assert btc["label"] == "Sample Exchange Hot Wallet" and btc["category"] == "exchange"
    assert btc["confidence"] == pytest.approx(0.70)
    assert btc["address"] == BTC_ADDR  # base58 untouched
    assert "confidence: forensic_investigation" in btc["note"]
    assert "source: https://example.org/research/sample-tagpack" in btc["note"]

    # ETH tag overrides confidence (ownership=100 -> 1.0) + category; address canonicalized (lowercased).
    eth = conn.execute(
        """SELECT at.label, at.category, at.confidence, at.note, at.source,
                  a.address, a.address_display, a.chain
           FROM attribution at JOIN address a ON a.id=at.address_id WHERE a.chain='ethereum'""").fetchone()
    assert eth["category"] == "organization" and eth["confidence"] == pytest.approx(1.0)
    assert eth["source"] == "graphsense"
    assert eth["address"] == ETH_CANON and eth["address_display"] == ETH_DISPLAY
    assert "confidence: ownership" in eth["note"]

    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_unsupported_currency_never_canonicalized(case):
    conn, db = case
    GraphSenseImporter().get_attributions(conn, FIX / "graphsense_tagpack.yaml")
    # The Litecoin address (L…) must not have been created — it never reached canonical_address.
    assert conn.execute("SELECT COUNT(*) FROM address WHERE address LIKE 'L%'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM address").fetchone()[0] == 2  # BTC + ETH only


def test_header_include_inheritance(case):
    conn, db = case
    res = GraphSenseImporter().get_attributions(conn, FIX / "graphsense_with_include.yaml")
    assert res["attributions"] == 1  # header (currency/source/confidence/category) came from !include

    row = conn.execute(
        """SELECT at.category, at.confidence, at.note, a.chain
           FROM attribution at JOIN address a ON a.id=at.address_id""").fetchone()
    assert row["chain"] == "bitcoin" and row["category"] == "organization"
    assert row["confidence"] == pytest.approx(0.10)  # heuristic = 10
    assert "source: https://example.org/shared-header" in row["note"]
    assert "confidence: heuristic" in row["note"]
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_reingest_is_idempotent(case):
    conn, db = case
    GraphSenseImporter().get_attributions(conn, FIX / "graphsense_tagpack.yaml")
    GraphSenseImporter().get_attributions(conn, FIX / "graphsense_tagpack.yaml")  # same file
    assert conn.execute("SELECT COUNT(*) FROM attribution").fetchone()[0] == 2  # no dupes (Inv #7)
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_distinct_backlink_kept_side_by_side(case, tmp_path):
    """Same (address,label) from two different TagPacks (different source backlinks) -> two rows,
    never merged (Invariant #4)."""
    conn, db = case
    a = tmp_path / "pack_a.yaml"
    b = tmp_path / "pack_b.yaml"
    a.write_text("title: A\ncreator: t\ncurrency: BTC\nsource: https://a.example/x\n"
                 f"tags:\n  - address: {BTC_ADDR}\n    label: Shared Label\n", encoding="utf-8")
    b.write_text("title: B\ncreator: t\ncurrency: BTC\nsource: https://b.example/y\n"
                 f"tags:\n  - address: {BTC_ADDR}\n    label: Shared Label\n", encoding="utf-8")
    GraphSenseImporter().get_attributions(conn, a)
    GraphSenseImporter().get_attributions(conn, b)
    rows = conn.execute("SELECT note FROM attribution WHERE label='Shared Label'").fetchall()
    assert len(rows) == 2 and {r["note"] for r in rows} == {
        "source: https://a.example/x", "source: https://b.example/y"}
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_malformed_supported_chain_tag_is_clean_error(case, tmp_path):
    conn, db = case
    bad = tmp_path / "bad.yaml"
    # ETH currency but a non-hex address -> hard error on a supported chain (all-or-nothing).
    bad.write_text("title: Bad\ncreator: t\nsource: https://x\n"
                   "tags:\n  - address: 0xNOTHEX\n    label: Bad\n    currency: ETH\n", encoding="utf-8")
    with pytest.raises(ConnectorError) as exc:
        GraphSenseImporter().get_attributions(conn, bad)
    assert "unparseable" in str(exc.value)
    for table in ("attribution", "address", "source_query"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # nothing written
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_not_a_mapping_is_rejected(case, tmp_path):
    conn, db = case
    bad = tmp_path / "list.yaml"
    bad.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ConnectorError):
        GraphSenseImporter().get_attributions(conn, bad)


# --- Phase B: abuse -> categorical risk ------------------------------------------------------

def test_abuse_tag_writes_categorical_risk(case):
    conn, db = case
    res = GraphSenseImporter().get_risk(conn, FIX / "graphsense_tagpack.yaml")
    # Only the ETH tag carries abuse=scam; BTC has none, LTC is skipped.
    assert res["risks"] == 1
    row = conn.execute(
        """SELECT r.score, r.score_scale, r.category, r.rationale, r.source, a.chain
           FROM risk_assessment r JOIN address a ON a.id=r.address_id""").fetchone()
    assert row["category"] == "scam" and row["source"] == "graphsense"
    assert row["score"] is None and row["score_scale"] is None  # categorical only — no invented score
    assert row["chain"] == "ethereum"
    assert "Sample Exchange Deposit" in row["rationale"]
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_risk_reingest_is_idempotent(case):
    conn, db = case
    GraphSenseImporter().get_risk(conn, FIX / "graphsense_tagpack.yaml")
    GraphSenseImporter().get_risk(conn, FIX / "graphsense_tagpack.yaml")
    assert conn.execute("SELECT COUNT(*) FROM risk_assessment").fetchone()[0] == 1  # no dupes (Inv #7)
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_attributions_and_risk_share_no_collapse(case):
    """get_attributions and get_risk are independent passes; the abuse tag yields BOTH an attribution
    and a categorical risk, each with its own provenance — never collapsed into a blended score."""
    conn, db = case
    GraphSenseImporter().get_attributions(conn, FIX / "graphsense_tagpack.yaml")
    GraphSenseImporter().get_risk(conn, FIX / "graphsense_tagpack.yaml")
    assert conn.execute("SELECT COUNT(*) FROM attribution").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM risk_assessment").fetchone()[0] == 1
    assert all(r.passed for r in run_audits(db_path=str(db)))


# --- Phase C: actors -> entities + memberships -----------------------------------------------

def test_actorpack_creates_source_entities(case):
    conn, db = case
    res = GraphSenseImporter().get_entities(conn, FIX / "graphsense_actorpack.yaml")
    assert res["actors"] == 1 and res["entities_created"] == 1
    row = conn.execute(
        "SELECT name, entity_type, origin, external_id FROM entity").fetchone()
    assert row["name"] == "Sample Exchange Inc." and row["entity_type"] == "exchange"
    assert row["origin"] == "source" and row["external_id"] == "sample_exchange"
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_tag_actor_creates_membership(case):
    conn, db = case
    GraphSenseImporter().get_entities(conn, FIX / "graphsense_actorpack.yaml")     # define the actor
    res = GraphSenseImporter().get_entities(conn, FIX / "graphsense_tagpack.yaml")  # link tag -> actor
    assert res["memberships"] == 1  # only the BTC tag carries actor=sample_exchange

    m = conn.execute(
        """SELECT m.source, m.method, m.confidence, m.flags, a.chain, a.address, e.external_id, e.name
           FROM entity_membership m JOIN address a ON a.id=m.address_id
           JOIN entity e ON e.id=m.entity_id""").fetchone()
    assert m["source"] == "graphsense" and m["method"] == "tagpack-actor"
    assert m["flags"] == "cluster-definer"                       # is_cluster_definer: true -> flags
    assert m["confidence"] == pytest.approx(0.70)                # the tag's (inherited) confidence
    assert m["chain"] == "bitcoin" and m["address"] == BTC_ADDR
    assert m["external_id"] == "sample_exchange" and m["name"] == "Sample Exchange Inc."
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_entity_membership_is_order_independent(case):
    """Ingesting the TagPack BEFORE the ActorPack still lands on ONE entity (a stub by id, upgraded
    to the real label when the ActorPack arrives) — actor id resolution is order-independent."""
    conn, db = case
    GraphSenseImporter().get_entities(conn, FIX / "graphsense_tagpack.yaml")    # creates a stub entity
    stub = conn.execute("SELECT name FROM entity WHERE external_id='sample_exchange'").fetchone()
    assert stub["name"] == "sample_exchange"                    # stub name defaults to the actor id
    GraphSenseImporter().get_entities(conn, FIX / "graphsense_actorpack.yaml")  # upgrades the stub
    rows = conn.execute("SELECT name, entity_type FROM entity WHERE external_id='sample_exchange'").fetchall()
    assert len(rows) == 1 and rows[0]["name"] == "Sample Exchange Inc."  # ONE entity, name upgraded
    assert rows[0]["entity_type"] == "exchange"
    assert conn.execute("SELECT COUNT(*) FROM entity_membership").fetchone()[0] == 1
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_phase_c_idempotent_reingest(case):
    conn, db = case
    for _ in range(2):
        GraphSenseImporter().get_entities(conn, FIX / "graphsense_actorpack.yaml")
        GraphSenseImporter().get_entities(conn, FIX / "graphsense_tagpack.yaml")
    assert conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM entity_membership").fetchone()[0] == 1
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_get_entities_rejects_non_actorpack_non_tagpack(case, tmp_path):
    conn, db = case
    bad = tmp_path / "neither.yaml"
    bad.write_text("title: Neither\ncreator: t\n", encoding="utf-8")
    with pytest.raises(ConnectorError) as exc:
        GraphSenseImporter().get_entities(conn, bad)
    assert "neither" in str(exc.value).lower()


# --- adversarial-review regression tests (2026-06-28) ----------------------------------------

def _pack(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_same_addr_label_different_category_kept_side_by_side(case, tmp_path):
    """Inv #4: two tags agreeing on (address,label,source,confidence) but DISAGREEING on `category`
    must be two rows — the disagreement rides only in category, which is now in the upsert key."""
    conn, db = case
    p = _pack(tmp_path, "cat.yaml",
              "title: T\ncreator: c\ncurrency: BTC\nsource: https://x\nconfidence: ownership\n"
              f"tags:\n  - address: {BTC_ADDR}\n    label: Acme\n    category: exchange\n"
              f"  - address: {BTC_ADDR}\n    label: Acme\n    category: mixer\n")
    res = GraphSenseImporter().get_attributions(conn, p)
    assert res["attributions"] == 2
    cats = {r["category"] for r in conn.execute(
        "SELECT category FROM attribution WHERE label='Acme'").fetchall()}
    assert cats == {"exchange", "mixer"}  # distinct categorical claims preserved, not collapsed
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_risk_distinct_categories_kept_side_by_side(case, tmp_path):
    """Inv #4 for risk_assessment: two distinct abuse types on one address -> two rows."""
    conn, db = case
    p = _pack(tmp_path, "risk2.yaml",
              "title: T\ncreator: c\ncurrency: ETH\nsource: https://x\n"
              f"tags:\n  - address: '{ETH_DISPLAY}'\n    label: Bad\n    abuse: scam\n"
              f"  - address: '{ETH_DISPLAY}'\n    label: Bad\n    abuse: ransomware\n")
    GraphSenseImporter().get_risk(conn, p)
    cats = {r["category"] for r in conn.execute("SELECT category FROM risk_assessment").fetchall()}
    assert cats == {"scam", "ransomware"}
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_membership_distinct_entities_kept_side_by_side(case, tmp_path):
    """Inv #4 for entity_membership: one address linked to two different actors -> two memberships."""
    conn, db = case
    p = _pack(tmp_path, "tags2.yaml",
              "title: T\ncreator: c\ncurrency: BTC\nsource: https://x\n"
              f"tags:\n  - address: {BTC_ADDR}\n    label: A\n    actor: actor_one\n"
              f"  - address: {BTC_ADDR}\n    label: B\n    actor: actor_two\n")
    res = GraphSenseImporter().get_entities(conn, p)
    assert res["memberships"] == 2
    assert conn.execute("SELECT COUNT(DISTINCT entity_id) FROM entity_membership").fetchone()[0] == 2
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_tag_only_membership_seeds_entity_type_from_category(case):
    """Spec: actor -> Entity(entity_type=category). With NO ActorPack, the stub entity must still get
    entity_type from the tag's (header-inherited) category."""
    conn, db = case
    GraphSenseImporter().get_entities(conn, FIX / "graphsense_tagpack.yaml")
    row = conn.execute(
        "SELECT entity_type FROM entity WHERE external_id='sample_exchange'").fetchone()
    assert row["entity_type"] == "exchange"  # seeded from tag category even without an ActorPack
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_get_risk_malformed_is_all_or_nothing(case, tmp_path):
    conn, db = case
    bad = _pack(tmp_path, "badrisk.yaml",
                "title: T\ncreator: c\nsource: https://x\n"
                "tags:\n  - address: 0xNOTHEX\n    label: Bad\n    currency: ETH\n    abuse: scam\n")
    with pytest.raises(ConnectorError):
        GraphSenseImporter().get_risk(conn, bad)
    for table in ("risk_assessment", "address", "source_query"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_get_entities_membership_malformed_is_all_or_nothing(case, tmp_path):
    conn, db = case
    bad = _pack(tmp_path, "badtags.yaml",
                "title: T\ncreator: c\nsource: https://x\n"
                "tags:\n  - address: 0xNOTHEX\n    label: Bad\n    currency: ETH\n    actor: a1\n")
    with pytest.raises(ConnectorError):
        GraphSenseImporter().get_entities(conn, bad)
    for table in ("entity", "entity_membership", "address", "source_query"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_get_entities_actorpack_malformed_is_all_or_nothing(case, tmp_path):
    conn, db = case
    bad = _pack(tmp_path, "badactors.yaml", "title: T\ncreator: c\nactors:\n  - id: only_id_no_label\n")
    with pytest.raises(ConnectorError):
        GraphSenseImporter().get_entities(conn, bad)
    for table in ("entity", "source_query"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_non_mapping_tag_is_clean_error(case, tmp_path):
    conn, db = case
    bad = _pack(tmp_path, "scalartag.yaml", "title: T\ncreator: c\ncurrency: BTC\ntags:\n  - just-a-string\n")
    with pytest.raises(ConnectorError) as exc:
        GraphSenseImporter().get_attributions(conn, bad)
    assert "unparseable" in str(exc.value)
    for table in ("attribution", "address", "source_query"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_include_missing_target_is_clean_error(case, tmp_path):
    conn, db = case
    p = _pack(tmp_path, "badinc.yaml", "header: !include nonexistent.yaml\ntags: []\n")
    with pytest.raises(ConnectorError) as exc:
        GraphSenseImporter().get_attributions(conn, p)
    assert "include target not found" in str(exc.value)


def test_include_cycle_is_bounded_not_stack_overflow(case, tmp_path):
    conn, db = case
    p = _pack(tmp_path, "selfinc.yaml", "header: !include selfinc.yaml\ntags: []\n")
    with pytest.raises(ConnectorError) as exc:
        GraphSenseImporter().get_attributions(conn, p)
    assert "nesting too deep" in str(exc.value)  # cycle guard fires, not a RecursionError


def test_include_path_escape_is_rejected(case, tmp_path):
    conn, db = case
    p = _pack(tmp_path, "escape.yaml", "header: !include ../../../etc/passwd\ntags: []\n")
    with pytest.raises(ConnectorError) as exc:
        GraphSenseImporter().get_attributions(conn, p)
    assert "unsafe" in str(exc.value).lower()  # parent-escape (..) rejected before any file read


def test_non_dict_included_header_fails_loud(case, tmp_path):
    conn, db = case
    _pack(tmp_path, "listheader.yaml", "- a\n- b\n")
    p = _pack(tmp_path, "withlistheader.yaml",
              f"header: !include listheader.yaml\ntags:\n  - address: {BTC_ADDR}\n    label: X\n    currency: BTC\n")
    with pytest.raises(ConnectorError) as exc:
        GraphSenseImporter().get_attributions(conn, p)
    assert "header" in str(exc.value).lower()  # malformed included header fails loud, not silent skip


def test_include_provenance_recorded_in_source_query(case):
    conn, db = case
    GraphSenseImporter().get_attributions(conn, FIX / "graphsense_with_include.yaml")
    params = json.loads(conn.execute("SELECT params FROM source_query").fetchone()["params"])
    incs = params.get("includes")
    assert incs and incs[0]["file"] == "graphsense_header.yaml"
    assert len(incs[0]["sha256"]) == 64  # the included header's hash is captured (Invariant #3)
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_abuse_without_source_rationale_is_bare_label(case, tmp_path):
    conn, db = case
    p = _pack(tmp_path, "abuse_nosrc.yaml",
              "title: T\ncreator: c\ncurrency: BTC\n"
              f"tags:\n  - address: {BTC_ADDR}\n    label: ScamWallet\n    abuse: scam\n")
    GraphSenseImporter().get_risk(conn, p)
    GraphSenseImporter().get_risk(conn, p)  # idempotent even with a bare-label rationale
    rows = conn.execute("SELECT rationale FROM risk_assessment").fetchall()
    assert len(rows) == 1 and rows[0]["rationale"] == "ScamWallet"
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_membership_flags_null_when_not_cluster_definer(case, tmp_path):
    conn, db = case
    p = _pack(tmp_path, "noncd.yaml",
              "title: T\ncreator: c\ncurrency: BTC\nsource: https://x\n"
              f"tags:\n  - address: {BTC_ADDR}\n    label: A\n    actor: a1\n")  # no is_cluster_definer
    GraphSenseImporter().get_entities(conn, p)
    assert conn.execute("SELECT flags FROM entity_membership").fetchone()["flags"] is None
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_membership_idempotent_and_resolves_to_canonical_after_merge(case, tmp_path):
    """#7 finding: if a source entity is merged away, re-ingesting its TagPack must NOT create a
    duplicate membership (Inv #7) and the membership must still resolve to the CANONICAL entity at
    read time (the owning id is stored; services.entities.resolve chases merged_into)."""
    from backend.app.db import repository as repo
    from backend.app.models import Entity
    from backend.app.services.entities import merge_entities, resolve
    conn, db = case
    p = _pack(tmp_path, "merge.yaml",
              "title: T\ncreator: c\ncurrency: BTC\nsource: https://x\n"
              f"tags:\n  - address: {BTC_ADDR}\n    label: A\n    actor: act_m\n")
    GraphSenseImporter().get_entities(conn, p)
    e1 = conn.execute("SELECT id FROM entity WHERE external_id='act_m'").fetchone()["id"]
    e2 = repo.insert_entity(conn, Entity(origin="investigator", name="Merge Target"))
    merge_entities(conn, into_id=e2, from_id=e1)  # tombstone e1 -> e2
    GraphSenseImporter().get_entities(conn, p)    # re-ingest: must stay idempotent
    rows = conn.execute("SELECT entity_id FROM entity_membership").fetchall()
    assert len(rows) == 1                                   # no duplicate membership (Inv #7)
    assert resolve(conn, rows[0]["entity_id"]) == e2        # resolves to the live entity at read time
    assert all(r.passed for r in run_audits(db_path=str(db)))


# --- pure-adapter unit tests -----------------------------------------------------------------

def test_confidence_mapping():
    assert confidence_to_float("ownership") == (1.0, True)
    assert confidence_to_float("forensic_investigation") == (0.70, True)
    assert confidence_to_float("heuristic") == (0.10, True)
    assert confidence_to_float("unknown") == (0.05, True)
    # A confidence id NOT in the taxonomy -> (None, False): never guess a level.
    assert confidence_to_float("not_a_real_id") == (None, False)
    assert confidence_to_float(None) == (None, False)
    assert CONFIDENCE_LEVELS["ledger_immanent"] == 100


def test_adapter_header_inheritance_and_override():
    doc = {"title": "t", "creator": "c", "source": "https://h", "confidence": "heuristic",
           "category": "exchange", "currency": "BTC",
           "tags": [
               {"address": BTC_ADDR, "label": "Inherited"},                       # inherits all
               {"address": ETH_DISPLAY, "label": "Override", "currency": "ETH",
                "confidence": "ownership", "category": "organization"},            # overrides
           ]}
    tags, notes = adapt_tagpack(doc)
    assert notes["attributions"] == 2 and notes["skipped_unsupported"] == []
    inh = next(t for t in tags if t.chain == "bitcoin")
    ovr = next(t for t in tags if t.chain == "ethereum")
    assert inh.category == "exchange" and inh.confidence == pytest.approx(0.10)
    assert ovr.category == "organization" and ovr.confidence == pytest.approx(1.0)


def test_adapter_unknown_confidence_keeps_id_drops_float():
    doc = {"currency": "BTC", "source": "https://h",
           "tags": [{"address": BTC_ADDR, "label": "X", "confidence": "mystery_level"}]}
    tags, notes = adapt_tagpack(doc)
    assert notes["unknown_confidence"] == 1
    assert tags[0].confidence is None and tags[0].confidence_id == "mystery_level"
    assert "confidence: mystery_level" in tags[0].note  # raw id preserved despite unknown level


def test_adapter_skips_unsupported_currency_before_canonicalizing():
    # A malformed non-EVM address must NOT raise — it's classified out by currency first.
    doc = {"tags": [{"address": "garbage!!!", "label": "Z", "currency": "ZEC"}]}
    tags, notes = adapt_tagpack(doc)
    assert tags == [] and notes["skipped_unsupported"][0]["currency"] == "ZEC"
    assert notes["errors"] == []


def test_adapter_counts_abuse_and_actor():
    doc = {"currency": "BTC", "source": "https://h", "tags": [
        {"address": BTC_ADDR, "label": "A", "abuse": "ransomware", "actor": "evil_corp",
         "is_cluster_definer": True}]}
    tags, notes = adapt_tagpack(doc)
    assert notes["abuse_tags"] == 1 and notes["actor_tags"] == 1
    assert tags[0].abuse == "ransomware" and tags[0].actor == "evil_corp" and tags[0].is_cluster_definer


def test_adapter_is_cluster_definer_truthiness():
    # A YAML bool passes through; a *quoted* "false"/"no"/"0" must be False (plain bool() would be True).
    def cd(v):
        t, _ = adapt_tagpack({"currency": "BTC", "tags": [
            {"address": BTC_ADDR, "label": "X", "is_cluster_definer": v}]})
        return t[0].is_cluster_definer
    assert cd(True) is True and cd(False) is False
    assert cd("false") is False and cd("no") is False and cd("0") is False
    assert cd("true") is True and cd(None) is False
