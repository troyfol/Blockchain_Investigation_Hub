"""Disagreements roster (FN-09).

Surfaces every subject where SOURCES DISAGREE — an address whose sources give different attribution
labels/categories or different risk categories, or a value movement priced differently by different
sources. Each subject is returned with every source's current claim SIDE-BY-SIDE plus the fields that
differ, and a `node_id` so the UI can navigate to it on the canvas.

Invariant #4 is the entire point: this NEVER emits a winner, a consensus, or a merged/averaged value —
adjudication is an explicit investigator finding/annotation, never something the tool computes. There is
deliberately no resolved / combined / averaged field anywhere in the output. Read-only.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation


def _latest_per_source(rows: list) -> dict:
    """The current claim from each source = its latest row. Rows arrive ordered by (retrieved_at, id)
    ascending, so the last one seen per source wins. Reducing to one-per-source means a single source
    revising its OWN claim over time is never mistaken for a cross-source disagreement."""
    latest: dict = {}
    for r in rows:
        latest[r["source"]] = r
    return latest


def _attribution_disagreements(conn) -> list[dict]:
    by_addr: dict[str, list] = defaultdict(list)
    for r in conn.execute(
        "SELECT a.address_id, ad.chain, ad.address, ad.address_display, a.source, a.label, a.category, "
        "a.confidence, a.retrieved_at, a.source_query_id, a.id "
        "FROM attribution a JOIN address ad ON ad.id=a.address_id "
        "ORDER BY a.address_id, a.source, a.retrieved_at, a.id"
    ).fetchall():
        by_addr[r["address_id"]].append(r)
    out: list[dict] = []
    for addr_id, rows in by_addr.items():
        latest = _latest_per_source(rows)
        if len(latest) < 2:
            continue  # a single source can't disagree with anyone
        labels = {c["label"] for c in latest.values()}
        categories = {c["category"] for c in latest.values()}
        fields = [f for f, distinct in (("label", labels), ("category", categories)) if len(distinct) > 1]
        if not fields:
            continue  # multiple sources, but they agree on every captured field
        head = rows[0]
        out.append({
            "subject_type": "address", "subject_id": addr_id, "node_id": f"addr:{addr_id}",
            "chain": head["chain"], "address": head["address"], "address_display": head["address_display"],
            "claim_type": "attribution", "fields": fields, "sources": sorted(latest),
            "claims": [{"source": c["source"], "label": c["label"], "category": c["category"],
                        "confidence": c["confidence"], "retrieved_at": c["retrieved_at"],
                        "source_query_id": c["source_query_id"]}
                       for c in sorted(latest.values(), key=lambda c: c["source"])],
        })
    return out


def _risk_disagreements(conn) -> list[dict]:
    by_addr: dict[str, list] = defaultdict(list)
    for r in conn.execute(
        "SELECT r.address_id, ad.chain, ad.address, ad.address_display, r.source, r.category, r.score, "
        "r.score_scale, r.rationale, r.retrieved_at, r.source_query_id, r.id "
        "FROM risk_assessment r JOIN address ad ON ad.id=r.address_id "
        "ORDER BY r.address_id, r.source, r.retrieved_at, r.id"
    ).fetchall():
        by_addr[r["address_id"]].append(r)
    out: list[dict] = []
    for addr_id, rows in by_addr.items():
        latest = _latest_per_source(rows)
        if len(latest) < 2:
            continue
        categories = {c["category"] for c in latest.values()}
        if len(categories) <= 1:
            continue  # sources agree on category. Scores on DIFFERENT scales aren't comparable, so a
            #           differing score alone is not treated as a disagreement (never a false conflict).
        head = rows[0]
        out.append({
            "subject_type": "address", "subject_id": addr_id, "node_id": f"addr:{addr_id}",
            "chain": head["chain"], "address": head["address"], "address_display": head["address_display"],
            "claim_type": "risk", "fields": ["category"], "sources": sorted(latest),
            "claims": [{"source": c["source"], "category": c["category"], "score": c["score"],
                        "score_scale": c["score_scale"], "rationale": c["rationale"],
                        "retrieved_at": c["retrieved_at"], "source_query_id": c["source_query_id"]}
                       for c in sorted(latest.values(), key=lambda c: c["source"])],
        })
    return out


def _valuation_disagreements(conn) -> list[dict]:
    by_subj: dict[tuple, list] = defaultdict(list)
    for r in conn.execute(
        "SELECT subject_type, subject_id, source, value, unit_price, currency, price_timestamp, "
        "confidence, retrieved_at, source_query_id, id "
        "FROM valuation ORDER BY subject_type, subject_id, source, retrieved_at, id"
    ).fetchall():
        by_subj[(r["subject_type"], r["subject_id"])].append(r)
    out: list[dict] = []
    for (stype, sid), rows in by_subj.items():
        latest = _latest_per_source(rows)
        if len(latest) < 2:
            continue
        values = set()
        for c in latest.values():
            try:
                values.add(Decimal(c["value"]))  # compare NUMERICALLY (100 == 100.0); never merge/average
            except (InvalidOperation, TypeError):
                values.add(c["value"])
        if len(values) <= 1:
            continue  # every source priced it the same — not a disagreement
        # Navigation hint: land on the movement's destination (or source) address node, if any.
        mv = conn.execute(
            "SELECT dst_address_id, src_address_id FROM v_value_movement WHERE movement_id=?", (sid,)
        ).fetchone()
        endpoint = (mv["dst_address_id"] or mv["src_address_id"]) if mv else None
        out.append({
            "subject_type": "movement", "subject_id": sid, "movement_kind": stype,
            "edge_id": f"mv:{sid}", "node_id": (f"addr:{endpoint}" if endpoint else None),
            "claim_type": "valuation", "fields": ["value"], "sources": sorted(latest),
            "claims": [{"source": c["source"], "value": c["value"], "unit_price": c["unit_price"],
                        "currency": c["currency"], "price_timestamp": c["price_timestamp"],
                        "confidence": c["confidence"], "retrieved_at": c["retrieved_at"],
                        "source_query_id": c["source_query_id"]}
                       for c in sorted(latest.values(), key=lambda c: c["source"])],
        })
    return out


def find_disagreements(conn) -> list[dict]:
    """Every cross-source disagreement in the case (attribution label/category, risk category, valuation),
    each with the sources' claims side-by-side + the differing fields + a `node_id` to navigate to.
    Deterministically ordered. NEVER emits a winner or a merged/averaged value (Invariant #4)."""
    out = (_attribution_disagreements(conn) + _risk_disagreements(conn) + _valuation_disagreements(conn))
    out.sort(key=lambda d: (d["claim_type"], d["subject_id"]))  # deterministic (subject_id stable per case)
    return out
