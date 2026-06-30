"""Arkham API connector — gating + pure-mapper LOGIC tests (Phase B paid; docs/findings/
paid_api_integrations.md §3).

NO live key / NO fabricated HTTP cassette (per the build directive — the wire shape is validated by the
RUN_LIVE drift test). These are: (a) the key-gated no-key guard, and (b) pure-mapper logic tests over the
CONFIRMED Address/RiskScore schema using synthetic minimal inputs — they guard the Invariant #4
"never collapse confirmed vs predicted" rule + the raw risk-breakdown, NOT the wire format.
"""

from __future__ import annotations

import pytest

from backend.app.connectors.arkham import ArkhamApiConnector
from backend.app.connectors.base import ConnectorError
from backend.app.normalization.arkham_api_adapter import (
    CONFIRMED_CONFIDENCE,
    PREDICTED_CONFIDENCE,
    adapt_address,
    adapt_risk,
    entity_key,
)
from backend.tests.integration._helpers import new_case


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path, title="Arkham API")
    yield conn, db
    conn.close()


def test_no_key_raises_naming_the_keyring_entry(case):
    conn, _ = case
    c = ArkhamApiConnector(api_key="")
    with pytest.raises(ConnectorError) as exc:
        c.get_attributions(conn, "ethereum", "0x52908400098527886e0f7030069857d2e4169ee7")
    c.close()
    assert "arkham_api_key" in str(exc.value)


# --- pure-mapper logic (synthetic input; confirmed schema) ------------------------------------

def test_predicted_never_collapsed_into_confirmed():
    """Inv #4: a confirmed `arkhamEntity` and a probabilistic `predictedEntity` become SEPARATE
    entities at different confidence; userEntity/userLabel (caller's private) are ignored."""
    plan = adapt_address({
        "arkhamEntity": {"id": "e1", "name": "Binance", "type": "cex"},
        "predictedEntity": {"id": "e2", "name": "Kraken", "type": "cex"},
        "arkhamLabel": {"name": "Cold Wallet"},
        "depositServiceID": "binance",
        "userEntity": {"name": "MY PRIVATE NOTE"},
        "userLabel": {"name": "my private tag"},
    })
    ents = {e.method: e for e in plan.entities}
    assert ents["arkham-entity"].name == "Binance" and ents["arkham-entity"].confidence == CONFIRMED_CONFIDENCE
    assert ents["arkham-predicted"].name == "Kraken" and ents["arkham-predicted"].confidence == PREDICTED_CONFIDENCE
    assert ents["arkham-entity"].confidence > ents["arkham-predicted"].confidence  # not collapsed

    labels = {a.label: a for a in plan.attributions}
    assert "Cold Wallet" in labels and "binance" in labels and "Kraken" in labels
    assert "MY PRIVATE NOTE" not in labels and "my private tag" not in labels  # private labels ignored
    assert labels["Kraken"].confidence == PREDICTED_CONFIDENCE and "predicted" in labels["Kraken"].note
    assert labels["binance"].category == "deposit_service"


def test_risk_keeps_category_breakdown_raw():
    r = adapt_risk({"max_score": 82, "risk_level": "HIGH", "greatest_risk_category": "mixer",
                    "mixer_score": 82, "sanctions_score": 40, "hacker_score": 0, "hop_distance": 2})
    assert r.score == 82.0 and r.score_scale == "0-100" and r.category == "mixer"
    assert "HIGH" in r.rationale and "mixer=82" in r.rationale and "sanctions=40" in r.rationale
    assert "hacker" not in r.rationale  # zero-valued categories omitted, real ones kept raw (Inv #4)


def test_entity_key_keeps_confirmed_and_predicted_distinct_without_ids():
    """The WRITE-path Inv #4 guard: with NO ids, the entity resolution key embeds the method, so a
    confirmed and a predicted entity sharing a name resolve to TWO entities, never one."""
    plan = adapt_address({"arkhamEntity": {"name": "Binance", "type": "cex"},
                          "predictedEntity": {"name": "Binance", "type": "cex"}})
    keys = {entity_key(e) for e in plan.entities}
    assert len(keys) == 2  # distinct -> find_or_create_source_entity lands on two rows, never collapsed
    assert entity_key(adapt_address({"arkhamEntity": {"id": "abc", "name": "X"}}).entities[0]) == "abc"


def test_adapt_handles_empty_and_nondict():
    assert adapt_address({}).entities == [] and adapt_address({}).attributions == []
    assert adapt_address([]).entities == []            # non-dict -> empty, no crash
    assert adapt_risk({}).score is None
    assert adapt_risk("nope") is None
