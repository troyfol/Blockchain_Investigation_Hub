"""Unit tests for valuation Decimal precision (docs/algorithms.md §3)."""

from __future__ import annotations

from decimal import Decimal

from backend.app.normalization.valuation_math import compute_value


def test_one_eth_at_price():
    # 1 ETH (1e18 wei, 18 decimals) at $2000.50.
    assert compute_value("1000000000000000000", 18, "2000.5") == "2000.500000000000000000"


def test_btc_satoshis():
    # 120000 sats (8 decimals) = 0.0012 BTC at $60000 = $72.
    assert compute_value("120000", 8, "60000") == "72.000000000000000000"


def test_fractional_token_amount():
    # 1500000 units of a 6-decimal token (1.5) at $0.999.
    assert compute_value("1500000", 6, "0.999") == "1.498500000000000000"


def test_half_even_rounding_at_18_places():
    # A value needing rounding at the 18th place uses banker's rounding (ROUND_HALF_EVEN).
    # 1 unit (0 decimals) * price 0.0000000000000000005 -> rounds half-even to ...000 (even).
    v = compute_value("1", 0, "0.0000000000000000005")
    assert v == "0.000000000000000000"  # 5e-19 rounds to 0 (nearest even at 1e-18)


def test_no_scientific_notation():
    # Tiny non-zero value renders as plain fixed-point, never '1E-18'.
    v = compute_value("1", 0, "0.000000000000000001")
    assert v == "0.000000000000000001" and "e" not in v.lower()


def test_recomputes_exactly():
    v = compute_value("123456789", 9, "1234.56789")
    # Exact Decimal recomputation matches (no float drift).
    expected = (Decimal("1234.56789") * (Decimal("123456789") / Decimal(10) ** 9))
    assert Decimal(v) == expected.quantize(Decimal("1e-18"))
