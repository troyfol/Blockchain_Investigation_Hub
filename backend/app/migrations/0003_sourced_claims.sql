-- depends: 0002_onchain_facts
-- Sourced claims (Family B): append-only, many per subject, each tied to its query. Disagreement
-- is preserved, never collapsed (Invariant #4). (docs/schema.md §3, verbatim.)

CREATE TABLE attribution (
  id               TEXT PRIMARY KEY,
  address_id       TEXT NOT NULL REFERENCES address(id),
  label            TEXT NOT NULL,
  category         TEXT,
  source           TEXT NOT NULL,        -- arkham|misttrack|breadcrumbs|investigator|...
  confidence       REAL,
  note             TEXT,
  retrieved_at     TEXT NOT NULL,
  source_query_id  TEXT REFERENCES source_query(id)   -- nullable for investigator-authored
);
CREATE INDEX ix_attribution_addr ON attribution(address_id);

CREATE TABLE risk_assessment (
  id               TEXT PRIMARY KEY,
  address_id       TEXT NOT NULL REFERENCES address(id),
  score            REAL,
  score_scale      TEXT,                 -- e.g. '0-100'
  category         TEXT,
  rationale        TEXT,
  source           TEXT NOT NULL,
  retrieved_at     TEXT NOT NULL,
  source_query_id  TEXT REFERENCES source_query(id)
);
CREATE INDEX ix_risk_addr ON risk_assessment(address_id);

CREATE TABLE valuation (
  id               TEXT PRIMARY KEY,
  subject_type     TEXT NOT NULL CHECK (subject_type IN ('transfer','tx_output')),
  subject_id       TEXT NOT NULL,        -- app-enforced poly ref (transfer.id | tx_output.id)
  currency         TEXT NOT NULL DEFAULT 'USD',
  unit_price       TEXT NOT NULL,
  value            TEXT NOT NULL,        -- unit_price × (amount / 10^decimals); Decimal, half-even, 18 sig
  price_timestamp  TEXT NOT NULL,
  confidence       REAL,
  source           TEXT NOT NULL DEFAULT 'defillama',
  retrieved_at     TEXT NOT NULL,
  source_query_id  TEXT REFERENCES source_query(id)
);
CREATE INDEX ix_valuation_subject ON valuation(subject_type, subject_id);

CREATE TABLE balance_snapshot (
  id               TEXT PRIMARY KEY,
  address_id       TEXT NOT NULL REFERENCES address(id),
  asset_id         TEXT REFERENCES asset(id),   -- NULL = native/aggregate
  amount           TEXT NOT NULL,
  as_of_ts         TEXT NOT NULL,
  source           TEXT NOT NULL,
  retrieved_at     TEXT NOT NULL,
  source_query_id  TEXT REFERENCES source_query(id)
);
CREATE INDEX ix_balance_addr ON balance_snapshot(address_id);

CREATE TABLE entity (
  id                      TEXT PRIMARY KEY,
  name                    TEXT,          -- NULL for auto co-spend clusters
  entity_type             TEXT,
  origin                  TEXT NOT NULL CHECK (origin IN ('cospend-cluster','source','investigator')),
  merged_into             TEXT REFERENCES entity(id),   -- tombstone; resolution chases this (decision #3)
  canonical_membership_id TEXT,          -- APP-ENFORCED ref to entity_membership(id) (decision #10)
  created_at              TEXT NOT NULL
);
CREATE INDEX ix_entity_merged_into ON entity(merged_into);

CREATE TABLE entity_membership (
  id               TEXT PRIMARY KEY,
  entity_id        TEXT NOT NULL REFERENCES entity(id),
  address_id       TEXT NOT NULL REFERENCES address(id),
  source           TEXT NOT NULL,        -- arkham|cospend-heuristic|same-address-heuristic|investigator
  method           TEXT NOT NULL,        -- shared-label|co-spend|same-address-heuristic|manual
  confidence       REAL,
  flags            TEXT,                 -- e.g. 'possible-coinjoin'
  created_at       TEXT NOT NULL,
  source_query_id  TEXT REFERENCES source_query(id)   -- nullable for investigator
);
CREATE INDEX ix_membership_entity ON entity_membership(entity_id);
CREATE INDEX ix_membership_addr   ON entity_membership(address_id);

CREATE TABLE entity_membership_retraction (   -- append-only retraction (decision #3)
  id               TEXT PRIMARY KEY,
  membership_id    TEXT NOT NULL REFERENCES entity_membership(id),
  reason           TEXT NOT NULL,        -- e.g. 'missed-coinjoin'
  source           TEXT NOT NULL,
  method           TEXT,
  created_at       TEXT NOT NULL,
  source_query_id  TEXT REFERENCES source_query(id)
);
CREATE INDEX ix_retraction_membership ON entity_membership_retraction(membership_id);
