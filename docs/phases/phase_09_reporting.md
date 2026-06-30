# Phase 9 — Reporting (Playwright)

> **Invariants (always):** a report is a **frozen, immutable snapshot**; a later report **supersedes**
> (never edits) an earlier one; reports represent missing/low-confidence valuations and the timestamp
> caveat honestly; `scope_spec` records applied **bounds** so the report never implies completeness. See
> `CLAUDE.md` §1 and `docs/overview.md` §7.

## Goal

Immutable report snapshots that render the actual live Cytoscape view at full fidelity (headless
Chromium), with content hash and clean supersession.

## Prerequisites

Phases 4, 5, 8 done.

## Steps

1. **Report service** (`services/reporting.py`) — assemble selected case state (traces/findings/
   addresses/entities) into a render context; capture the **live Cytoscape view** via Playwright headless
   Chromium; emit a PDF into `reports/`; optionally embed an interactive HTML appendix.
2. **Immutability** — write a `report` row: `scope_spec` (incl. applied bounds), `rendered_file_ref`,
   `content_hash` (SHA-256 of the PDF); set `supersedes_report_id` when replacing an earlier report. Never
   edit an existing report.
3. **Honesty** — render missing/low-confidence valuations explicitly; include the local-clock timestamp
   caveat; render entity conflicts as contested where unresolved; FIFO links labeled as convention.
4. **Tests** — golden "report" smoketest: generate a report from a seeded case, assert the row + hash +
   that the PDF exists; supersession test (new report points at the old; old unchanged).

## Files to create

`services/reporting.py`, `report_templates/` (HTML/CSS), `tests/integration/test_report.py`.

## Acceptance criteria

- [ ] Report PDF renders the real graph view (not a fragile client export).
- [ ] `report` row immutable; `content_hash` set; superseding creates a new row, leaves the old intact.
- [ ] `scope_spec` records the expansion bounds applied to the included data.
- [ ] Missing valuations, contested entities, FIFO-as-convention, and the timestamp caveat all represented
      honestly.

## Confirm-at-build

- Playwright Python API (`CLAUDE.md` §2 version); headless Chromium install via `make setup`.

## Before exit (Definition of Done)

`make test && make audit && make smoke` green, no regression; `make report CASE=...` works; `PROGRESS.md`
updated.
