# Phase 7 — Import parsers + risk/attribution display

> **Invariants (always):** imported claims are **stored raw per source** and **never collapsed** with
> another source's claim; the import path still writes a `source_query` (provenance holds); no scraping —
> imports are human-exported files only. See `CLAUDE.md` §1 #1/#3/#4.

## Goal

Bespoke per-tool import parsers (Arkham, MisTrack) behind the capability interface, plus the side-by-side
multi-source risk/attribution display. Drop-in design so a future API connector replaces the importer.

## Prerequisites

Phase 1 done (claim tables). Parallelizable with Phase 6; needs Phase 6 for membership display.

## Steps

1. **Import connector base** — ingest a human-exported file (CSV/JSON/etc.); store the file as the
   `source_query.raw_response_ref` (hashed); connector name e.g. `arkham-import`, `misttrack-import`.
2. **Arkham parser** (`connectors/imports/arkham.py`) — `get_attributions` → `attribution` (+
   `entity_membership` where the export asserts grouping, `source='arkham'`).
3. **MisTrack parser** (`connectors/imports/misttrack.py`) — `get_risk` → `risk_assessment`,
   `get_attributions` → `attribution` (`source='misttrack'`).
4. **Screenshot-as-exhibit fallback** — attach visually-only data as an `exhibit` (typed `screenshot`,
   hashed).
5. **Side-by-side display** — API/UI surfaces all attributions and all risk rows per address, grouped by
   source, **no synthetic combined score** — this is the never-collapse principle made visible.
6. **Tests** — parser unit tests on sample exports; an audit re-affirming no synthetic combination exists
   (e.g. no code path writes a "combined" row); golden display test showing two sources side-by-side.

## Files to create

`connectors/imports/{base,arkham,misttrack}.py`, `services/claims_display.py`,
`tests/unit/test_arkham_parser.py`, `tests/unit/test_misttrack_parser.py`,
`tests/fixtures/imports/*` (sample exports), `tests/integration/test_multisource_display.py`.

## Acceptance criteria

- [ ] Arkham/MisTrack exports parse into canonical `attribution`/`risk_assessment` rows with provenance.
- [ ] Two sources for one address render side-by-side; no averaged/combined score anywhere.
- [ ] Import writes a `source_query` referencing the stored export file (hashed).
- [ ] Screenshot fallback stored as a hashed `exhibit`.

## Confirm-at-build

- Current Arkham / MisTrack export formats (columns/fields). Log a sample structure in `PROGRESS.md`.

## Before exit (Definition of Done)

`make test && make audit && make smoke` green, no regression; `PROGRESS.md` updated.
