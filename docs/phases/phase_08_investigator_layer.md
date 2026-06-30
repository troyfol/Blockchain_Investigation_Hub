# Phase 8 — Investigator layer (traces, findings, annotations, tags)

> **Invariants (always):** Bitcoin input→output linkage is a **trace-time claim** with a `basis`, never a
> ledger fact; FIFO output always renders as a named **convention**, never ground-truth flow; tags
> (investigator) stay distinct from attributions (source). See `CLAUDE.md` §1 #5.

## Goal

Named savable traces — including Bitcoin FIFO input→output linkages with manual override — plus findings,
annotations, and tags.

## Prerequisites

Phases 3 and 6 done.

## Steps

1. **Traces** — create/name/save a trace; EVM edges as `trace_transfer` (reference `transfer` rows);
   Bitcoin edges as `trace_btc_link` (`basis='fifo'|'investigator'`).
2. **FIFO tracing** (`services/tracing.py`) — implement `fifo_apportion` per `docs/algorithms.md` §6;
   produce `trace_btc_link` rows with `basis='fifo'` along an investigator-expanded path (not automated
   discovery). Render clearly as a labeled convention.
3. **Manual override** — investigator adds/edits links with `basis='investigator'`.
4. **Findings** — `finding` + `finding_ref` (polymorphic refs to any object). **Annotations** —
   polymorphic `annotation`. **Tags** — `tag` (distinct from `attribution`).
5. **Tests** — property test (conservation: links into an output sum to its amount; out of an input ≤ its
   amount; no negatives); golden "FIFO trace" smoketest vs a hand-computed expected; app-enforced
   poly-ref audit extended to finding_ref/annotation/tag targets.

## Files to create

`services/tracing.py`, `services/investigator.py`, `tests/property/test_fifo_conservation.py`,
`tests/integration/test_fifo_trace_golden.py`, `tests/unit/test_finding_refs.py`.

## Acceptance criteria

- [ ] A FIFO trace produces `trace_btc_link(basis='fifo')` matching a hand-computed expected; conservation
      property green.
- [ ] Manual override produces `basis='investigator'` links; FIFO never overwrites facts.
- [ ] Findings/annotations/tags attach polymorphically; refs resolve (audit green).
- [ ] UI/report renders FIFO explicitly as a convention with its basis, not as ground truth.

## Confirm-at-build

- None new (algorithm is self-contained).

## Before exit (Definition of Done)

`make test && make audit && make smoke` green, no regression; `PROGRESS.md` updated.
