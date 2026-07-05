"""Side-by-side valuation display (FN-03).

Surfaces ALL valuations for a movement, grouped by source and kept side-by-side — never collapsed into
one number or an average (Invariant #4). A movement priced by >1 source is ``contested``; each source's
price + confidence + retrieval time + provenance is shown, never merged. Mirrors ``claims_display`` for
attribution/risk. Read-only.
"""

from __future__ import annotations

_COLUMNS = ("source", "currency", "unit_price", "value", "price_timestamp", "confidence",
            "retrieved_at", "source_query_id")


def movement_valuations(conn, subject_id: str) -> dict:
    """Every ``valuation`` for a movement (``subject_id`` = transfer.id | tx_output.id), grouped by source
    and kept side-by-side. ``contested`` is True when >1 distinct source priced it. There is deliberately
    NO averaged/combined/winner value anywhere — disagreement is preserved, shown, never resolved."""
    rows = conn.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM valuation WHERE subject_id=? ORDER BY source, retrieved_at",
        (subject_id,)).fetchall()
    by_source: dict = {}
    for r in rows:
        by_source.setdefault(r["source"], []).append({k: r[k] for k in _COLUMNS})
    return {
        "subject_id": subject_id,
        "valuations_by_source": by_source,
        "contested": len(by_source) > 1,  # >1 SOURCE priced it — shown side-by-side, never averaged
        # No "value"/"combined"/"averaged" key — multi-source valuations are NEVER collapsed (Invariant #4).
    }
