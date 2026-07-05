"""FN-15 (P20): structured per-sub-signal risk detail rows.

A `risk_assessment` carries ONE headline score + dominant category, but a paid intel source (Arkham,
MisTrack) reports MANY per-category sub-signals (hacker/mixer/sanctions/…). Those were flattened into
`risk_assessment.rationale` (an un-queryable blob). P20 promotes each sub-signal to a first-class RAW
`risk_detail` row — never collapsed/averaged (Invariant #4), written in the parent's txn with its own
provenance (Invariant #3), idempotent on re-ingest (Invariant #7).
"""

from __future__ import annotations

import pytest

from backend.app.audits.runner import run_audits
from backend.app.connectors.arkham import ArkhamApiConnector
from backend.app.db import repository as repo
from backend.app.models import RiskAssessment, RiskDetail, SourceQuery
from backend.app.normalization.arkham_api_adapter import adapt_risk as adapt_arkham_risk
from backend.app.normalization.misttrack_adapter import adapt_risk as adapt_misttrack_risk
from backend.app.provenance.atomic import write_with_provenance
from backend.tests.integration._helpers import new_case

ADDR = "0x52908400098527886e0f7030069857d2e4169ee7"


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="Risk detail")
    yield conn, db
    conn.close()


def test_arkham_categories_become_detail_rows(case, monkeypatch):
    conn, db = case
    c = ArkhamApiConnector(api_key="test-key")  # a key so the gate opens; NO network (monkeypatched)
    payload = {"max_score": 82, "risk_level": "HIGH", "greatest_risk_category": "mixer",
               "mixer_score": 82, "sanctions_score": 40, "ransomware_score": 15, "hacker_score": 0}
    monkeypatch.setattr(c, "_get", lambda path: payload)
    res = c.get_risk(conn, "ethereum", ADDR)
    c.close()

    assert res["risks"] == 1
    rows = conn.execute("SELECT signal, score, score_scale FROM risk_detail").fetchall()
    got = {r["signal"]: (r["score"], r["score_scale"]) for r in rows}
    # each NON-zero per-category score is its own first-class row (zero-valued 'hacker' omitted, parity
    # with the rationale breakdown); each raw, none collapsed/averaged.
    assert got == {"mixer": (82.0, "0-100"), "sanctions": (40.0, "0-100"), "ransomware": (15.0, "0-100")}
    # the headline risk_assessment is unchanged (rationale still carries the breakdown — back-compat).
    ra = conn.execute("SELECT rationale FROM risk_assessment").fetchone()
    assert "mixer=82" in ra["rationale"] and "sanctions=40" in ra["rationale"]
    # every risk_detail references its parent + is provenance-complete → the audits stay green.
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_arkham_adapter_maps_categories_to_details():
    r = adapt_arkham_risk({"max_score": 82, "risk_level": "HIGH", "greatest_risk_category": "mixer",
                           "mixer_score": 82, "sanctions_score": 40, "hacker_score": 0})
    got = {d.signal: d.score for d in r.details}
    assert got == {"mixer": 82.0, "sanctions": 40.0}  # zero/absent categories excluded
    # the flattened rationale is still produced (a summary alongside the queryable rows), not removed.
    assert "mixer=82" in r.rationale


def test_arkham_risk_detail_reingest_is_idempotent(case, monkeypatch):
    conn, db = case
    payload = {"max_score": 60, "risk_level": "MED", "greatest_risk_category": "mixer",
               "mixer_score": 60, "sanctions_score": 30}
    for _ in range(2):  # re-fetch the SAME risk twice
        c = ArkhamApiConnector(api_key="k")
        monkeypatch.setattr(c, "_get", lambda path: payload)
        c.get_risk(conn, "ethereum", ADDR)
        c.close()
    assert conn.execute("SELECT COUNT(*) FROM risk_assessment").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM risk_detail").fetchone()[0] == 2  # mixer + sanctions, no dupes
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_two_sources_sub_signals_kept_side_by_side(case):
    conn, db = case
    # Arkham (0-100) and MisTrack (3-100) both report a 'mixer' sub-signal on the SAME address: two parents
    # -> two risk_detail rows, different scales, NEVER merged/averaged into one (Invariant #4).
    addr_id = None

    def w(c, sqid):
        nonlocal addr_id
        from backend.app.models import Address
        addr_id = repo.upsert_address(c, Address(chain="ethereum", address_display=ADDR), sqid)
        for source, scale, score in (("arkham-api", "0-100", 82.0), ("misttrack", "3-100", 40.0)):
            ra_id = repo.upsert_risk_assessment(c, RiskAssessment(
                address_id=addr_id, score=score, score_scale=scale, category="mixer",
                rationale="mixer", source=source, retrieved_at="2026-01-01T00:00:00Z"), sqid)
            repo.insert_risk_detail(c, RiskDetail(
                risk_assessment_id=ra_id, signal="mixer", score=score, score_scale=scale), sqid)

    sq = SourceQuery(connector="etherscan", capability="get_risk", endpoint="x",
                     params={"bounds": "default"}, requested_at="2026-01-01T00:00:00Z", status="ok")
    write_with_provenance(conn, sq, w)

    mixers = conn.execute("SELECT score, score_scale FROM risk_detail WHERE signal='mixer' ORDER BY score").fetchall()
    assert [(m["score"], m["score_scale"]) for m in mixers] == [(40.0, "3-100"), (82.0, "0-100")]
    assert all(r.passed for r in run_audits(db_path=str(db)))


def test_misttrack_risk_detail_scaffolded():
    # Gated on a key for LIVE use, but the mapping LOGIC is synthetic-testable now (like P18 Bitquery):
    # each nested risk_detail[] entry -> one sub-signal row (signal=risk_type). Field names TODO: confirm.
    r = adapt_misttrack_risk({"score": 55, "risk_level": "MED", "risk_detail": [
        {"risk_type": "mixer", "entity": "Tornado", "percent": 60, "volume": 1000, "hop_num": 1},
        {"risk_type": "sanctions", "entity": "OFAC", "percent": 25, "volume": 500, "hop_num": 2}]})
    got = {d.signal: d.score for d in r.details}
    assert got == {"mixer": 60.0, "sanctions": 25.0}  # percent as the sub-signal score (TODO: confirm)
