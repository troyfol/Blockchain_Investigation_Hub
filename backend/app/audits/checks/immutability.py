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

**Schema-aware baselines (format 2 — review finding BASE-02).** The row hashes cover full rows
(``SELECT *``), so a forward-only schema migration that touches an audited table (e.g. 0007 adding
``transfer.occurrence``) changes EVERY hash — which used to be indistinguishable from mass
tampering (cases/live: 2,362 false "final row modified"). The baseline therefore records, next to
the row hashes, the audited tables' schema fingerprint and the applied-migration set:

  * schema unchanged                          → rows compare exactly as before;
  * schema advanced by NEW forward-only
    migrations (the sanctioned upgrade path)  → cross-schema hashes are not comparable; the
                                                 baseline is RE-ESTABLISHED, loudly, in the result
                                                 detail (a passing run, not a silent reset);
  * schema changed with NO new migration      → FAIL (an ALTER outside the migration path is
                                                 possible tampering);
  * a legacy (pre-format-2) baseline           → rows compare as before (and upgrade to format 2 on
                                                 a passing run); on a mismatch the failure detail
                                                 names the explicit ``--rebaseline`` escape hatch,
                                                 because the mismatch may be a pre-fix migration
                                                 artifact — the operator verifies and decides.

**Trust model (read this):** the baseline is tamper-EVIDENCE, not tamper-PROOF. It catches changes
across runs that share an intact baseline. An adversary who can rewrite rows can also delete the
sidecar (or ride a migration through the re-baseline path) — that was true before format 2 and is
unchanged by it. The baseline lives in the case folder (``<case.db parent>/.audit_baselines/``) so
it travels with the portable case; export (Phase 10) must include and verify it. The FIRST audit
after ingest establishes the baseline — run it while the data is fresh. Notarization /
cryptographic non-repudiation is a named future item (docs/overview.md §7).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from .. import AuditContext, AuditResult, audit_check

FINAL_BASELINE = "final-immutability"
CLAIMS_BASELINE = "append-only-claims"

CLAIM_TABLES = [
    "attribution", "risk_assessment", "valuation", "balance_snapshot",
    "entity_membership", "entity_membership_retraction",
]

# Tables whose row hashes feed each cross-run baseline: a schema change to any of THESE (and only
# these) invalidates hash comparability for that baseline.
FINAL_AUDITED_TABLES = ("transaction_", "transfer", "tx_input", "tx_output")

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


def _schema_fingerprint(conn, tables) -> str:
    """SHA-256 over the audited tables' ``sqlite_master`` DDL — changes iff their schema does."""
    qmarks = ",".join("?" * len(tables))
    rows = conn.execute(
        f"SELECT name, COALESCE(sql, '') FROM sqlite_master WHERE type='table' AND name IN ({qmarks})",
        tuple(tables),
    ).fetchall()
    payload = json.dumps(sorted((r[0], r[1]) for r in rows))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _applied_migrations(conn) -> "list[str] | None":
    """The forward-only migration ids applied to this DB, or ``None`` when it has no migration
    record (a scratch DB) — in which case no schema change can be attributed to a migration."""
    try:
        rows = conn.execute("SELECT migration_id FROM _yoyo_migration").fetchall()
    except sqlite3.OperationalError:
        return None
    return sorted(r[0] for r in rows)


def _pack_baseline(rows, schema: str, migrations: "list[str] | None") -> dict:
    return {"format": 2, "schema": schema, "migrations": migrations, "rows": rows}


def _unpack_baseline(raw) -> "tuple[object, str | None, list[str] | None]":
    """-> ``(rows, schema, migrations)``. Legacy (pre-format-2) baselines are the bare rows payload
    with no schema metadata; the sentinel keys can't collide (final rows are ``table:id`` keys,
    claims rows are keyed by the fixed CLAIM_TABLES names)."""
    if isinstance(raw, dict) and raw.get("format") == 2:
        return raw.get("rows", {}), raw.get("schema"), raw.get("migrations")
    return raw, None, None


def _schema_change_verdict(baseline_name: str, schema_base: "str | None", schema_now: str,
                           migs_base: "list[str] | None", migs_now: "list[str] | None"):
    """Adjudicate a fingerprint difference. Returns ``None`` when the schemas match (compare rows),
    ``"rebaseline"`` for a sanctioned forward-only migration advance, or an offending-row dict for
    a schema change nothing accounts for."""
    if schema_base is None or schema_base == schema_now:
        return None
    if migs_base is not None and migs_now is not None and set(migs_base) < set(migs_now):
        return "rebaseline"
    return {
        "reason": "audited-table schema changed without new forward-only migrations "
                  "(possible tampering — no migration accounts for the DDL change)",
        "baseline": baseline_name,
    }


_LEGACY_MISMATCH_HINT = (
    " baseline predates schema tracking (legacy format): if a schema migration touched the audited "
    "tables since it was recorded, this can be a pre-format-2 false alarm — verify row content "
    "out-of-band, then re-establish explicitly with "
    "`python -m backend.app.audits.runner --db <case.db> --rebaseline {name}`."
)


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
    schema_now = _schema_fingerprint(ctx.conn, FINAL_AUDITED_TABLES)
    migs_now = _applied_migrations(ctx.conn)
    raw = ctx.baselines.read(FINAL_BASELINE)

    if raw is None:
        ctx.baselines.write(FINAL_BASELINE, _pack_baseline(current, schema_now, migs_now))
        return AuditResult("final-immutability", passed=True,
                           detail=f"baseline recorded ({len(current)} final rows)")

    baseline, schema_base, migs_base = _unpack_baseline(raw)

    verdict = _schema_change_verdict(FINAL_BASELINE, schema_base, schema_now, migs_base, migs_now)
    if verdict == "rebaseline":
        ctx.baselines.write(FINAL_BASELINE, _pack_baseline(current, schema_now, migs_now))
        new = ", ".join(sorted(set(migs_now or []) - set(migs_base or []))) or "(unknown)"
        return AuditResult("final-immutability", passed=True,
                           detail=f"audited-table schema advanced by migration(s) {new}; baseline "
                                  f"re-established ({len(current)} final rows) — cross-schema row "
                                  f"hashes are not comparable")
    if verdict is not None:
        return AuditResult("final-immutability", passed=False, offending=[verdict])

    offending = []
    for key, old_hash in baseline.items():
        cur_hash = current.get(key)
        if cur_hash is None:
            offending.append({"row": key, "reason": "final row deleted"})
        elif cur_hash != old_hash:
            offending.append({"row": key, "reason": "final row modified"})

    passed = not offending
    detail = ""
    if passed:
        # No regression — advance the baseline to include any newly-final rows.
        ctx.baselines.write(FINAL_BASELINE, _pack_baseline(current, schema_now, migs_now))
    elif schema_base is None:
        detail = _LEGACY_MISMATCH_HINT.format(name=FINAL_BASELINE).strip()
    return AuditResult("final-immutability", passed=passed, offending=offending, detail=detail)


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
    schema_now = _schema_fingerprint(ctx.conn, CLAIM_TABLES)
    migs_now = _applied_migrations(ctx.conn)
    raw = ctx.baselines.read(CLAIMS_BASELINE)

    if raw is None:
        ctx.baselines.write(CLAIMS_BASELINE, _pack_baseline(current, schema_now, migs_now))
        total = sum(len(v) for v in current.values())
        return AuditResult("append-only-claims", passed=True,
                           detail=f"baseline recorded ({total} claims)")

    baseline, schema_base, migs_base = _unpack_baseline(raw)

    verdict = _schema_change_verdict(CLAIMS_BASELINE, schema_base, schema_now, migs_base, migs_now)
    if verdict == "rebaseline":
        ctx.baselines.write(CLAIMS_BASELINE, _pack_baseline(current, schema_now, migs_now))
        new = ", ".join(sorted(set(migs_now or []) - set(migs_base or []))) or "(unknown)"
        total = sum(len(v) for v in current.values())
        return AuditResult("append-only-claims", passed=True,
                           detail=f"claim-table schema advanced by migration(s) {new}; baseline "
                                  f"re-established ({total} claims) — cross-schema row hashes are "
                                  f"not comparable")
    if verdict is not None:
        return AuditResult("append-only-claims", passed=False, offending=[verdict])

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
    detail = ""
    if passed:
        ctx.baselines.write(CLAIMS_BASELINE, _pack_baseline(current, schema_now, migs_now))
    elif schema_base is None:
        detail = _LEGACY_MISMATCH_HINT.format(name=CLAIMS_BASELINE).strip()
    return AuditResult("append-only-claims", passed=passed, offending=offending, detail=detail)
