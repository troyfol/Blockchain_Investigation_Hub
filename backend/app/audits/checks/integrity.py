"""Structural-integrity audits (docs/testing.md §2 #2 and #3).

#2 no-dangling-fk: ``PRAGMA foreign_key_check`` empty AND every app-enforced poly ref
   (valuation.subject_id, finding_ref.ref_id, annotation.target_id, tag.target_id,
   entity.canonical_membership_id) resolves to an existing row of the declared type
   (docs/schema.md §5).
#3 idempotency: per fact table, row count == distinct natural-key count (the unique indexes
   enforce; the audit asserts it).
"""

from __future__ import annotations

from .. import AuditContext, AuditResult, audit_check

# finding_ref.ref_type / annotation.target_type / tag.target_type -> the table that holds the id.
FINDING_REF_TABLE = {
    "address": "address", "transfer": "transfer", "transaction": "transaction_",
    "tx_output": "tx_output", "trace": "trace", "exhibit": "exhibit", "entity": "entity",
}
ANNOTATION_TARGET_TABLE = {
    "address": "address", "transfer": "transfer", "transaction": "transaction_",
    "tx_output": "tx_output", "trace": "trace", "entity": "entity", "finding": "finding",
}
TAG_TARGET_TABLE = {"address": "address", "entity": "entity"}
# investigator_label.target_type -> the table that holds the id (display-label overrides, migration 0008;
# widened to transactions + flows in migration 0009).
INVESTIGATOR_LABEL_TARGET_TABLE = {
    "address": "address", "trace": "trace", "transaction": "transaction_",
    "transfer": "transfer", "tx_output": "tx_output",
}

# table -> SQL expression for its natural unique key (docs/schema.md §4).
NATURAL_KEYS = {
    "asset": "chain || '|' || COALESCE(contract_address,'')",
    "address": "chain || '|' || address",
    "transaction_": "chain || '|' || tx_hash",
    # transfer dedup is content-based + occurrence (decision (c), migration 0007) — NOT position.
    "transfer": ("transaction_id || '|' || transfer_type || '|' || COALESCE(from_address_id,'') || '|' || "
                 "COALESCE(to_address_id,'') || '|' || asset_id || '|' || amount || '|' || occurrence"),
    "tx_output": "transaction_id || '|' || output_index",
    "tx_input": "transaction_id || '|' || input_index",
}


def _antijoin(conn, sql: str, params=()) -> list:
    return conn.execute(sql, params).fetchall()


@audit_check("no-dangling-fk")
def check_no_dangling_fk(ctx: AuditContext) -> AuditResult:
    conn = ctx.conn
    offending: list[dict] = []

    # Declared FKs.
    for r in conn.execute("PRAGMA foreign_key_check").fetchall():
        # row = (table, rowid, parent, fkid)
        offending.append({"kind": "fk", "table": r[0], "rowid": r[1], "parent": r[2]})

    # valuation.subject_id (poly by subject_type)
    for r in _antijoin(conn, """
        SELECT id, subject_type, subject_id FROM valuation v
        WHERE (subject_type='transfer'  AND NOT EXISTS (SELECT 1 FROM transfer t  WHERE t.id=v.subject_id))
           OR (subject_type='tx_output' AND NOT EXISTS (SELECT 1 FROM tx_output o WHERE o.id=v.subject_id))
    """):
        offending.append({"kind": "valuation.subject_id", "id": r["id"],
                          "subject_type": r["subject_type"], "subject_id": r["subject_id"]})

    # finding_ref.ref_id (poly by ref_type)
    for rtype, table in FINDING_REF_TABLE.items():
        for r in _antijoin(conn, f"""
            SELECT id, ref_id FROM finding_ref
            WHERE ref_type=? AND NOT EXISTS (SELECT 1 FROM {table} x WHERE x.id=finding_ref.ref_id)
        """, (rtype,)):
            offending.append({"kind": "finding_ref.ref_id", "id": r["id"],
                              "ref_type": rtype, "ref_id": r["ref_id"]})

    # annotation.target_id (poly by target_type)
    for ttype, table in ANNOTATION_TARGET_TABLE.items():
        for r in _antijoin(conn, f"""
            SELECT id, target_id FROM annotation
            WHERE target_type=? AND NOT EXISTS (SELECT 1 FROM {table} x WHERE x.id=annotation.target_id)
        """, (ttype,)):
            offending.append({"kind": "annotation.target_id", "id": r["id"],
                              "target_type": ttype, "target_id": r["target_id"]})

    # tag.target_id (poly by target_type)
    for ttype, table in TAG_TARGET_TABLE.items():
        for r in _antijoin(conn, f"""
            SELECT id, target_id FROM tag
            WHERE target_type=? AND NOT EXISTS (SELECT 1 FROM {table} x WHERE x.id=tag.target_id)
        """, (ttype,)):
            offending.append({"kind": "tag.target_id", "id": r["id"],
                              "target_type": ttype, "target_id": r["target_id"]})

    # investigator_label.target_id (poly by target_type) — display-label overrides (migration 0008)
    for ttype, table in INVESTIGATOR_LABEL_TARGET_TABLE.items():
        for r in _antijoin(conn, f"""
            SELECT id, target_id FROM investigator_label
            WHERE target_type=? AND NOT EXISTS (SELECT 1 FROM {table} x WHERE x.id=investigator_label.target_id)
        """, (ttype,)):
            offending.append({"kind": "investigator_label.target_id", "id": r["id"],
                              "target_type": ttype, "target_id": r["target_id"]})

    # entity.canonical_membership_id -> entity_membership.id (when set)
    for r in _antijoin(conn, """
        SELECT id, canonical_membership_id FROM entity
        WHERE canonical_membership_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM entity_membership m WHERE m.id=entity.canonical_membership_id)
    """):
        offending.append({"kind": "entity.canonical_membership_id", "id": r["id"],
                          "canonical_membership_id": r["canonical_membership_id"]})

    return AuditResult("no-dangling-fk", passed=not offending, offending=offending)


@audit_check("idempotency")
def check_idempotency(ctx: AuditContext) -> AuditResult:
    conn = ctx.conn
    offending: list[dict] = []
    for table, key in NATURAL_KEYS.items():
        total, distinct = conn.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT {key}) FROM {table}"
        ).fetchone()
        if total != distinct:
            offending.append({"table": table, "rows": total, "distinct_keys": distinct})
    return AuditResult("idempotency", passed=not offending, offending=offending)
