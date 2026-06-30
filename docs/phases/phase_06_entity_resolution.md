# Phase 6 — Entity resolution

> **Invariants (always):** memberships are **append-only and may contradict**; never collapse them;
> co-spend over a CoinJoin is **flagged**; entity merge/split uses `merged_into` (memberships not
> rewritten); display is curated-canonical or explicitly **contested**, never silently collapsed. See
> `CLAUDE.md` §1 #4.

## Goal

Validate the resolution model: Bitcoin co-spend clustering at ingest (CoinJoin-flagged), source-label and
same-address memberships, investigator grouping, first-class **merge/split** with retraction, and the
curated-canonical / contested **display** policy.

## Prerequisites

Phase 3 done.

## Steps

1. **Co-spend clustering** (`services/entities.py`) — union-find over Bitcoin input addresses at ingest;
   materialize clusters as `entity(origin='cospend-cluster')` + `entity_membership(method='co-spend')`.
   Per `docs/algorithms.md` §4.
2. **CoinJoin flagging** — `is_probable_coinjoin` (`docs/algorithms.md` §5); flag memberships
   `flags='possible-coinjoin'` + reduced confidence.
3. **Merge/split** — merge sets `entity.merged_into` (tombstone); `resolve(entity)` chases the pointer;
   split creates a new entity + `entity_membership_retraction` on the old + new memberships. No membership
   rewrites.
4. **Other membership sources** — source labels (from imports, Phase 7) and `same-address-heuristic`
   (low confidence, never across EVM/BTC boundary, `docs/algorithms.md` §7); investigator manual grouping.
5. **Display policy** — `entity.canonical_membership_id` when set; else if memberships conflict, render
   **"contested"** with all active claims side-by-side. Expose via API/UI.
6. **Audits/tests** — audit #7 (no `merged_into` cycle; canonical membership belongs to entity); golden
   smoketests "known co-spend cluster" and "known CoinJoin"; merge-then-split round-trip test.

## Files to create

`services/entities.py`, `services/entity_display.py`, `audits/checks/entity_resolution.py`,
`tests/unit/test_union_find.py`, `tests/unit/test_coinjoin_detection.py`,
`tests/integration/test_entities_golden.py`, `tests/integration/test_merge_split.py`.

## Acceptance criteria

- [ ] Co-spend cluster forms over a known tx; memberships `method='co-spend'` with confidence.
- [ ] Known CoinJoin tx → memberships carry `flags='possible-coinjoin'`.
- [ ] Merge sets `merged_into`; resolution chases it; no membership rows rewritten; no cycles (audit #7).
- [ ] Split via retraction + new memberships round-trips; retraction is append-only.
- [ ] Display shows curated-canonical when set, else "contested" with all claims — never auto-collapsed.

## Confirm-at-build

- Current CoinJoin patterns / Whirlpool denominations (tune thresholds). Log it.

## Before exit (Definition of Done)

`make test && make audit && make smoke` green, no regression; `PROGRESS.md` updated.
