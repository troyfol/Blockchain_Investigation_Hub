"""Provenance drill-through (FN-01).

Serializes one ``source_query`` row so the UI/report can show WHERE any displayed fact or claim came
from — the connector, capability, endpoint, params/bounds, retrieval time, and the raw-response hash —
making the provenance spine (Invariant #3) visible in a single interaction. Read-only: this reads the
spine, it never writes or collapses anything.
"""

from __future__ import annotations

import json

# Fixed column set (no user input) — safe to interpolate into the SELECT.
_COLUMNS = (
    "id", "connector", "capability", "endpoint", "params", "requested_at",
    "completed_at", "status", "raw_response_ref", "raw_response_hash", "result_summary",
)


def source_query(conn, source_query_id: str) -> dict | None:
    """Return the full provenance record for ``source_query_id``, or ``None`` if unknown in this case.

    ``params`` is stored as JSON text; it is parsed back to an object for display, falling back to the
    raw string if it somehow isn't valid JSON — never silently dropped."""
    row = conn.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM source_query WHERE id=?", (source_query_id,)
    ).fetchone()
    if row is None:
        return None
    out = {k: row[k] for k in _COLUMNS}
    if out.get("params") is not None:
        try:
            out["params"] = json.loads(out["params"])
        except (TypeError, ValueError):
            pass  # keep the raw string rather than lose provenance
    return out
