# Phase 1 — Data model + provenance spine + finality + idempotency

> **Invariants (always):** every fact/claim is written **in the same transaction** as its `source_query`;
> a fact is immutable only once `final`; ingest is idempotent (upsert on natural keys); canonicalize
> addresses on ingest; never collapse claims. This phase makes these structurally true.

## Goal

All tables and views from `docs/schema.md`, created via migrations; the provenance spine wired in; the
finality, idempotency, canonicalization, and shared-cache-copy-in mechanics; and the **invariant audits**
that guard them all — green on a hand-seeded `case.db`.

## Prerequisites

Phase 0 done (`make test/audit/smoke` green no-op).

## Steps

1. **Migrations** — copy the DDL from `docs/schema.md` §3 verbatim into `migrations/0001..0005_*.sql`.
   `make migrate` builds a full empty `case.db`. Set `case_meta.schema_version`.
2. **Canonical models** — `models/` Pydantic types: `Asset, Address, Transaction, Transfer, TxInput,
   TxOutput, Attribution, RiskAssessment, Valuation, BalanceSnapshot, Entity, EntityMembership,
   ValueDetail, BalanceSnapshot, SourceQuery, ...`.
3. **Provenance writer** — `provenance/atomic.py`: a `write_with_provenance(conn, source_query, rows)`
   helper that opens one transaction, inserts the `source_query` (+ writes the raw response file and its
   SHA-256 to `raw_response_hash`), then inserts/upserts the fact/claim rows, commits atomically. **All
   ingest goes through this.**
4. **Canonicalization** — `normalization/canonical.py` per `docs/algorithms.md` §1.
5. **Finality** — `normalization/finality.py` per `docs/algorithms.md` §2 (config thresholds).
6. **Idempotent upsert** — repository functions using the natural keys in `docs/schema.md` §4
   (`INSERT ... ON CONFLICT(...) DO UPDATE` for facts; **append-only insert** for claims).
7. **Shared library cache** — separate DB (`docs/schema.md` §6); `copy_into_case()` copies cached claim
   rows **and their `source_query` rows** into the active case; copying is not itself a `source_query`.
8. **Invariant audits** — implement checks #1–4, #6, #8 from `docs/testing.md` §2 (provenance
   completeness, no dangling FKs, idempotency, final-immutability, append-only claims, cache provenance
   carried). Wire into `make audit`.

## Files to create

`migrations/0001..0005_*.sql`, `models/*.py`, `provenance/atomic.py`, `normalization/{canonical,finality}.py`,
`db/repository.py`, `db/shared_cache.py`, `audits/checks/*.py`, `tests/unit/test_canonical.py`,
`tests/unit/test_finality.py`, `tests/integration/test_seeded_case.py`.

## Acceptance criteria

- [ ] `make migrate` builds all tables + `v_value_movement` + `v_address_flow`; `PRAGMA foreign_key_check`
      empty on a seeded DB.
- [ ] A fact cannot be written without a `source_query` (helper enforces; test proves the failure path).
- [ ] Upserting the same seeded record twice yields exactly one row (idempotency audit green).
- [ ] Final rows are frozen; mutating one is caught by the final-immutability audit.
- [ ] Copying a cached claim brings its `source_query`; cache-provenance audit green.
- [ ] All Phase-1 invariant audits green on a hand-seeded case.

## Confirm-at-build

- Per-chain finality thresholds (`docs/schema.md` §2) — set conservatively; record chosen values.

## Before exit (Definition of Done)

`make test && make audit && make smoke` green; seeded-case integration test passes; `PROGRESS.md` updated
(schema_version = 1).
