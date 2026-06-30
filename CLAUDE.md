# CLAUDE.md — Blockchain Investigation Hub (build contract)

> This file is auto-loaded every session. It is the always-on contract for building this project.
> Read it fully before any work. If anything you are about to do conflicts with the **Invariants**
> below, stop and resolve the conflict first.

## 0. What this project is

A **provenance-first integration-and-reporting hub** for blockchain investigations. It is **not** an
analytics/clustering engine. It orchestrates data from public tools (some via API, some via structured
manual import), normalizes it into one provenance-first data model, provides an investigation surface,
and emits investigation-grade, reproducible case files.

**New here? Read `README.md` (the build-package index/map) first**, then this file, then start Phase 0.
Full rationale: `docs/overview.md`. Schema: `docs/schema.md`. Build order: `docs/roadmap.md`.
Per-phase instructions: `docs/phases/phase_NN_*.md`. Test/audit strategy: `docs/testing.md`.
Live build state: `PROGRESS.md` (update it as you finish each phase).

## 1. Invariants (NON-NEGOTIABLE — restated atop every phase doc)

1. **No scraping.** Data enters only via official APIs or structured manual import of data a human
   legitimately accessed through a tool's own UI. Never automate against a third-party UI/ToS.
2. **Single-user, local.** No multi-user auth, no server multi-tenancy, no collaboration beyond the
   exported case bundle.
3. **Provenance on every fact.** Every raw fact and sourced claim references the `source_query` that
   produced it. A fact/claim row and its `source_query` row are written in **one DB transaction** —
   never one without the other.
4. **Never collapse multi-source claims.** Different sources may disagree; store all, side-by-side,
   with provenance. No averaged/synthesized risk scores, labels, or valuations. Ever.
5. **The schema tells the truth on both chains.** EVM stores `transfer` (A→B is a fact). Bitcoin stores
   `tx_input`/`tx_output` only; **never** synthesize an input→output transfer as a fact. Input→output
   linkage exists only inside a `trace` as a labeled claim (`basis=fifo|investigator`).
6. **Finality before immutability.** A raw fact is immutable only once its transaction is `final`
   (confirmations ≥ per-chain threshold). `provisional` (tip) facts may be corrected/deleted on
   re-fetch. Never freeze tip data as ground truth.
7. **Idempotent ingest.** Re-fetching the same data upserts on natural keys; it never duplicates.
8. **Canonicalize addresses on ingest.** Store the canonical form in `address.address`; keep the source
   display form in `address.address_display`.

If you ever find code that violates one of these, treat it as a bug regardless of tests passing.

## 2. Tech stack (pinned; confirm at build — see §6)

- **Language/runtime:** Python 3.12.
- **Backend:** FastAPI `0.138.x`, Pydantic `2.13.x`, httpx `0.28.1` (HTTP client). *httpx maintenance
  status is uncertain as of 2026 — confirm before relying on it; the connector base isolates it behind
  one module so it can be swapped.*
- **DB:** SQLite (stdlib `sqlite3`), one `case.db` per case + one shared library cache DB.
- **Migrations:** `yoyo-migrations` (raw-SQL migrations; we are **not** using an ORM). Versioned,
  forward-only by default; `schema_version` tracked in `case_meta`.
- **Frontend:** React `19.2.x` (React 18 LTS acceptable if stability preferred), Cytoscape.js `3.34.x`
  for the graph. `graphology` optional analysis layer.
- **Reporting:** Playwright (Python) `1.60.x`, headless Chromium, renders the live Cytoscape view.
- **Secrets:** `keyring` `25.7.x` (OS keyring). No plaintext key storage; a loud, explicit dev opt-in
  env var is the only exception.
- **Packaging (post-v1):** pywebview one-click launcher. v1 runs FastAPI + React on localhost.

## 3. Repository layout

```
backend/
  app/
    main.py            # FastAPI app
    config.py          # app config + per-chain finality thresholds
    db/                # connection, migration runner, shared-cache copy-in
    migrations/        # yoyo SQL files: NNNN_description.sql
    models/            # Pydantic canonical models (Transaction, Transfer, ...)
    connectors/        # base.py + etherscan.py, esplora.py, defillama.py, imports/
    normalization/     # per-connector adapters -> canonical records
    provenance/        # source_query writer; atomic fact+provenance write helper
    services/          # orchestrator, valuation, entities, traces, reporting, export
    audits/            # runnable invariant checks (make audit)
  tests/
    unit/  contract/  integration/  property/
    cassettes/         # recorded raw responses (double as provenance fixtures)
    fixtures/          # golden case data
frontend/              # React + Cytoscape app
docs/                  # overview, roadmap, schema, connectors, algorithms, testing, phases/
cases/                 # runtime case folders (gitignored)
Makefile  pyproject.toml  PROGRESS.md  CLAUDE.md
```

## 4. Commands (Makefile targets — keep these working)

- `make setup` — install deps, install Playwright Chromium, init dev env.
- `make migrate` — apply pending migrations to a target DB.
- `make test` — full test suite (unit + contract + integration + property).
- `make audit` — run invariant audits against a case.db (structural integrity).
- `make smoke` — run golden integration smoketests end-to-end.
- `make run` — start backend + frontend on localhost.
- `make report CASE=...` — generate a report PDF.
- `make export CASE=...` — hash-manifest + zip a `.casefile`.

## 5. Definition of Done (applies to EVERY phase/task)

A task is **not** done until ALL of the following hold:

1. Code implements the phase's acceptance criteria checklist.
2. `make test` passes (new tests added for new behavior).
3. `make audit` passes against any case.db the phase can produce.
4. `make smoke` passes (no regression in earlier golden cases).
5. The relevant Invariants (§1) are demonstrably upheld — there is at least one test that fails if the
   invariant is broken (e.g., the "no fabricated UTXO edge" audit).
6. `PROGRESS.md` is updated: phase status, key decisions, any follow-ups.
7. Migrations are forward-only and `schema_version` is bumped if the schema changed.

**Session ritual:** at the START of every session run `make audit && make smoke` to confirm nothing
regressed; at the END run them again before marking work done.

## 6. When to confirm against live/external information

The docs bake in stable facts. The following are **volatile** — confirm against the cited live docs
before implementing the affected code, and update the doc + `PROGRESS.md` if reality differs:

- Etherscan V2 rate limits, free-tier chain coverage, exact param/field names (`docs/connectors.md`).
- Esplora endpoint paths and pagination cursor semantics.
- DeFiLlama endpoint shape and coin-key format.
- Current pinned dependency versions (this file §2).
- Per-chain finality thresholds (`docs/schema.md` finality section).

Confirming is allowed and encouraged. Inventing endpoint shapes or limits is not — if unconfirmed, mark
the code path `TODO: confirm` and surface it, don't guess silently.

## 7. How to work through this build

- Work **one phase at a time, in order** (`docs/roadmap.md`). Do not start phase N+1 until phase N meets
  the Definition of Done.
- Each phase doc is self-contained and **resumable** — if a session ends mid-phase, the next session
  re-reads the phase doc + `PROGRESS.md` and continues.
- Prefer small, verifiable steps. After each step that touches data, run `make audit`.
- Keep the connector boundary clean: connectors return canonical records; normalization adapters do the
  mapping; nothing downstream knows a source's native shape.
