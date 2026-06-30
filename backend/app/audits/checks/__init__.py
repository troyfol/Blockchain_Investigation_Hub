"""Invariant check modules.

Empty in Phase 0. Each later phase drops its required checks here (docs/testing.md §2):
provenance completeness, no dangling FKs, idempotency, final-immutability,
no-fabricated-UTXO-edge, append-only claims, entity-resolution sanity, cache provenance,
valuation-subject validity, bounds-recorded. The runner discovers them automatically.

A check is a function decorated with ``@audit_check("name")`` taking an ``AuditContext`` and
returning an ``AuditResult``. ``offending`` is a list of anything printable (dicts encouraged
for clarity). Example shape::

    from .. import audit_check, AuditContext, AuditResult

    @audit_check("no-dangling-fk")
    def check_no_dangling_fk(ctx: AuditContext) -> AuditResult:
        bad = ctx.conn.execute("PRAGMA foreign_key_check").fetchall()
        return AuditResult(
            name="no-dangling-fk",
            passed=not bad,
            offending=[dict(r) for r in bad],
        )

Cross-run checks use ``ctx.baselines`` to persist/compare state, e.g.::

    prev = ctx.baselines.read("final-immutability")
    current = compute_checksum(ctx.conn)
    if prev is not None and prev != current:
        return AuditResult("final-immutability", passed=False, offending=[current])
    ctx.baselines.write("final-immutability", current)
    return AuditResult("final-immutability", passed=True)
"""
