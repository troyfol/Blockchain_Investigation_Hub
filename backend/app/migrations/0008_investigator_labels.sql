-- depends: 0007_transfer_cross_source_reconciliation
-- Investigator display-label overrides (Family C). A custom label the investigator sets on a node
-- (address) or a trace/path. These are investigator CONSTRUCTIONS, not sourced facts/claims — they
-- carry NO source_query_id (exactly like trace/finding/annotation/tag in 0004), and they NEVER touch
-- the underlying facts, which stay immutable (Invariants #5/#6).
--
-- Append-only: the CURRENT display label for a target is the MOST-RECENT row; every rename is a new
-- immutable row, so the rename history is preserved (and the whole table travels in the .casefile
-- bundle automatically — export ships the entire case.db). Surfaced with display precedence on the
-- graph read-model (over the auto first4…last4 alias / trace name) and in the report.
CREATE TABLE investigator_label (
  id           TEXT PRIMARY KEY,
  target_type  TEXT NOT NULL CHECK (target_type IN ('address','trace')),
  target_id    TEXT NOT NULL,           -- app-enforced poly ref (address.id | trace.id)
  label        TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
CREATE INDEX ix_investigator_label_target ON investigator_label(target_type, target_id);
