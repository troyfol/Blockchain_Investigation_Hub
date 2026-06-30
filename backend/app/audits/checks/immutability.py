"""Immutability / append-only audits (docs/testing.md §2 #4 and #6).

These are CROSS-RUN checks: they persist a baseline (via ``ctx.baselines``) and fail if state
regresses on a later run.

#4 final-immutability: once a transaction is ``final``, its ledger facts (and its transfers /
   inputs / outputs) are frozen. New final rows may appear; a baselined final row that changes
   or disappears is a failure. Columns that the schema legitimately refreshes *after*
   finalization are EXCLUDED from the hash (docs/schema.md §4):
     - ``tx_output.spent`` / ``spending_tx_id`` (set when a later tx spends the output);
     - ``tx_input.prev_output_id`` / ``address_id`` (the "prev_output linkage refresh" — resolved
       once the funding output is in-DB, which may be after the spending tx is already final).
#6 append-only-claims: claim tables only grow and are never rewritten (Invariant #4). A baselined
   claim id that disappears (deletion) OR whose content changes (rewrite / id-reuse) is a failure
   — so the snapshot hashes each claim row, not just its id.

**Trust model (read this):** the baseline is tamper-EVIDENCE, not tamper-PROOF. It catches changes
across runs that share an intact baseline. The baseline lives in the case folder
(``<case.db parent>/.audit_baselines/``) so it travels with the portable case; export (Phase 10)
must include and verify it. The FIRST audit after ingest establishes the baseline — run it while
the data is fresh. Notarization / cryptographic non-repudiation is a named future item
(docs/overview.md §7).
"""

from __future__ import annotations

import hashlib
import json

from .. import AuditContext, AuditResult, audit_check

FINAL_BASELINE = "final-immutability"
CLAIMS_BASELINE = "append-only-claims"

CLAIM_TABLES = [
    "attribution", "risk_assessment", "valuation", "balance_snapshot",
    "entity_membership", "entity_membership_retraction",
]

# Immutable column projections (qualified by alias — the snapshot queries join transaction_,
# which shares column names like id / source_query_id). Excluded columns are post-finalization
# linkage that the schema legitimately refreshes (docs/schema.md §4).
TX_OUTPUT_IMMUTABLE_COLS = (
    "o.id, o.transaction_id, o.address_id, o.amount, o.output_index, o.source_query_id"
)
TX_INPUT_IMMUTABLE_COLS = (
    "i.id, i.transaction_id, i.amount, i.input_index, i.source_query_id"
)


def _hash_row(row) -> str:
    payload = json.dumps({k: row[k] for k in row.keys()}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _final_snapshot(conn) -> dict[str, str]:
    """Map ``{table:id -> hash}`` over the immutable facts of all final transactions."""
    snap: dict[str, str] = {}
    for r in conn.execute("SELECT * FROM transaction_ WHERE finality_status='final'").fetchall():
        snap[f"transaction_:{r['id']}"] = _hash_row(r)
    for r in conn.execute("""
        SELECT t.* FROM transfer t
        JOIN transaction_ x ON x.id=t.transaction_id WHERE x.finality_status='final'
    """).fetchall():
        snap[f"transfer:{r['id']}"] = _hash_row(r)
    for r in conn.execute(f"""
        SELECT {TX_INPUT_IMMUTABLE_COLS} FROM tx_input i
        JOIN transaction_ x ON x.id=i.transaction_id WHERE x.finality_status='final'
    """).fetchall():
        snap[f"tx_input:{r['id']}"] = _hash_row(r)
    for r in conn.execute(f"""
        SELECT {TX_OUTPUT_IMMUTABLE_COLS} FROM tx_output o
        JOIN transaction_ x ON x.id=o.transaction_id WHERE x.finality_status='final'
    """).fetchall():
        snap[f"tx_output:{r['id']}"] = _hash_row(r)
    return snap


@audit_check("final-immutability")
def check_final_immutability(ctx: AuditContext) -> AuditResult:
    current = _final_snapshot(ctx.conn)
    baseline = ctx.baselines.read(FINAL_BASELINE)

    if baseline is None:
        ctx.baselines.write(FINAL_BASELINE, current)
        return AuditResult("final-immutability", passed=True,
                           detail=f"baseline recorded ({len(current)} final rows)")

    offending = []
    for key, old_hash in baseline.items():
        cur_hash = current.get(key)
        if cur_hash is None:
            offending.append({"row": key, "reason": "final row deleted"})
        elif cur_hash != old_hash:
            offending.append({"row": key, "reason": "final row modified"})

    passed = not offending
    if passed:
        # No regression — advance the baseline to include any newly-final rows.
        ctx.baselines.write(FINAL_BASELINE, current)
    return AuditResult("final-immutability", passed=passed, offending=offending)


def _claims_snapshot(conn) -> dict[str, dict[str, str]]:
    """Map ``{table -> {claim_id -> row_hash}}`` over all claim rows."""
    snap: dict[str, dict[str, str]] = {}
    for table in CLAIM_TABLES:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        snap[table] = {r["id"]: _hash_row(r) for r in rows}
    return snap


@audit_check("append-only-claims")
def check_append_only_claims(ctx: AuditContext) -> AuditResult:
    current = _claims_snapshot(ctx.conn)
    baseline = ctx.baselines.read(CLAIMS_BASELINE)

    if baseline is None:
        ctx.baselines.write(CLAIMS_BASELINE, current)
        total = sum(len(v) for v in current.values())
        return AuditResult("append-only-claims", passed=True,
                           detail=f"baseline recorded ({total} claims)")

    offending = []
    for table in CLAIM_TABLES:
        base = baseline.get(table, {})
        # Tolerate a legacy id-only baseline (list) — deletion-only until upgraded on next pass.
        if isinstance(base, list):
            base = {cid: None for cid in base}
        cur = current[table]
        for cid, old_hash in base.items():
            cur_hash = cur.get(cid)
            if cur_hash is None:
                offending.append({"table": table, "id": cid, "reason": "claim deleted"})
            elif old_hash is not None and cur_hash != old_hash:
                offending.append({"table": table, "id": cid, "reason": "claim rewritten"})

    passed = not offending
    if passed:
        ctx.baselines.write(CLAIMS_BASELINE, current)
    return AuditResult("append-only-claims", passed=passed, offending=offending)
