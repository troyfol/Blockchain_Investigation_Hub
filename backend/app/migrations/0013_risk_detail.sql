-- depends: 0012_trace_bridge_link
-- P20 / FN-15: per-sub-signal risk detail rows. A `risk_assessment` carries ONE headline score + dominant
-- category, but a paid intel source (Arkham, MisTrack) reports MANY per-category sub-signals
-- (hacker/mixer/sanctions/ransomware/…). Those were flattened into `risk_assessment.rationale` (an
-- un-queryable blob). This child table promotes each sub-signal to a first-class RAW row — never
-- collapsed/averaged/merged, and each source's breakdown stays side-by-side (Invariant #4). Each row FKs its
-- parent `risk_assessment`, is written in the SAME txn as the parent, and carries its OWN `source_query_id`
-- (like every fact/claim — tx_output precedent), so provenance-completeness holds per row (Invariant #3).
-- Idempotent on (risk_assessment_id, signal): re-ingesting a parent's breakdown is a no-op (Invariant #7).

CREATE TABLE risk_detail (
  id                  TEXT PRIMARY KEY,
  risk_assessment_id  TEXT NOT NULL REFERENCES risk_assessment(id),
  signal              TEXT NOT NULL,               -- the source's own sub-signal key (e.g. 'mixer','hacker')
  score               REAL,                        -- the sub-signal's numeric score (nullable)
  score_scale         TEXT,                        -- e.g. '0-100' (Arkham) / '3-100' or 'percent' (MisTrack)
  source_query_id     TEXT REFERENCES source_query(id)
);
CREATE INDEX ix_risk_detail_assessment ON risk_detail(risk_assessment_id);
CREATE UNIQUE INDEX ux_risk_detail ON risk_detail(risk_assessment_id, signal);
