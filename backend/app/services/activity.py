"""Case activity timeline (P24/FN-14) — one time-ordered log of everything that happened to a case.

A read-only aggregation of every timestamped event across the case into a single chronological log: data
FETCHES (each `source_query`), and the investigator's own constructions — traces, findings, annotations,
tags, trace retractions/edits, cross-chain bridge links, exhibits, and generated reports. It feeds the
chain-of-custody narrative (pairs with P2's custody appendix and P23's re-fetch diff).

**Granularity decision.** Events are emitted at a MEANINGFUL activity grain, not one-per-fact:
- Data acquisition is emitted once per `source_query` (a `fetch` event). This deliberately covers valuation
  and attribution/risk ENRICHMENT too — a DeFiLlama pricing run, an Arkham/MisTrack pull, and an Etherscan
  ingest are each ONE `source_query`, so they appear as fetch events. Emitting one event per priced movement
  or per attribution row would flood a real case's timeline with thousands of near-simultaneous rows; those
  per-claim rows already live in the facts + the P2 custody appendix (grouped by `source_query`).
- Each investigator object (trace/finding/annotation/tag/bridge link/retraction/exhibit/report) is one event
  (these are low-volume, deliberate actions — the point of the timeline).

**Read-only** (Invariant: none at risk): every query is a SELECT; nothing is written. **Deterministic**:
sorted by `(ts, kind, ref_id)` so equal-timestamp events never reorder across renders (mirrors P15's
exhibit-numbering determinism), and a report can cite a stable ordering.
"""

from __future__ import annotations


def _clip(text, limit: int = 80) -> str | None:
    """Trim a free-text field for a one-line timeline summary (None/empty -> None)."""
    s = (text or "").strip()
    if not s:
        return None
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _event(ts, kind: str, summary: str, ref_type: str, ref_id: str, detail=None) -> dict:
    return {"ts": ts, "kind": kind, "summary": summary, "ref_type": ref_type, "ref_id": ref_id,
            "detail": detail}


def case_activity(conn) -> list[dict]:
    """Return the case's activity as a single time-ordered list of ``{ts, kind, summary, ref_type, ref_id,
    detail}`` events. Read-only; deterministically ordered by ``(ts, kind, ref_id)``."""
    events: list[dict] = []

    # --- data acquisition: one event per source_query (covers ingest + valuation/enrichment fetches) ------
    for r in conn.execute(
        "SELECT id, connector, capability, endpoint, requested_at, status FROM source_query"
    ).fetchall():
        note = f" · {r['status']}" if r["status"] and r["status"] != "ok" else ""
        events.append(_event(r["requested_at"], "fetch",
                             f"Fetched {r['connector']} · {r['capability']}{note}",
                             "source_query", r["id"], detail=_clip(r["endpoint"])))

    # --- investigator constructions (Family C) — each a deliberate, low-volume action -------------------
    for r in conn.execute("SELECT id, name, created_at FROM trace").fetchall():
        events.append(_event(r["created_at"], "trace", f"Trace created: {r['name']}", "trace", r["id"]))

    for r in conn.execute("SELECT id, statement, created_at FROM finding").fetchall():
        events.append(_event(r["created_at"], "finding", "Finding recorded", "finding", r["id"],
                             detail=_clip(r["statement"])))

    for r in conn.execute("SELECT id, target_type, created_at FROM annotation").fetchall():
        events.append(_event(r["created_at"], "annotation", f"Annotation added to {r['target_type']}",
                             "annotation", r["id"]))

    for r in conn.execute("SELECT id, target_type, label, created_at FROM tag").fetchall():
        events.append(_event(r["created_at"], "tag", f"Tag '{r['label']}' added to {r['target_type']}",
                             "tag", r["id"]))

    # trace edits: the two append-only retraction tables (P9) + the cross-chain bridge links (P12)
    for r in conn.execute("SELECT id, created_at FROM trace_transfer_retraction").fetchall():
        events.append(_event(r["created_at"], "trace_edit", "Trace transfer link retracted",
                             "trace_transfer_retraction", r["id"]))
    for r in conn.execute("SELECT id, created_at FROM trace_btc_link_retraction").fetchall():
        events.append(_event(r["created_at"], "trace_edit", "Trace BTC link retracted",
                             "trace_btc_link_retraction", r["id"]))
    for r in conn.execute("SELECT id, created_at FROM trace_bridge_link").fetchall():
        events.append(_event(r["created_at"], "bridge_link", "Cross-chain bridge link added",
                             "trace_bridge_link", r["id"]))

    for r in conn.execute("SELECT id, exhibit_type, captured_at, description FROM exhibit").fetchall():
        events.append(_event(r["captured_at"], "exhibit", f"Exhibit captured ({r['exhibit_type']})",
                             "exhibit", r["id"], detail=_clip(r["description"])))

    for r in conn.execute("SELECT id, title, generated_at FROM report").fetchall():
        events.append(_event(r["generated_at"], "report", f"Report generated: {r['title']}",
                             "report", r["id"]))

    # Deterministic order: chronological, then a stable (kind, ref_id) tiebreak so equal-ts events never
    # reorder across renders (a court exhibit must be reproducible).
    events.sort(key=lambda e: (e["ts"] or "", e["kind"], e["ref_id"]))
    return events
