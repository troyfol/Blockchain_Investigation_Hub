"""Entity-resolution-sanity audit (docs/testing.md §2 #7).

No cycle in ``entity.merged_into`` (resolution must terminate); and ``canonical_membership_id``,
when set, references a membership whose entity resolves (through any merged_into chain) to this
entity (docs/schema.md §5 app-enforced ref).
"""

from __future__ import annotations

from .. import AuditContext, AuditResult, audit_check


def _resolve(conn, entity_id):
    """Follow merged_into to the terminal id; returns (resolved_id, cycle_detected)."""
    seen = set()
    cur = entity_id
    while cur is not None:
        if cur in seen:
            return cur, True
        seen.add(cur)
        row = conn.execute("SELECT merged_into FROM entity WHERE id=?", (cur,)).fetchone()
        if row is None:
            return cur, False
        cur = row["merged_into"]
    return None, False


@audit_check("entity-resolution-sanity")
def check_entity_resolution_sanity(ctx: AuditContext) -> AuditResult:
    conn = ctx.conn
    offending = []

    for e in conn.execute("SELECT id FROM entity").fetchall():
        _, cycle = _resolve(conn, e["id"])
        if cycle:
            offending.append({"entity": e["id"], "reason": "merged_into cycle"})

    for e in conn.execute(
        "SELECT id, canonical_membership_id FROM entity WHERE canonical_membership_id IS NOT NULL"
    ).fetchall():
        m = conn.execute(
            "SELECT entity_id FROM entity_membership WHERE id=?", (e["canonical_membership_id"],)).fetchone()
        if m is None:
            offending.append({"entity": e["id"], "reason": "canonical_membership_id missing"})
            continue
        retracted = conn.execute(
            "SELECT 1 FROM entity_membership_retraction WHERE membership_id=?",
            (e["canonical_membership_id"],)).fetchone()
        if retracted:
            offending.append({"entity": e["id"], "reason": "canonical membership is retracted"})
        ent_resolved, _ = _resolve(conn, e["id"])
        mem_resolved, _ = _resolve(conn, m["entity_id"])
        if ent_resolved != mem_resolved:
            offending.append({"entity": e["id"], "reason": "canonical membership belongs to a different entity"})

    return AuditResult("entity-resolution-sanity", passed=not offending, offending=offending)
