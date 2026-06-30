-- depends: 0008_investigator_labels
-- Widen investigator_label.target_type so the investigator can relabel TRANSACTIONS and FLOWS
-- (transfer / tx_output), not only addresses and traces. SQLite cannot ALTER a CHECK constraint in
-- place, so rebuild the table (the standard 12-step recreate), preserving every existing row + the
-- index. investigator_label has no real FKs (its poly ref is app-enforced, validated on write), so the
-- rebuild needs no foreign-key toggling. Still Family C (no source_query_id); the underlying facts stay
-- immutable — a display-label override never touches them (Invariants #5/#6).
CREATE TABLE investigator_label_new (
  id           TEXT PRIMARY KEY,
  target_type  TEXT NOT NULL CHECK (target_type IN ('address','trace','transaction','transfer','tx_output')),
  target_id    TEXT NOT NULL,           -- app-enforced poly ref (address|trace|transaction|transfer|tx_output)
  label        TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
INSERT INTO investigator_label_new (id, target_type, target_id, label, created_at)
  SELECT id, target_type, target_id, label, created_at FROM investigator_label;
DROP TABLE investigator_label;
ALTER TABLE investigator_label_new RENAME TO investigator_label;
CREATE INDEX ix_investigator_label_target ON investigator_label(target_type, target_id);
