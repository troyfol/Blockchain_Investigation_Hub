"""Unit tests for app config + per-chain finality thresholds (phase_00 step 3)."""

from __future__ import annotations

from backend.app.config import Settings


def test_defaults_load():
    s = Settings()
    assert s.etherscan_enabled is True
    assert s.esplora_enabled is True
    assert s.defillama_enabled is True
    assert s.allow_plaintext_keys is False
    # LOG-08: cache_ttl_days / etherscan_paid_tier were removed (dead keys, no readers).
    assert not hasattr(s, "cache_ttl_days")
    assert not hasattr(s, "etherscan_paid_tier")


def test_finality_thresholds_defaults():
    s = Settings()
    # Settled convention (docs/schema.md §2).
    assert s.finality_threshold("bitcoin") == 6
    assert s.finality_threshold("ethereum") == 64


def test_finality_threshold_by_chainid():
    s = Settings()
    assert s.finality_threshold(1) == 64        # ethereum mainnet
    assert s.finality_threshold(42161) == 20    # arbitrum (placeholder)


def test_finality_threshold_unknown_chain_is_conservative():
    s = Settings()
    # Unknown chain falls back to the strict default rather than 0.
    assert s.finality_threshold("dogecoin") == 64
    assert s.finality_threshold(999999) == 64


def test_env_override(monkeypatch):
    monkeypatch.setenv("BIH_ETHERSCAN_ENABLED", "0")
    s = Settings()
    assert s.etherscan_enabled is False


def test_partial_finality_override_keeps_settled_defaults(monkeypatch):
    # A partial override must NOT drop the settled bitcoin/ethereum thresholds (Invariant #6).
    monkeypatch.setenv("BIH_FINALITY_THRESHOLDS", '{"polygon": 200}')
    s = Settings()
    assert s.finality_threshold("polygon") == 200   # override applied
    assert s.finality_threshold("bitcoin") == 6      # default preserved
    assert s.finality_threshold("ethereum") == 64     # default preserved
