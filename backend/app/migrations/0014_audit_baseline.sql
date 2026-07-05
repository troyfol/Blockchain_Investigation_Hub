-- depends: 0013_risk_detail
-- P27 / FN-19: in-DB append-only anchor for the final-immutability baseline.
-- The cross-run baseline used to live ONLY in a JSON sidecar (`.audit_baselines/`). An adversary who
-- rewrites a `final` row can also delete that sidecar; the next audit then finds no baseline and
-- silently RE-BASELINES the already-tampered state (the hole admitted in audits/checks/immutability.py's
-- trust model). This table commits the baseline INSIDE the case DB so a re-open cannot silently
-- re-baseline a pre-tampered state: `anchor_hash` binds the immutable final snapshot to the
-- `source_query.raw_response_hash` provenance the case commits (Invariants #3, #6). Append-only —
-- an explicit operator re-baseline APPENDS a superseding row (latest wins), it never rewrites history.
-- The table rides inside case.db (manifest-hashed) so export stays tamper-evident (phase_10 follow-up).

CREATE TABLE audit_baseline (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  baseline_name  TEXT NOT NULL,               -- the owning cross-run check (e.g. 'final-immutability')
  anchor_hash    TEXT NOT NULL,               -- SHA-256 over {final snapshot + its raw_response_hashes}
  row_count      INTEGER NOT NULL,            -- final rows at establishment (informational; not hashed)
  schema_version INTEGER NOT NULL,            -- case_meta.schema_version at establishment (informational)
  established_at TEXT NOT NULL                -- ISO-8601 UTC timestamp (informational; not hashed)
);
CREATE INDEX ix_audit_baseline_name ON audit_baseline(baseline_name, id);

-- Append-only enforcement at the DB layer: anchors are appended, never rewritten or deleted. This is
-- tamper-EVIDENCE, not tamper-PROOF (an adversary with raw DB access can drop the trigger) — but the
-- application itself can only ever APPEND, so a silent in-app rewrite is impossible.
CREATE TRIGGER audit_baseline_no_update
BEFORE UPDATE ON audit_baseline
BEGIN
  SELECT RAISE(ABORT, 'audit_baseline is append-only (P27/FN-19): anchors are appended, never rewritten');
END;
CREATE TRIGGER audit_baseline_no_delete
BEFORE DELETE ON audit_baseline
BEGIN
  SELECT RAISE(ABORT, 'audit_baseline is append-only (P27/FN-19): anchors are appended, never deleted');
END;
