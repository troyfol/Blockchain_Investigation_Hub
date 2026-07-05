-- depends: 0014_audit_baseline
-- v1.3.1 / FN-04: append-only RETRACTION of a WHOLE trace. An investigator can withdraw a trace they built
-- (a mistaken or experimental path) WITHOUT deleting it — the `trace` row and its edges/links stay, a
-- retraction row is appended, and every effective read path (the trace list, the graph overlay, the report
-- trace section, the activity timeline) excludes the retracted trace. This mirrors the edge/link retraction
-- of 0011 one level up: a Family-C investigator construction (no `source_query_id`), append-only,
-- `source='investigator'`. The existing `trace-retraction-append-only` audit covers it (RETRACTION_TABLES
-- gains this table), so a retracted trace can never be silently un-retracted (deleted) or rewritten.

CREATE TABLE trace_retraction (
  id          TEXT PRIMARY KEY,
  trace_id    TEXT NOT NULL REFERENCES trace(id),
  reason      TEXT NOT NULL,
  source      TEXT NOT NULL,      -- 'investigator'
  created_at  TEXT NOT NULL
);
CREATE INDEX ix_trace_retraction ON trace_retraction(trace_id);
