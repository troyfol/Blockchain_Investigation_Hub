-- depends: 0009_investigator_label_targets
-- P8.8 clustering heuristics. Two changes, both reuse the existing entity/membership/retraction spine so
-- every new heuristic is a side-by-side, provenance-carrying, REVERSIBLE cluster claim (Invariants #3/#4).
--
-- (1) Widen entity.origin so machine clustering heuristics (BlockSci change-address, Victor EVM) get an
--     HONEST distinct origin ('heuristic-cluster') instead of masquerading as co-spend or investigator.
--     SQLite can't ALTER a CHECK, so the table is rebuilt (mirrors 0009). entity IS referenced by FKs
--     (entity_membership.entity_id, entity.merged_into self-ref); the rebuild preserves every row so no FK
--     is ever orphaned mid-migration (the self-FK resolves against the OLD table during the copy, then
--     against the renamed new table — no row is deleted, so foreign_keys=ON never trips). The specific
--     heuristic is carried on the membership (source/method/confidence), so origin stays coarse.
CREATE TABLE entity_new (
  id                      TEXT PRIMARY KEY,
  name                    TEXT,
  entity_type             TEXT,
  origin                  TEXT NOT NULL CHECK (origin IN ('cospend-cluster','source','investigator','heuristic-cluster')),
  merged_into             TEXT REFERENCES entity(id),
  canonical_membership_id TEXT,
  external_id             TEXT,
  created_at              TEXT NOT NULL
);
INSERT INTO entity_new (id, name, entity_type, origin, merged_into, canonical_membership_id, external_id, created_at)
  SELECT id, name, entity_type, origin, merged_into, canonical_membership_id, external_id, created_at FROM entity;
DROP TABLE entity;
ALTER TABLE entity_new RENAME TO entity;
CREATE INDEX ix_entity_merged_into ON entity(merged_into);
CREATE INDEX ix_entity_external_id ON entity(external_id);

-- (2) erc20_approval — the data the Victor self-authorization heuristic needs (owner approves a spender).
--     DATA-GATED: the Etherscan connector currently fetches txlist/txlistinternal/tokentx (Transfer events)
--     only, NOT Approval events, so this table is populated solely by an explicit import / a future getLogs
--     fetch (TODO: confirm). The self-authorization producer reads it and is a clean no-op when it is empty
--     (an honest "no approval data" result, never a fabricated link). A sourced fact: source_query_id ties
--     each row to the import that produced it (Invariant #3).
CREATE TABLE erc20_approval (
  id                 TEXT PRIMARY KEY,
  chain              TEXT NOT NULL,
  owner_address_id   TEXT NOT NULL REFERENCES address(id),
  spender_address_id TEXT NOT NULL REFERENCES address(id),
  asset_id           TEXT REFERENCES asset(id),
  amount             TEXT,
  block_height       INTEGER,
  tx_hash            TEXT,
  retrieved_at       TEXT NOT NULL,
  source_query_id    TEXT REFERENCES source_query(id)
);
CREATE INDEX ix_erc20_approval_owner   ON erc20_approval(owner_address_id);
CREATE INDEX ix_erc20_approval_spender ON erc20_approval(spender_address_id);
