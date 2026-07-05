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
from datetime import datetime, timezone

from .. import AuditContext, AuditResult, audit_check
from ..baselines import append_anchor, read_latest_anchor

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


# --------------------------------------------------------------------------- in-DB anchor (P27/FN-19)
#
# The sidecar baseline above lives OUTSIDE the DB, so deleting it forces the "no baseline -> establish
# from current state" path to silently re-baseline whatever the DB now holds (see the trust-model note
# in this module's docstring). P27 commits a SECOND witness inside case.db: an append-only
# ``audit_baseline`` anchor (migration 0014). The anchor hashes the same immutable final snapshot AND the
# ``source_query.raw_response_hash`` provenance that produced those final facts (Invariant #3) — data the
# case commits. When the sidecar is MISSING, the check compares the recomputed anchor to the committed
# one instead of blindly re-baselining: an intact case still matches (benign sidecar loss -> re-establish
# and pass); a case whose final-state was rewritten no longer matches -> FAIL (tamper). The sidecar stays
# the authority WHEN PRESENT (it distinguishes benign additions from modifications, which a single digest
# cannot); the anchor only advances in lockstep so it tracks the same "current legitimate state".


def _final_provenance_hashes(conn) -> list[str]:
    """The distinct ``raw_response_hash`` values of the ``source_query`` rows that produced the FINAL
    (immutable) facts — the provenance the case commits (Invariant #3). Anchoring the baseline to THESE
    roots it in the ledger's ground truth, not only in the derived (rewritable) fact rows."""
    rows = conn.execute("""
        SELECT DISTINCT sq.raw_response_hash AS h
        FROM source_query sq
        WHERE sq.raw_response_hash IS NOT NULL AND sq.id IN (
            SELECT source_query_id FROM transaction_
              WHERE finality_status='final' AND source_query_id IS NOT NULL
            UNION SELECT t.source_query_id FROM transfer t JOIN transaction_ x ON x.id=t.transaction_id
              WHERE x.finality_status='final' AND t.source_query_id IS NOT NULL
            UNION SELECT i.source_query_id FROM tx_input i JOIN transaction_ x ON x.id=i.transaction_id
              WHERE x.finality_status='final' AND i.source_query_id IS NOT NULL
            UNION SELECT o.source_query_id FROM tx_output o JOIN transaction_ x ON x.id=o.transaction_id
              WHERE x.finality_status='final' AND o.source_query_id IS NOT NULL
        )
    """).fetchall()
    return sorted(r["h"] for r in rows)


def _anchor_digest(final_snapshot: dict[str, str], provenance_hashes: list[str]) -> str:
    """SHA-256 binding the immutable final snapshot to its committed provenance. A rewrite of any final
    row changes the snapshot; the provenance set ties it to the raw responses (Invariant #3)."""
    payload = json.dumps({"final": final_snapshot, "provenance": provenance_hashes}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _schema_version(conn) -> int:
    """The DB's own recorded ``case_meta.schema_version`` (informational for the anchor row); 0 if absent."""
    try:
        row = conn.execute("SELECT schema_version FROM case_meta LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def _sync_anchor(ctx: AuditContext, anchor_now: str, row_count: int) -> None:
    """Advance the in-DB anchor to ``anchor_now`` iff it changed (append-only; latest wins). Called only
    on establishing/passing paths, where the sidecar has already confirmed the change is legitimate."""
    if read_latest_anchor(ctx.conn, FINAL_BASELINE) != anchor_now:
        append_anchor(ctx.conn, FINAL_BASELINE, anchor_now, row_count=row_count,
                      schema_version=_schema_version(ctx.conn),
                      established_at=datetime.now(timezone.utc).isoformat())


@audit_check("final-immutability")
def check_final_immutability(ctx: AuditContext) -> AuditResult:
    current = _final_snapshot(ctx.conn)
    schema_now = _schema_fingerprint(ctx.conn, FINAL_AUDITED_TABLES)
    migs_now = _applied_migrations(ctx.conn)
    anchor_now = _anchor_digest(current, _final_provenance_hashes(ctx.conn))  # P27: in-DB witness
    raw = ctx.baselines.read(FINAL_BASELINE)

    if raw is None:
        # Sidecar missing. The in-DB anchor (P27/FN-19) is the witness that survives a deleted sidecar:
        # if a prior anchor exists and the committed final-state no longer matches it, refuse to silently
        # re-baseline (possible tampering) UNLESS the operator explicitly re-baselined this run.
        stored = read_latest_anchor(ctx.conn, FINAL_BASELINE)
        if stored is not None and stored != anchor_now and FINAL_BASELINE not in ctx.rebaselined:
            return AuditResult(
                "final-immutability", passed=False,
                offending=[{"reason": "committed final-state no longer matches the in-DB audit_baseline "
                                      "anchor", "baseline": FINAL_BASELINE}],
                detail="the baseline sidecar is absent and the in-DB anchor does not match current "
                       "final-state — refusing to silently re-baseline a possibly-tampered state "
                       "(P27/FN-19). If you verified this change out-of-band, re-establish explicitly "
                       "with `python -m backend.app.audits.runner --db <case.db> --rebaseline "
                       "final-immutability`.")
        ctx.baselines.write(FINAL_BASELINE, _pack_baseline(current, schema_now, migs_now))
        _sync_anchor(ctx, anchor_now, len(current))
        if stored is None:
            detail = f"baseline recorded ({len(current)} final rows)"
        elif stored == anchor_now:
            detail = f"baseline sidecar re-established from intact in-DB anchor ({len(current)} final rows)"
        else:  # guard bypassed via --rebaseline: an operator re-baseline of a re-verified change
            detail = f"operator re-baseline: in-DB anchor advanced to the re-verified state ({len(current)} final rows)"
        return AuditResult("final-immutability", passed=True, detail=detail)

    baseline, schema_base, migs_base = _unpack_baseline(raw)

    verdict = _schema_change_verdict(FINAL_BASELINE, schema_base, schema_now, migs_base, migs_now)
    if verdict == "rebaseline":
        ctx.baselines.write(FINAL_BASELINE, _pack_baseline(current, schema_now, migs_now))
        _sync_anchor(ctx, anchor_now, len(current))  # schema advance changes the snapshot -> re-anchor
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
        # No regression — advance the sidecar (newly-final rows) AND the in-DB anchor in lockstep, so the
        # anchor keeps tracking the current legitimate state (else a later sidecar loss would false-alarm).
        ctx.baselines.write(FINAL_BASELINE, _pack_baseline(current, schema_now, migs_now))
        _sync_anchor(ctx, anchor_now, len(current))
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
