"""Side-by-side claims display (Phase 7 step 5).

Surfaces ALL attributions and ALL risk assessments for an address, grouped by source — the
never-collapse principle made visible (Invariant #4). There is deliberately NO averaged/combined
score or synthesized label anywhere: disagreement between sources is preserved and shown.
"""

from __future__ import annotations


def _group_by_source(rows) -> dict:
    grouped: dict = {}
    for r in rows:
        grouped.setdefault(r["source"], []).append({k: r[k] for k in r.keys()})
    return grouped


def address_claims(conn, address_id: str) -> dict:
    attributions = conn.execute(
        "SELECT * FROM attribution WHERE address_id=? ORDER BY source, retrieved_at", (address_id,)).fetchall()
    risks = conn.execute(
        "SELECT * FROM risk_assessment WHERE address_id=? ORDER BY source, retrieved_at", (address_id,)).fetchall()
    return {
        "address_id": address_id,
        "attributions_by_source": _group_by_source(attributions),
        "risks_by_source": _group_by_source(risks),
        # No "combined"/"averaged" key — multi-source claims are NEVER collapsed (Invariant #4).
    }
