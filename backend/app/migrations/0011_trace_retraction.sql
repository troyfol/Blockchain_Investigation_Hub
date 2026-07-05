-- depends: 0010_clustering_heuristics
-- P9 / FN-04: append-only RETRACTION of a trace edge/link. An investigator can withdraw a specific
-- `trace_transfer` (EVM edge) or `trace_btc_link` (BTC input->output linkage claim) WITHOUT deleting it —
-- the edge row stays, a retraction row is appended, and read paths + report exclude the retracted edge.
-- Mirrors `entity_membership_retraction` (0003): a Family-C investigator construction (no source_query_id),
-- append-only. `source` is 'investigator' (the only author of a trace today; explicit for parity/future).

CREATE TABLE trace_transfer_retraction (
  id                 TEXT PRIMARY KEY,
  trace_transfer_id  TEXT NOT NULL REFERENCES trace_transfer(id),
  reason             TEXT NOT NULL,
  source             TEXT NOT NULL,      -- 'investigator'
  created_at         TEXT NOT NULL
);
CREATE INDEX ix_trace_transfer_retraction ON trace_transfer_retraction(trace_transfer_id);

CREATE TABLE trace_btc_link_retraction (
  id                 TEXT PRIMARY KEY,
  trace_btc_link_id  TEXT NOT NULL REFERENCES trace_btc_link(id),
  reason             TEXT NOT NULL,
  source             TEXT NOT NULL,      -- 'investigator'
  created_at         TEXT NOT NULL
);
CREATE INDEX ix_trace_btc_link_retraction ON trace_btc_link_retraction(trace_btc_link_id);
