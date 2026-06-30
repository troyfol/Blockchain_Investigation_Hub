"""Property tests: valuation has no float drift and is deterministic (docs/testing.md §4)."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN, localcontext

import pytest
from hypothesis import given, strategies as st

from backend.app.normalization.valuation_math import compute_value

amounts = st.integers(min_value=0, max_value=10 ** 30)
decimals = st.integers(min_value=0, max_value=18)
prices = st.decimals(min_value=0, max_value=Decimal(10) ** 9, places=8,
                     allow_nan=False, allow_infinity=False)


@pytest.mark.property
@given(amount=amounts, dec=decimals, price=prices)
def test_value_equals_pure_decimal(amount, dec, price):
    price_s = format(price, "f")
    got = compute_value(str(amount), dec, price_s)
    with localcontext() as ctx:  # high-prec reference (exact)
        ctx.prec = 80
        expected = (Decimal(price_s) * (Decimal(amount) / Decimal(10) ** dec)).quantize(
            Decimal("1e-18"), rounding=ROUND_HALF_EVEN)
    assert Decimal(got) == expected   # exact — no float drift


@pytest.mark.property
@given(amount=amounts, dec=decimals, price=prices)
def test_deterministic_and_nonnegative(amount, dec, price):
    price_s = format(price, "f")
    a = compute_value(str(amount), dec, price_s)
    assert a == compute_value(str(amount), dec, price_s)   # recomputes identically
    assert Decimal(a) >= 0
