# Blockchain Investigation Hub — v1.3.0

**Release date:** 2026-07-05 · Previous release: v1.2.0.

v1.3.0 is a **capability + usability** release: the full P1–P40 upgrade cycle across ten tracks. It deepens
provenance surfacing, makes cross-source disagreement first-class, completes the trace-building workflow,
hardens the court-facing report, widens source coverage, and puts a serious pass through the investigation UI
and the first-run experience. **The schema advances 6 → 10** (four new forward-only migrations, applied
automatically on open — see *Upgrade notes*). The eight invariants are unchanged and still enforced by the
audit suite (now **11** checks).

The theme throughout: never collapse a multi-source claim, never fabricate a fact or a valuation, and make the
provenance behind every number reachable in one click.

---

## Provenance & disagreement surfacing (Track A)

- **One-click provenance drill-through.** Every sourced claim exposes the exact `source_query` behind it —
  connector, endpoint, params/bounds, retrieval time, and the raw-response hash — without leaving the panel.
- **Disagreement stays visible, never merged.** When two sources disagree on an attribution, a risk level, or a
  valuation, both are shown side-by-side with their own provenance (Invariant #4) — in the panel and in the
  report — rather than averaged into one number.

## Valuation (Track B)

- **Value-at-time with a price cache** and **side-by-side valuations** for a contested movement (each source's
  price shown separately, never averaged; an unpriced movement is an honest gap, never a fabricated $0).
- **Theming & customization.** A Dark / Light / Custom canvas theme system driven by a single shared color-token
  catalog, with a live color-customization editor for the Custom preset. The report always renders its own
  paper-legible palette regardless of the canvas theme.

## Traces (Track C)

- **Complete trace-building workflow, reachable in the UI:** create a named trace, add an EVM transfer as a
  fact, FIFO-apportion a Bitcoin transaction (a labeled convention, never ground-truth flow), add a manual
  within-transaction link, follow guided multi-hop next-hops from the frontier, and assert a cross-chain bridge
  crossing — all as labeled investigator claims. **Trace retraction is append-only** (an audited, tamper-evident
  history), the foundation the rest of the trace tooling builds on.

## Court-ready reporting (Track D)

- The exhibit gained a **methodology section, a cleaner layout, exhibit numbering, cover-page + table-of-contents
  scaffolding, and a glossary** — so an exported case file reads as a formal, self-contained evidentiary document.

## Sources & imports (Track E)

- **Wider source coverage:** transfer-alignment improvements, a structured **risk-detail** table, and an
  **Etherscan CSV import** path for data an investigator legitimately exported from the tool's own UI.
- **Bitquery** and **MisTrack** connectors are wired end-to-end; their live paths are gated on the respective
  API credentials (see *Known remaining issues*).

## Lifecycle, scale & hardening (Tracks F–G)

- Case-lifecycle and scale improvements, plus **declarative case templates** on the New-case screen.
- **Tamper-evidence hardening:** the final-immutability baseline is now anchored **inside** `case.db` (migration
  0014) so it travels with an exported `.casefile`, and the audit suite grew an 11th invariant check
  (`trace-retraction-append-only`).

## Validation (Track H)

- An additional end-to-end **synthetic LEA/FIU case** exercises the full pipeline against a known-answer
  scenario, alongside the existing real-world validation cases.

## UX foundations & polish (Track I)

- **Clearer states:** split loading / action-error handling (a dismissible toast that never blanks the graph),
  honest "unvalued ≠ $0" empty states, and accessible modal dialogs (focus-trap + Esc) across the app.
- **Faster hands:** global keyboard shortcuts and inline rename (no more native prompts).
- **A readable graph:** an on-canvas, context-aware **legend**; a decluttered, grouped header; and — critically
  for color-blind safety — **edges now read on two color-independent channels**: a per-kind arrowhead shape
  (EVM transfer / Bitcoin input / Bitcoin output) and a per-meaning dash family (finality vs trace-convention vs
  heuristic). Both the live legend and the report name these cues.
- **Accessibility:** report greys raised to **WCAG AA** contrast on the printed page, near-duplicate source-badge
  colors re-spaced so adjacent sources stay distinguishable, and small controls enlarged to ≥24px touch targets.
- **Less friction:** the add-address flow defaults to type-address → Enter, with depth options behind an
  "Advanced" disclosure and a clear "Done / Add another" step on success.

## First-run onboarding (Track J)

- **Explore a sample case in one click.** A fresh install now ships a bundled **public Tornado Cash sample
  investigation** and offers "Explore the sample case" on the first-run screen — no keys, no setup. The
  first-run screen also reassures where data lives (on your machine) and points to the free, no-key sources that
  are already on.

## Known remaining issues

- **Code-signing is optional and this release ships unsigned.** The exe + installer build cleanly without a
  certificate; expect a SmartScreen "unknown publisher" prompt (friction, not a block). Signing is a one-line
  config change when a certificate is available — see the README "Distribution" note.
- **Bitquery / MisTrack live paths are credential-gated.** The connectors are implemented; their live-confirm
  step awaits the respective API keys. The MisTrack CSV column mapping remains the assumed layout pending a real
  export.
- **Bulk valuation is rate-limited by the free price tier.** Large cases value in bounded batches and may show
  honest coverage gaps until priced on a fresh window; prices are exact where present.
- **`build_view` deep query (deferred by choice).** Correct and acceptable at the intended case scale; a future
  single-recursive-query rewrite behind golden tests remains the one deliberately-deferred item.

## Upgrade notes (existing case DBs)

- **Schema advances 6 → 10.** Four new forward-only migrations (`0011`–`0014`) apply **automatically** the first
  time v1.3.0 opens an existing case; no manual step. The migrations are additive (trace retraction, risk-detail,
  and the in-DB immutability anchor) — no fact rows are rewritten.
- **The forward-compatibility guard still holds:** a case created by a *newer* build refuses to open rather than
  run against an unknown schema.
- **Existing `.casefile` bundles remain valid** — they re-import and re-verify (hash manifest + audits). A bundle
  exported by an older build re-imports and forward-migrates on open; re-export to refresh its embedded schema +
  immutability anchor.
- **No new runtime dependencies.** The dependency set is unchanged from v1.2.0; exact versions stay pinned in
  `requirements.lock`.
