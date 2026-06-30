-- depends: 0001_provenance_and_container
-- Raw on-chain facts (Family A). EVM uses `transfer`; Bitcoin uses tx_output/tx_input only
-- (Invariant #5 — never synthesize an input->output transfer). (docs/schema.md §3, verbatim.)

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
