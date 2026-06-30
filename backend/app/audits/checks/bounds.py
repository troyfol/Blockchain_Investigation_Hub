"""Bounds-recorded audit (docs/testing.md §2 #10).

Every ``source_query`` for an address-scoped capability must carry a ``params`` JSON that
includes the applied expansion bounds (or an explicit ``"bounds":"default"``), so the partiality
of a pull is reproducible. Imports record ``"bounds":"default"`` (no expansion).
"""

from __future__ import annotations

import json

from .. import AuditContext, AuditResult, audit_check

ADDRESS_SCOPED_CAPABILITIES = {"get_transactions", "get_balance", "get_attributions", "get_risk"}


@audit_check("bounds-recorded")
def check_bounds_recorded(ctx: AuditContext) -> AuditResult:
    offending = []
    for r in ctx.conn.execute("SELECT id, capability, params FROM source_query").fetchall():
        if r["capability"] not in ADDRESS_SCOPED_CAPABILITIES:
            continue
        try:
            params = json.loads(r["params"]) if r["params"] else {}
        except (json.JSONDecodeError, TypeError):
            params = {}
        if "bounds" not in params:
            offending.append({"source_query": r["id"], "capability": r["capability"],
                              "reason": "params missing 'bounds'"})
    return AuditResult("bounds-recorded", passed=not offending, offending=offending)
