"""Unit tests for finality computation (docs/algorithms.md §2)."""

from __future__ import annotations

from backend.app.config import Settings
from backend.app.normalization.finality import (
    compute_confirmations,
    compute_finality,
    finality_for,
)


def test_confirmations_formula():
    # tip - block + 1
    assert compute_confirmations(1000, 900) == 101
    assert compute_confirmations(1000, 1000) == 1  # in the tip block = 1 confirmation


def test_confirmations_unconfirmed_or_unknown_is_zero():
    assert compute_confirmations(1000, None) == 0   # mempool
    assert compute_confirmations(None, 900) == 0     # unknown tip


def test_confirmations_never_negative():
    # block ahead of tip (transient/reorg) clamps to 0, not negative.
    assert compute_confirmations(900, 1000) == 0


def test_finality_threshold_boundary():
    assert compute_finality(64, 64) == "final"
    assert compute_finality(63, 64) == "provisional"
    assert compute_finality(0, 6) == "provisional"
    assert compute_finality(6, 6) == "final"


def test_finality_for_eth_default():
    # ETH threshold 64: block at tip-63 → 64 confirmations → final; one less → provisional.
    conf, status = finality_for(tip_height=1000, block_height=937, threshold=64)
    assert conf == 64 and status == "final"
    conf, status = finality_for(tip_height=1000, block_height=938, threshold=64)
    assert conf == 63 and status == "provisional"


def test_finality_for_btc_default():
    s = Settings()
    thr = s.finality_threshold("bitcoin")  # 6
    conf, status = finality_for(tip_height=800000, block_height=799995, threshold=thr)
    assert conf == 6 and status == "final"
    conf, status = finality_for(tip_height=800000, block_height=799996, threshold=thr)
    assert conf == 5 and status == "provisional"
