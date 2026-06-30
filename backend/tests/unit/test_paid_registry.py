"""Optional paid-connector registry — gating (docs/findings/paid_api_integrations.md).

A paid source is available ONLY when its config flag is on AND its key is in the keyring; otherwise it is
silently absent and never blocks the free baseline (Invariant #4). No live keys / no fabricated responses.
"""

from __future__ import annotations

import keyring
import pytest

from backend.app.config import Settings
from backend.app.connectors.registry import (
    PAID_SPECS,
    available_fact_connectors,
    available_intel_connectors,
    paid_status,
)


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    """paid_status consults the runtime settings overlay (settings_store, an app-data JSON) for the
    enabled flag (P5). Point app-data at a temp dir so gating is deterministic regardless of the dev
    machine's saved settings.json — mirrors how empty_keyring forces the keyring empty."""
    monkeypatch.setenv("BIH_APP_DATA_DIR", str(tmp_path / "appdata"))


@pytest.fixture
def empty_keyring(monkeypatch):
    """Force the keyring empty so gating is deterministic regardless of the dev machine's stored keys."""
    monkeypatch.setattr(keyring, "get_password", lambda service, name: None)


def test_all_paid_disabled_by_default(empty_keyring):
    st = {s["name"]: s for s in paid_status(Settings())}
    assert set(st) == {"bitquery", "arkham-api", "misttrack-api", "oklink"}
    assert all((not s["enabled"]) and (not s["available"]) for s in st.values())
    assert available_fact_connectors(Settings()) == []
    assert available_intel_connectors(Settings()) == []


def test_enabled_but_no_key_is_still_absent(empty_keyring):
    s = Settings(bitquery_enabled=True, arkham_api_enabled=True)
    st = {x["name"]: x for x in paid_status(s)}
    assert st["bitquery"]["enabled"] and not st["bitquery"]["has_key"]
    assert not st["bitquery"]["available"]      # enabled but unkeyed -> absent
    assert available_fact_connectors(s) == []   # nothing wired into the orchestrator


def test_enabled_and_keyed_becomes_available(empty_keyring, monkeypatch):
    monkeypatch.setenv("BIH_ALLOW_PLAINTEXT_KEYS", "1")  # use the loud plaintext opt-in for the key
    monkeypatch.setenv("BIH_SECRET_BITQUERY_TOKEN", "t")
    monkeypatch.setenv("BIH_SECRET_ARKHAM_API_KEY", "k")
    s = Settings(bitquery_enabled=True, arkham_api_enabled=True, misttrack_enabled=False)
    st = {x["name"]: x for x in paid_status(s)}
    assert st["bitquery"]["available"] and st["arkham-api"]["available"]
    assert not st["misttrack-api"]["available"]  # disabled -> absent even though it could be keyed

    facts = available_fact_connectors(s)
    try:
        assert [c.name for c in facts] == ["bitquery"]  # Bitquery is the only paid FACT connector
    finally:
        for c in facts:
            c.close()
    intel = available_intel_connectors(s)
    try:
        assert {c.name for c in intel} == {"arkham-api"}  # misttrack disabled, oklink unkeyed
    finally:
        for c in intel:
            c.close()


def test_spec_kinds_and_capabilities():
    by = {s["name"]: s for s in PAID_SPECS}
    assert by["bitquery"]["kind"] == "fact" and "get_transactions" in by["bitquery"]["capabilities"]
    for n in ("arkham-api", "misttrack-api", "oklink"):
        assert by[n]["kind"] == "intel" and {"get_risk", "get_attributions"} == by[n]["capabilities"]
