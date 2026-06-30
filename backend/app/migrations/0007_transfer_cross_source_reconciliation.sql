-- depends: 0006_graphsense_entity_external_id
-- Cross-source transfer reconciliation (docs/findings/arkham_export_reconciliation.md decision (c)).
--
-- A `transfer` is a FACT, so the SAME on-chain movement reported by two sources must dedup to ONE row,
-- never double-count. The old key `(transaction_id, transfer_type, position)` can't do this: `position`
-- is source-dependent (Etherscan = receipt-log order; Arkham/Bitquery = CSV/row order), so the same
-- movement gets different positions from different sources -> a duplicate row, AND different movements
-- could collide on the same position. Re-key dedup on the movement's CONTENT plus an `occurrence` ordinal
-- that disambiguates legitimately-identical movements within a (tx, transfer_type). `position` is kept as
-- a source-reported display ordinal (the real log order is still meaningful for Etherscan). Genuinely
-- DISAGREEING facts (different parties/amount) have different content -> distinct rows -> kept
-- side-by-side, never silently collapsed (Invariant #4); matching movements dedup (Invariant #7).

ALTER TABLE transfer ADD COLUMN occurrence INTEGER NOT NULL DEFAULT 0;

-- Backfill `occurrence` for ANY pre-existing rows so identical-content movements (which used to coexist
-- via distinct `position` values) get distinct occurrences — otherwise the new UNIQUE INDEX below would
-- fail to build on a populated case.db. Rank within each content group by the old display order. No-op on
-- a fresh DB (forward-only safety).
WITH ranked AS (
  SELECT id, ROW_NUMBER() OVER (
    PARTITION BY transaction_id, transfer_type, COALESCE(from_address_id, ''),
                 COALESCE(to_address_id, ''), asset_id, amount
    ORDER BY position, id) - 1 AS occ
  FROM transfer)
UPDATE transfer SET occurrence = (SELECT occ FROM ranked WHERE ranked.id = transfer.id);

DROP INDEX ux_transfer;
CREATE UNIQUE INDEX ux_transfer ON transfer(
  transaction_id, transfer_type, COALESCE(from_address_id, ''), COALESCE(to_address_id, ''),
  asset_id, amount, occurrence);
