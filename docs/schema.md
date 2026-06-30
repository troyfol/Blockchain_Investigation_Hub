# Schema — DDL, migrations, views

Engine: **SQLite per case** (`case.db`). Migrations via **yoyo-migrations** (raw SQL files
`backend/app/migrations/NNNN_*.sql`). This doc is the source of truth for the schema; the DDL below is
copied verbatim into migration files in Phase 1. Forward-only; never edit an applied migration.

> Reflects the ten settled Tier 1 decisions. See `docs/overview.md` for rationale and the original
> design spec for the full argument.

## 1. Conventions

- **PRAGMAs (per connection):** `PRAGMA foreign_keys = ON;` `PRAGMA journal_mode = WAL;`
  `PRAGMA busy_timeout = 5000;`
- **IDs:** `id TEXT PRIMARY KEY` holding a UUIDv4 string. Portable across merged case files.
- **Amounts:** raw base-unit integers as **TEXT** (satoshi, wei). Compute with Python `int`/`Decimal`.
- **Timestamps:** UTC **ISO-8601 text** (`YYYY-MM-DDTHH:MM:SSZ`), one format everywhere. *Local-clock —
  see overview §7 caveat.*
- **Booleans:** `INTEGER` 0/1.
- **Enums:** enforced with `CHECK (col IN (...))`.
- **Provenance:** `source_query_id` FK on every fact/claim; nullable only on investigator-authored rows
  (marked). A fact/claim and its `source_query` are written in the **same transaction**.
- **Idempotency:** every raw fact has a natural `UNIQUE` key; ingest does `INSERT ... ON CONFLICT(...)
  DO UPDATE` (upsert), never blind insert.

## 2. Finality model (decision #1)

Finality lives on `transaction`; child facts inherit it (no own finality column). On each fetch the
normalization layer sets `confirmations` (tip height − block_height) and `finality_status`:
`provisional` until `confirmations ≥ threshold(chain)`, then `final`.

- **`provisional`** transactions and their children MAY be updated/deleted on re-fetch (reorg /
  replacement). Upsert handles this.
- **`final`** transactions and their children are immutable; the `make audit` "final-immutability" check
  treats any change to a final row as a failure.

**Default thresholds (CONFIRM at build — policy knobs in `config.py`):**

| Chain | Default threshold | Note |
|---|---|---|
| bitcoin | 6 confirmations | Long-standing convention. |
| ethereum (chainid 1) | 64 blocks (~2 epochs) | Proxy for the consensus `finalized` checkpoint (~12.8 min). Prefer a node/explorer `finalized` tag if available; Etherscan exposes confirmations, so the block-count proxy is the v1 default. |
| L2s (Arbitrum/Optimism/Base/Polygon/…) | conservative per-chain count | L2 finality depends on L1 settlement; set per chain in config. Confirm current guidance per chain. |

## 3. Migrations & DDL

### `0001_provenance_and_container.sql`

```sql
-- Provenance spine and single-row case container come first: facts FK to source_query.
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
```

### `0002_onchain_facts.sql`

```sql
CREATE TABLE asset (
  id                TEXT PRIMARY KEY,
  chain             TEXT NOT NULL,
  contract_address  TEXT,                 -- NULL = native coin; EVM stored lowercase-canonical
  symbol            TEXT,
  decimals          INTEGER NOT NULL,
  source_query_id   TEXT REFERENCES source_query(id)
);
-- Treat NULL contract as native: enforce uniqueness with a coalesced expression index.
CREATE UNIQUE INDEX ux_asset_native ON asset(chain, COALESCE(contract_address,''));

CREATE TABLE address (
  id               TEXT PRIMARY KEY,
  chain            TEXT NOT NULL,
  address          TEXT NOT NULL,         -- CANONICAL form (decision #8)
  address_display  TEXT,                  -- original source form (e.g. EVM checksum)
  first_seen_ts    TEXT,
  source_query_id  TEXT REFERENCES source_query(id)
);
CREATE UNIQUE INDEX ux_address ON address(chain, address);

CREATE TABLE transaction_ (            -- 'transaction' is reserved in SQL; use transaction_
  id               TEXT PRIMARY KEY,
  chain            TEXT NOT NULL,
  tx_hash          TEXT NOT NULL,
  block_height     INTEGER,              -- NULL = unconfirmed/mempool
  block_ts         TEXT,
  fee              TEXT,
  status           TEXT,                 -- e.g. EVM success/fail
  confirmations    INTEGER,              -- decision #1
  finality_status  TEXT NOT NULL DEFAULT 'provisional'
                     CHECK (finality_status IN ('provisional','final')),
  source_query_id  TEXT REFERENCES source_query(id)
);
CREATE UNIQUE INDEX ux_transaction ON transaction_(chain, tx_hash);

CREATE TABLE transfer (               -- EVM value movements
  id               TEXT PRIMARY KEY,
  transaction_id   TEXT NOT NULL REFERENCES transaction_(id),
  chain            TEXT NOT NULL,
  from_address_id  TEXT REFERENCES address(id),   -- NULL for mint
  to_address_id    TEXT REFERENCES address(id),   -- NULL for burn
  asset_id         TEXT NOT NULL REFERENCES asset(id),
  amount           TEXT NOT NULL,
  transfer_type    TEXT NOT NULL CHECK (transfer_type IN ('native','erc20','internal')),
  position         INTEGER NOT NULL,              -- log index or internal-call index
  source_query_id  TEXT REFERENCES source_query(id)
);
CREATE UNIQUE INDEX ux_transfer ON transfer(transaction_id, transfer_type, position);  -- decision #9
CREATE INDEX ix_transfer_from ON transfer(from_address_id);
CREATE INDEX ix_transfer_to   ON transfer(to_address_id);

CREATE TABLE tx_output (              -- Bitcoin; defined before tx_input (input FKs prev_output)
  id               TEXT PRIMARY KEY,
  transaction_id   TEXT NOT NULL REFERENCES transaction_(id),
  address_id       TEXT REFERENCES address(id),   -- NULL for non-standard scripts
  amount           TEXT NOT NULL,
  output_index     INTEGER NOT NULL,
  spent            INTEGER NOT NULL DEFAULT 0,
  spending_tx_id   TEXT REFERENCES transaction_(id),
  source_query_id  TEXT REFERENCES source_query(id)
);
CREATE UNIQUE INDEX ux_tx_output ON tx_output(transaction_id, output_index);   -- decision #9
CREATE INDEX ix_tx_output_addr ON tx_output(address_id);

CREATE TABLE tx_input (
  id               TEXT PRIMARY KEY,
  transaction_id   TEXT NOT NULL REFERENCES transaction_(id),
  prev_output_id   TEXT REFERENCES tx_output(id),  -- NULL if spent output not in-DB
  address_id       TEXT REFERENCES address(id),
  amount           TEXT NOT NULL,
  input_index      INTEGER NOT NULL,
  source_query_id  TEXT REFERENCES source_query(id)
);
CREATE UNIQUE INDEX ux_tx_input ON tx_input(transaction_id, input_index);       -- decision #9
CREATE INDEX ix_tx_input_addr ON tx_input(address_id);
```

### `0003_sourced_claims.sql`

```sql
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
```

### `0004_investigator_objects.sql`

```sql
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
```

### `0005_read_model_views.sql`

```sql
-- Unified value-movement projection over the truthful-asymmetric base tables (decision #4).
-- EVM: a transfer is a directed A->B movement. BTC: an output is value arriving at an address;
-- src_address_id is DELIBERATELY NULL because which input funded it is NOT a ledger fact.
CREATE VIEW v_value_movement AS
  SELECT
    'evm'        AS paradigm,
    tr.id        AS movement_id,
    'transfer'   AS movement_kind,
    tr.transaction_id,
    tr.chain,
    tr.from_address_id AS src_address_id,
    tr.to_address_id   AS dst_address_id,
    tr.asset_id,
    tr.amount,
    tr.position,
    tx.finality_status
  FROM transfer tr
  JOIN transaction_ tx ON tx.id = tr.transaction_id
  UNION ALL
  SELECT
    'utxo'       AS paradigm,
    o.id         AS movement_id,
    'tx_output'  AS movement_kind,
    o.transaction_id,
    tx.chain,
    NULL         AS src_address_id,          -- never fabricate input->output
    o.address_id AS dst_address_id,
    (SELECT a.id FROM asset a
       WHERE a.chain = tx.chain AND a.contract_address IS NULL) AS asset_id,  -- native coin
    o.amount,
    o.output_index AS position,
    tx.finality_status
  FROM tx_output o
  JOIN transaction_ tx ON tx.id = o.transaction_id;

-- Optional helper: per-address net flow with USD when valued. Built on the view above.
CREATE VIEW v_address_flow AS
  SELECT
    m.dst_address_id AS address_id,
    m.chain,
    m.asset_id,
    m.amount,
    v.value AS usd_value
  FROM v_value_movement m
  LEFT JOIN valuation v
    ON v.subject_type = (CASE m.paradigm WHEN 'evm' THEN 'transfer' ELSE 'tx_output' END)
   AND v.subject_id   = m.movement_id;
```

## 4. Idempotency keys (decision #9) — summary

| Table | Natural unique key | Upsert target |
|---|---|---|
| asset | `(chain, COALESCE(contract_address,''))` | symbol/decimals refresh |
| address | `(chain, address)` | display/first_seen refresh |
| transaction_ | `(chain, tx_hash)` | confirmations/finality/status refresh |
| transfer | `(transaction_id, transfer_type, position)` | — (insert-once when final) |
| tx_output | `(transaction_id, output_index)` | spent/spending_tx refresh |
| tx_input | `(transaction_id, input_index)` | prev_output linkage refresh |

Claims (Family B) are **append-only** — no upsert; each fetch is a new row (preserve disagreement).

## 5. Application-enforced references (no DB FK)

SQLite can't express variable-target FKs; the application enforces these and `make audit` checks them:
`valuation.subject_id`, `finding_ref.ref_id`, `annotation.target_id`, `tag.target_id`, and
`entity.canonical_membership_id` (kept app-enforced to avoid an entity↔entity_membership circular FK).

## 6. Shared library cache (separate DB — NOT in any case folder)

A separate SQLite DB caching cross-case claims/assets/prices keyed by natural keys, plus
`cached_at`/`ttl`. On use, the relevant claim rows **and their originating `source_query` rows (with
`raw_response_hash`)** are copied into the active `case.db` so the case is self-contained and provenance
FKs resolve. The cache is a performance optimization only — never a runtime dependency of an opened case,
and copying from cache is itself **not** a `source_query`. (No parquet — decision #7.)
