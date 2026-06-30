# Phase 0 — Scaffolding, tooling, harness

> **Invariants (always):** provenance on every fact; never collapse multi-source claims; no scraping;
> single-user local. Full list: `CLAUDE.md` §1. This phase builds the skeleton that lets later phases
> uphold them.

## Goal

Stand up the repository, dependency environment, migration runner, config + keyring, and an **empty but
runnable** test/audit harness — so guardrails exist before any data model.

## Prerequisites

None. This is the first phase.

## Steps

1. Create the repo layout from `CLAUDE.md` §3 (`backend/app/...`, `backend/tests/...`, `frontend/`,
   `docs/`, `cases/` gitignored).
2. `pyproject.toml` — pin deps from `CLAUDE.md` §2 (Python 3.12; FastAPI, Pydantic, httpx, yoyo-migrations,
   keyring, playwright, pytest, hypothesis). Add `frontend/` with Vite + React + Cytoscape.js scaffold.
3. `config.py` — load app config (connector enable/disable, base URLs, paid-tier flags, **per-chain
   finality thresholds** with the defaults from `docs/schema.md` §2). API keys via `keyring`; a single
   loud, explicit dev opt-in env var (`BIH_ALLOW_PLAINTEXT_KEYS=1`) is the only non-keyring path.
4. `db/` — connection helper applying PRAGMAs (`foreign_keys=ON`, WAL, busy_timeout); a yoyo migration
   runner (`make migrate`); `schema_version` read/write against `case_meta`.
5. `audits/` — an audit runner that discovers and runs check functions, prints offending rows, exits
   non-zero on any failure. No checks yet (added in Phase 1) — `make audit` is a green no-op.
6. `Makefile` — implement `setup`, `migrate`, `test`, `audit`, `smoke`, `run` (others stubbed). `test`,
   `smoke` run pytest over (currently empty) suites and pass.
7. CI config — run `make test && make audit && make smoke` on every push.
8. `PROGRESS.md` — mark Phase 0 active, then complete.

## Files to create

`pyproject.toml`, `Makefile`, `.gitignore`, CI config, `backend/app/{config.py,db/__init__.py,
audits/runner.py}`, `frontend/` scaffold, empty `backend/tests/{unit,contract,integration,property,
cassettes,fixtures}/`.

## Acceptance criteria

- [ ] `make setup` installs deps and Playwright Chromium without error.
- [ ] `make migrate` runs against a fresh DB (no migrations yet → no-op OK).
- [ ] `make test`, `make audit`, `make smoke` all run and pass (empty/no-op).
- [ ] `frontend` dev server starts via `make run`.
- [ ] Keyring read/write works; plaintext path requires the explicit env var and logs a warning.

## Confirm-at-build

- Current dependency versions (`CLAUDE.md` §2) — refresh pins if newer stable exists; note in `PROGRESS.md`.

## Before exit (Definition of Done)

All acceptance boxes checked; `make test && make audit && make smoke` green; `PROGRESS.md` updated.
