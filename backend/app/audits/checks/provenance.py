"""Provenance audits (docs/testing.md §2 #1 and #8).

#1 provenance-completeness: every Family A fact and Family B claim has a resolvable
   ``source_query_id``, except investigator-authored attribution/membership/retraction
   (``source='investigator'``), whose provenance may be NULL.
#8 cache-provenance-carried: no claim points at a ``source_query`` missing from this case.db
   (a cache copy must bring the originating query).
"""

from __future__ import annotations

from .. import AuditContext, AuditResult, audit_check

FACT_TABLES = ["asset", "address", "transaction_", "transfer", "tx_output", "tx_input"]
CLAIM_TABLES = [
    "attribution", "risk_assessment", "valuation", "balance_snapshot",
    "entity_membership", "entity_membership_retraction",
]
# Claim tables whose investigator-authored rows (source='investigator') may have NULL provenance.
INVESTIGATOR_EXEMPT = {"attribution", "entity_membership", "entity_membership_retraction"}


def _dangling_sq(table: str) -> str:
    return (
        f"(source_query_id IS NOT NULL AND NOT EXISTS "
        f"(SELECT 1 FROM source_query sq WHERE sq.id = {table}.source_query_id))"
    )


@audit_check("provenance-completeness")
def check_provenance_completeness(ctx: AuditContext) -> AuditResult:
    conn = ctx.conn
    offending: list[dict] = []

    for table in FACT_TABLES:  # table names are from a fixed whitelist
        rows = conn.execute(
            f"SELECT id FROM {table} WHERE source_query_id IS NULL OR {_dangling_sq(table)}"
        ).fetchall()
        offending += [
            {"table": table, "id": r["id"], "reason": "fact missing/unresolvable source_query_id"}
            for r in rows
        ]

    for table in CLAIM_TABLES:
        if table in INVESTIGATOR_EXEMPT:
            null_clause = "(source_query_id IS NULL AND source <> 'investigator')"
        else:
            null_clause = "source_query_id IS NULL"
        rows = conn.execute(
            f"SELECT id FROM {table} WHERE {null_clause} OR {_dangling_sq(table)}"
        ).fetchall()
        offending += [
            {"table": table, "id": r["id"], "reason": "claim missing/unresolvable source_query_id"}
            for r in rows
        ]

    return AuditResult("provenance-completeness", passed=not offending, offending=offending)


@audit_check("cache-provenance-carried")
def check_cache_provenance_carried(ctx: AuditContext) -> AuditResult:
    conn = ctx.conn
    offending: list[dict] = []
    for table in CLAIM_TABLES:
        rows = conn.execute(
            f"SELECT id, source_query_id FROM {table} WHERE {_dangling_sq(table)}"
        ).fetchall()
        offending += [
            {"table": table, "id": r["id"], "source_query_id": r["source_query_id"]} for r in rows
        ]
    return AuditResult("cache-provenance-carried", passed=not offending, offending=offending)
