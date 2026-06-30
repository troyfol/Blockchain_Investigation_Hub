"""Unit tests for CoinJoin detection (docs/algorithms.md §5)."""

from __future__ import annotations

import pytest

from backend.app.services.entities import is_probable_coinjoin
from backend.tests.integration._helpers import new_case, seed_btc_custom


@pytest.fixture
def case(tmp_path):
    conn, db = new_case(tmp_path)
    yield conn
    conn.close()


def test_equal_output_pattern_is_coinjoin(case):
    # 5 inputs + 5 equal outputs (Whirlpool 0.001 BTC denom) -> probable CoinJoin.
    tx = seed_btc_custom(case, txid="a" * 64, input_addrs=[f"1cj{i}" for i in range(5)],
                         output_amounts=[100_000] * 5)
    assert is_probable_coinjoin(case, tx) is True


def test_whirlpool_denomination_pattern(case):
    # Many equal outputs at a known pool denomination flags even with the structural test aside.
    tx = seed_btc_custom(case, txid="b" * 64, input_addrs=[f"1w{i}" for i in range(6)],
                         output_amounts=[5_000_000] * 5 + [123])  # 0.05 BTC pool
    assert is_probable_coinjoin(case, tx) is True


def test_ordinary_tx_is_not_coinjoin(case):
    # 2 inputs, 2 distinct outputs -> ordinary spend, not CoinJoin.
    tx = seed_btc_custom(case, txid="c" * 64, input_addrs=["1a", "1b"], output_amounts=[120_000, 79_000])
    assert is_probable_coinjoin(case, tx) is False


def test_few_inputs_equal_outputs_not_flagged(case):
    # Equal outputs but only 2 inputs and below the equal-output threshold.
    tx = seed_btc_custom(case, txid="d" * 64, input_addrs=["1a", "1b"], output_amounts=[50_000, 50_000])
    assert is_probable_coinjoin(case, tx) is False
