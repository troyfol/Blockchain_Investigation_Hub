"""MisTrack API connector — gating + pure-mapper LOGIC tests (paid; docs/findings/
misttrack_reconciliation.md). NO live key / NO fabricated cassette; the wire shape is validated by the
RUN_LIVE drift test. These guard the score-scale (3-100, not 0-100) + the raw risk_detail breakdown.
"""

from __future__ import annotations

import pytest

from backend.app.connectors.base import ConnectorError
from backend.app.connectors.misttrack import MisTrackConnector
from backend.app.normalization.misttrack_adapter import adapt_labels, adapt_risk, coin_for
from backend.tests.integration._helpers import new_case


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="MisTrack API")
    yield conn, db
    conn.close()


def test_no_key_raises_naming_the_keyring_entry(case):
    conn, _ = case
    c = MisTrackConnector(api_key="")
    with pytest.raises(ConnectorError) as exc:
        c.get_risk(conn, "ethereum", "0x52908400098527886e0f7030069857d2e4169ee7")
    c.close()
    assert "misttrack_api_key" in str(exc.value)


def test_unsupported_chain_coin_raises(case):
    conn, _ = case
    c = MisTrackConnector(api_key="k")  # keyed, but the chain has no coin mapping
    with pytest.raises(ConnectorError) as exc:
        c.get_risk(conn, "solana", "whatever")
    c.close()
    assert "coin mapping" in str(exc.value)


def test_coin_map():
    assert coin_for("ethereum") == "ETH" and coin_for("bitcoin") == "BTC" and coin_for("bsc") == "BNB"
    assert coin_for("nope") is None


def test_api_key_never_recorded_in_provenance_params():
    """Inv #1/#3: the api_key is sent as a query param but must NEVER land in the persisted
    source_query.params (which is written to the case DB on disk)."""
    c = MisTrackConnector(api_key="SUPERSECRETKEY")
    sq = c._sq("get_risk", "v2/risk_score", "ETH", "0xabc", "ethereum", "2026-01-01T00:00:00Z", "x")
    c.close()
    assert "api_key" not in sq.params and "SUPERSECRETKEY" not in str(sq.params)
    assert sq.params["bounds"] == "default"  # still satisfies the bounds-recorded audit


def test_numeric_score_change_is_a_new_side_by_side_row(case):
    """Inv #6/#7: a re-fetch returning a DIFFERENT numeric score (same category/rationale) is captured as
    a new side-by-side row, not silently dropped; an identical re-fetch stays idempotent."""
    from backend.app.db import repository as repo
    from backend.app.models import Address, RiskAssessment, SourceQuery
    from backend.app.provenance.atomic import write_with_provenance
    from backend.app.audits.runner import run_audits
    conn, db = case
    addr = "0x52908400098527886e0f7030069857d2e4169ee7"

    def seed(score):
        sq = SourceQuery(connector="misttrack-api", capability="get_risk", endpoint="v2/risk_score",
                         params={"address": addr, "coin": "ETH", "bounds": "default"},
                         requested_at="2026-01-01T00:00:00Z", status="ok")

        def w(c, sqid):
            aid = repo.upsert_address(c, Address(chain="ethereum", address_display=addr), sqid)
            repo.upsert_risk_assessment(c, RiskAssessment(
                address_id=aid, score=score, score_scale="3-100", category="mixer",
                rationale="High | mixer:x", source="misttrack", retrieved_at="2026-01-01T00:00:00Z"), sqid)
        write_with_provenance(conn, sq, w)

    seed(60.0)
    seed(60.0)  # identical re-fetch -> idempotent
    assert conn.execute("SELECT COUNT(*) FROM risk_assessment").fetchone()[0] == 1
    seed(85.0)  # re-scored, same category/rationale -> a NEW side-by-side row (corrected score captured)
    scores = sorted(r[0] for r in conn.execute("SELECT score FROM risk_assessment").fetchall())
    assert scores == [60.0, 85.0]
    assert all(r.passed for r in run_audits(db_path=str(db)))


# --- pure-mapper logic (synthetic input; confirmed schema) ------------------------------------

def test_risk_scale_is_3_100_and_breakdown_raw():
    r = adapt_risk({"score": 85, "risk_level": "High", "detail_list": ["Involved Illicit Activity"],
                    "risk_detail": [
                        {"risk_type": "mixer", "entity": "tornado", "exposure_type": "direct",
                         "hop_num": 1, "volume": 1000, "percent": 80},
                        {"risk_type": "gambling", "entity": "x", "exposure_type": "indirect",
                         "hop_num": 3, "volume": 10, "percent": 5}]})
    assert r.score == 85.0 and r.score_scale == "3-100"          # NOT 0-100
    assert r.category == "mixer"                                 # primary by percent
    assert "High" in r.rationale and "Involved Illicit Activity" in r.rationale
    assert "mixer:tornado" in r.rationale and "gambling:x" in r.rationale  # full nested breakdown kept


def test_risk_no_score_returns_none():
    assert adapt_risk({"risk_level": "Low"}).score is None
    assert adapt_risk("nope") is None


def test_labels_entity_plus_tags():
    labs = adapt_labels({"label_list": ["Binance", "hot"], "label_type": "exchange"})
    assert len(labs) == 1 and labs[0].label == "Binance" and labs[0].category == "exchange"
    assert "hot" in labs[0].note
    assert adapt_labels({"label_list": []}) == []  # no labels -> nothing
