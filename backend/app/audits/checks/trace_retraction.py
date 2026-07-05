"""Trace-retraction append-only audit (P9 / FN-04).

A trace edge/link — and, since v1.3.1, a WHOLE trace — is RETRACTED, never deleted; the retraction is
append-only investigator history. This CROSS-RUN check baselines the trace-retraction tables and fails if a
baselined retraction row disappears (a deletion — an attempt to silently un-retract) or is rewritten. Adding more retractions is
normal append-only growth. It mirrors ``append-only-claims`` (immutability.py) and reuses its schema-aware
baseline helpers, so a forward-only migration that touches these tables re-baselines loudly instead of
looking like tampering.
"""

from __future__ import annotations

from .. import AuditContext, AuditResult, audit_check
from .immutability import (
    _LEGACY_MISMATCH_HINT,
    _applied_migrations,
    _hash_row,
    _pack_baseline,
    _schema_change_verdict,
    _schema_fingerprint,
    _unpack_baseline,
)

RETRACTION_BASELINE = "trace-retraction-append-only"
RETRACTION_TABLES = ("trace_transfer_retraction", "trace_btc_link_retraction", "trace_retraction")


def _retraction_snapshot(conn) -> dict[str, dict[str, str]]:
    """Map ``{table -> {retraction_id -> row_hash}}`` over every trace-retraction row. A retraction table
    added by a LATER migration (e.g. ``trace_retraction``, v1.3.1) is simply absent on an older-schema DB —
    the audit runs against the raw extracted DB during import/verify BEFORE it forward-migrates, so skip a
    table that does not exist yet (no rows to protect; the schema-change verdict handles the bump on migrate)."""
    existing = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    snap: dict[str, dict[str, str]] = {}
    for table in RETRACTION_TABLES:
        if table not in existing:
            continue
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        snap[table] = {r["id"]: _hash_row(r) for r in rows}
    return snap


@audit_check("trace-retraction-append-only")
def check_trace_retraction_append_only(ctx: AuditContext) -> AuditResult:
    current = _retraction_snapshot(ctx.conn)
    schema_now = _schema_fingerprint(ctx.conn, RETRACTION_TABLES)
    migs_now = _applied_migrations(ctx.conn)
    raw = ctx.baselines.read(RETRACTION_BASELINE)

    if raw is None:
        ctx.baselines.write(RETRACTION_BASELINE, _pack_baseline(current, schema_now, migs_now))
        total = sum(len(v) for v in current.values())
        return AuditResult(RETRACTION_BASELINE, passed=True,
                           detail=f"baseline recorded ({total} retractions)")

    baseline, schema_base, migs_base = _unpack_baseline(raw)

    verdict = _schema_change_verdict(RETRACTION_BASELINE, schema_base, schema_now, migs_base, migs_now)
    if verdict == "rebaseline":
        ctx.baselines.write(RETRACTION_BASELINE, _pack_baseline(current, schema_now, migs_now))
        new = ", ".join(sorted(set(migs_now or []) - set(migs_base or []))) or "(unknown)"
        total = sum(len(v) for v in current.values())
        return AuditResult(RETRACTION_BASELINE, passed=True,
                           detail=f"trace-retraction schema advanced by migration(s) {new}; baseline "
                                  f"re-established ({total} retractions)")
    if verdict is not None:
        return AuditResult(RETRACTION_BASELINE, passed=False, offending=[verdict])

    offending = []
    for table in RETRACTION_TABLES:
        base = baseline.get(table, {})
        if isinstance(base, list):  # tolerate a legacy id-only baseline (deletion-only until upgraded)
            base = {rid: None for rid in base}
        cur = current.get(table, {})   # {} when the table is absent on an older-schema DB (see _retraction_snapshot)
        for rid, old_hash in base.items():
            cur_hash = cur.get(rid)
            if cur_hash is None:
                offending.append({"table": table, "id": rid, "reason": "retraction deleted"})
            elif old_hash is not None and cur_hash != old_hash:
                offending.append({"table": table, "id": rid, "reason": "retraction rewritten"})

    passed = not offending
    detail = ""
    if passed:
        ctx.baselines.write(RETRACTION_BASELINE, _pack_baseline(current, schema_now, migs_now))
    elif schema_base is None:
        detail = _LEGACY_MISMATCH_HINT.format(name=RETRACTION_BASELINE).strip()
    return AuditResult(RETRACTION_BASELINE, passed=passed, offending=offending, detail=detail)
