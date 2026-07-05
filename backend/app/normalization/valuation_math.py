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


def unit_price_from_total(total_usd: str, amount_base_units: str | int, decimals: int) -> str | None:
    """Derive the per-unit USD price from a source-reported TOTAL value (e.g. Arkham ``historicalUSD``):
    ``unit_price = total_usd ÷ (amount / 10^decimals)``. The inverse of :func:`compute_value` — used when
    a source states the movement's whole USD value rather than a per-coin price, so the ``valuation`` row
    still carries both fields. Returns ``None`` for a zero amount (no derivable unit price). Decimal,
    half-even, 18 places — same precision policy as :func:`compute_value`."""
    amount = Decimal(str(amount_base_units))
    if amount == 0:
        return None
    total = Decimal(str(total_usd))
    with localcontext() as ctx:
        ctx.prec = max(38, len(total.as_tuple().digits) + len(amount.as_tuple().digits) + 40)
        human_amount = amount / (Decimal(10) ** decimals)
        price = (total / human_amount).quantize(QUANTUM, rounding=ROUND_HALF_EVEN)
        return format(price, "f")
