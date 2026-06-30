"""Valuation-subject-validity audit (docs/testing.md §2 #9).

Every ``valuation`` is a derived claim on a value movement; its app-enforced poly ref must point
at an existing ``transfer`` (EVM) or ``tx_output`` (BTC) matching ``subject_type``.
"""

from __future__ import annotations

from .. import AuditContext, AuditResult, audit_check


@audit_check("valuation-subject-validity")
def check_valuation_subject_validity(ctx: AuditContext) -> AuditResult:
    bad = ctx.conn.execute(
        """
        SELECT id, subject_type, subject_id FROM valuation v
        WHERE (subject_type='transfer'  AND NOT EXISTS (SELECT 1 FROM transfer  t WHERE t.id=v.subject_id))
           OR (subject_type='tx_output' AND NOT EXISTS (SELECT 1 FROM tx_output o WHERE o.id=v.subject_id))
        """
    ).fetchall()
    return AuditResult("valuation-subject-validity", passed=not bad,
                       offending=[dict(r) for r in bad])
