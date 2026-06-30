-- depends: 0005_read_model_views
-- Phase C (GraphSense ActorPacks): a source-origin entity carries the upstream actor id, so an
-- `actor` reference in a TagPack tag resolves to the SAME entity idempotently (Invariant #7),
-- regardless of whether the ActorPack or the TagPack was ingested first. Nullable — only
-- origin='source' entities set it; cospend-cluster / investigator entities leave it NULL.
-- (docs/connectors.md §6; docs/findings/graphsense_tagpack_reconciliation.md)

ALTER TABLE entity ADD COLUMN external_id TEXT;
CREATE INDEX ix_entity_external_id ON entity(external_id);
