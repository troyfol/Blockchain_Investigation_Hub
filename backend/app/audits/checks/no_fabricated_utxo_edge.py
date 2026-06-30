"""No-fabricated-UTXO-edge audit (docs/testing.md §2 #5; Invariant #5).

Bitcoin never records which input funded which output — that ambiguity *is* the UTXO tracing
problem. ``v_value_movement`` therefore projects UTXO rows with ``src_address_id`` deliberately
NULL. If any UTXO row has a non-NULL src, something fabricated an input->output edge as a fact.
This check is Invariant #5 as a query that must return 0.
"""

from __future__ import annotations

from .. import AuditContext, AuditResult, audit_check


@audit_check("no-fabricated-utxo-edge")
def check_no_fabricated_utxo_edge(ctx: AuditContext) -> AuditResult:
    bad = ctx.conn.execute(
        "SELECT movement_id FROM v_value_movement "
        "WHERE paradigm='utxo' AND src_address_id IS NOT NULL"
    ).fetchall()
    return AuditResult(
        "no-fabricated-utxo-edge",
        passed=not bad,
        offending=[{"movement_id": r["movement_id"]} for r in bad],
    )
