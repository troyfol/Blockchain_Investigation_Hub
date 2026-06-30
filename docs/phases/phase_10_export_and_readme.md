# Phase 10 — Case export, README & final audit

> **Invariants (always):** the case folder is **self-contained**; the cache is never a runtime
> dependency; provenance and hashes make the bundle tamper-evident. See `CLAUDE.md` §1.

## Goal

Portable `.casefile` export with a verifiable hash manifest, the project **README** (which must
explicitly discuss the conservative defaults), and a final full-system audit/smoke pass.

## Prerequisites

All prior phases done.

## Steps

1. **Export** (`services/export.py`) — generate `manifest.json` listing SHA-256 of `case.db` and every
   file under `raw_responses/`, `exhibits/`, `reports/`; then zip the folder to `<case>.casefile`. Verify
   on re-open (hashes match; case opens with no cache dependency).
2. **Re-open verification** — opening a `.casefile` validates the manifest and confirms provenance FKs
   resolve entirely within the bundle.
3. **README** (`README.md`) — first **relocate the build index** (`README.md` → `docs/BUILD_INDEX.md`) so
   it isn't overwritten, then write the end-user product README at the repo root. **It MUST include a
   dedicated "Default settings & how to tune them" section** that explicitly discusses every conservative
   default and how to change it
   (`config.py`): per-chain **finality thresholds** (BTC 6, ETH ~64 blocks, L2 per-chain — why
   conservative, how to set to the operator's evidentiary bar); claim **TTL** (~30 days); **expansion
   bounds** defaults; **valuation precision** (Decimal, half-even, 18 sig); CoinJoin detection thresholds;
   co-spend confidence default. State the trust model honestly (local-clock timestamps; non-repudiation
   deferred). *(This is the directive carried in `PROGRESS.md`.)*
4. **Final audit** — run the full `make audit` suite + all golden smoketests across a case that exercised
   every phase; confirm green.

## Files to create

`services/export.py`, `README.md`, `tests/integration/test_export_roundtrip.py`.

## Acceptance criteria

- [ ] Export produces `manifest.json` + `<case>.casefile`; re-open validates all hashes.
- [ ] Exported case is fully self-contained (no shared-cache dependency; all provenance FKs resolve).
- [ ] Build index relocated to `docs/BUILD_INDEX.md`; product `README.md` written at repo root (no
      overwrite of the index).
- [ ] **README includes the "Default settings & how to tune them" section covering all conservative
      defaults (finality thresholds first) and how to change each.**
- [ ] README states the honest trust model (timestamp caveat; deferred non-repudiation).
- [ ] Full-system `make audit && make smoke` green across an all-phases case.

## Confirm-at-build

- None new. Re-confirm any volatile facts touched earlier are logged in `PROGRESS.md`.

## Before exit (Definition of Done)

`make test && make audit && make smoke` green; export round-trip test green; README complete with the
defaults section; `PROGRESS.md` marked complete for all phases.
