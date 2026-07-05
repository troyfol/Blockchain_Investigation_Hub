-- depends: 0011_trace_retraction
-- P12 / FN-17: a manual CROSS-CHAIN bridge link inside a trace. The investigator asserts that an outflow
-- movement on chain A corresponds to an inflow movement on chain B (a bridge crossing) — a labeled
-- `basis='investigator'` CLAIM, NEVER a synthesized `transfer`/ledger fact (Invariant #5) and never a
-- collapse of the two sides (Invariant #4). It is a Family-C investigator construction (no source_query_id).
-- Each side is an app-enforced poly ref to a value movement (`transfer` | `tx_output`), mirroring
-- `valuation.subject_id` (the `no-dangling-fk` audit validates both refs). `basis` is investigator-only —
-- automated bridge detection stays rejected (RJ-02).

CREATE TABLE trace_bridge_link (
  id                TEXT PRIMARY KEY,
  trace_id          TEXT NOT NULL REFERENCES trace(id),
  src_subject_type  TEXT NOT NULL CHECK (src_subject_type IN ('transfer','tx_output')),
  src_subject_id    TEXT NOT NULL,     -- app-enforced poly ref: the chain-A outflow movement
  dst_subject_type  TEXT NOT NULL CHECK (dst_subject_type IN ('transfer','tx_output')),
  dst_subject_id    TEXT NOT NULL,     -- app-enforced poly ref: the chain-B inflow movement
  basis             TEXT NOT NULL CHECK (basis IN ('investigator')),  -- manual assertion only (RJ-02)
  confidence        REAL,
  ordering          INTEGER,
  note              TEXT,
  created_at        TEXT NOT NULL
);
CREATE INDEX ix_trace_bridge_link_trace ON trace_bridge_link(trace_id);
