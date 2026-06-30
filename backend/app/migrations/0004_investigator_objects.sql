-- depends: 0003_sourced_claims
-- Investigator-constructed objects (Family C): traces, findings, annotations, tags, reports.
-- Bitcoin input->output linkage exists ONLY here as a labeled trace claim (Invariant #5).
-- (docs/schema.md §3, verbatim.)

CREATE TABLE trace (
  id           TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  description  TEXT,
  created_at   TEXT NOT NULL
);

CREATE TABLE trace_transfer (         -- EVM edges in a trace
  id           TEXT PRIMARY KEY,
  trace_id     TEXT NOT NULL REFERENCES trace(id),
  transfer_id  TEXT NOT NULL REFERENCES transfer(id),
  ordering     INTEGER,
  note         TEXT
);
CREATE INDEX ix_trace_transfer_trace ON trace_transfer(trace_id);

CREATE TABLE trace_btc_link (         -- Bitcoin trace-time input->output linkage (a CLAIM, not a fact)
  id               TEXT PRIMARY KEY,
  trace_id         TEXT NOT NULL REFERENCES trace(id),
  transaction_id   TEXT NOT NULL REFERENCES transaction_(id),
  source_output_id TEXT NOT NULL REFERENCES tx_output(id),  -- output being spent
  dest_output_id   TEXT NOT NULL REFERENCES tx_output(id),  -- output of the same transaction
  basis            TEXT NOT NULL CHECK (basis IN ('fifo','investigator')),  -- extensible
  confidence       REAL,
  ordering         INTEGER,
  note             TEXT
);
CREATE INDEX ix_trace_btc_link_trace ON trace_btc_link(trace_id);

CREATE TABLE finding (
  id           TEXT PRIMARY KEY,
  statement    TEXT NOT NULL,
  assessment   TEXT,
  created_at   TEXT NOT NULL
);

CREATE TABLE finding_ref (
  id          TEXT PRIMARY KEY,
  finding_id  TEXT NOT NULL REFERENCES finding(id),
  ref_type    TEXT NOT NULL CHECK (ref_type IN
                ('address','transfer','transaction','tx_output','trace','exhibit','entity')),
  ref_id      TEXT NOT NULL,            -- app-enforced poly ref
  note        TEXT
);
CREATE INDEX ix_finding_ref_finding ON finding_ref(finding_id);

CREATE TABLE annotation (
  id           TEXT PRIMARY KEY,
  target_type  TEXT NOT NULL CHECK (target_type IN
                 ('address','transfer','transaction','tx_output','trace','entity','finding')),
  target_id    TEXT NOT NULL,           -- app-enforced poly ref
  content      TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
CREATE INDEX ix_annotation_target ON annotation(target_type, target_id);

CREATE TABLE tag (
  id           TEXT PRIMARY KEY,
  target_type  TEXT NOT NULL CHECK (target_type IN ('address','entity')),
  target_id    TEXT NOT NULL,           -- app-enforced poly ref
  label        TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
CREATE INDEX ix_tag_target ON tag(target_type, target_id);

CREATE TABLE report (
  id                  TEXT PRIMARY KEY,
  title               TEXT NOT NULL,
  generated_at        TEXT NOT NULL,
  scope_spec          TEXT NOT NULL,    -- JSON; includes applied expansion bounds (decision #2)
  rendered_file_ref   TEXT NOT NULL,    -- path under reports/
  content_hash        TEXT NOT NULL,
  supersedes_report_id TEXT REFERENCES report(id)   -- later report supersedes, never edits
);
