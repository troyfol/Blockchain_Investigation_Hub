"""Valuation precision (Phase 5, docs/algorithms.md §3).

``value = unit_price × (amount_base_units / 10^decimals)``, computed with Decimal at 38 significant
digits and quantized to 18 decimal places, banker's rounding (ROUND_HALF_EVEN). No float anywhere
— values round-trip exactly. Inputs/outputs are strings (stored as TEXT).
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, localcontext

QUANTUM = Decimal("1e-18")  # 18 decimal places


def compute_value(amount_base_units: str | int, decimals: int, unit_price: str) -> str:
    """USD value of a movement. Returns a fixed-point decimal string (18 places).

    Precision is scaled to the inputs (floor 38 per docs/algorithms.md §3) so the product is
    computed EXACTLY and the 18-place quantize never overflows the context — realistic values use
    ~38 digits; only implausibly large magnitudes need more. Result is exact, no float drift.
    """
    amount = Decimal(str(amount_base_units))
    price = Decimal(str(unit_price))
    with localcontext() as ctx:
        ctx.prec = max(38, len(amount.as_tuple().digits) + len(price.as_tuple().digits) + 40)
        human_amount = amount / (Decimal(10) ** decimals)
        value = (price * human_amount).quantize(QUANTUM, rounding=ROUND_HALF_EVEN)
        return format(value, "f")  # plain fixed-point, never scientific notation
