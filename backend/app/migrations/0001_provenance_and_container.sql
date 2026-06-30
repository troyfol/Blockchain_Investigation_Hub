-- Provenance spine and single-row case container come first: facts FK to source_query.
-- (docs/schema.md §3 — copied verbatim; forward-only, never edit once applied.)

CREATE TABLE case_meta (              -- 'case' is reserved-ish; use case_meta. Single row.
  id              TEXT PRIMARY KEY,
  title           TEXT NOT NULL,
  description     TEXT,
  status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed','archived')),
  schema_version  INTEGER NOT NULL,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE TABLE source_query (
  id                 TEXT PRIMARY KEY,
  connector          TEXT NOT NULL,
  capability         TEXT NOT NULL,
  endpoint           TEXT NOT NULL,
  params             TEXT,                 -- JSON; INCLUDES applied expansion bounds (decision #2)
  requested_at       TEXT NOT NULL,
  completed_at       TEXT,
  status             TEXT NOT NULL CHECK (status IN ('ok','error','partial')),
  raw_response_ref   TEXT,                 -- path under raw_responses/
  raw_response_hash  TEXT,                 -- SHA-256 of the raw response (decision #5)
  result_summary     TEXT
);

CREATE TABLE exhibit (
  id            TEXT PRIMARY KEY,
  exhibit_type  TEXT NOT NULL CHECK (exhibit_type IN ('screenshot','file','export')),
  source        TEXT,
  captured_at   TEXT NOT NULL,
  file_ref      TEXT NOT NULL,            -- path under exhibits/
  content_hash  TEXT NOT NULL,
  description   TEXT
);
