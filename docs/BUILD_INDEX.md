# Blockchain Investigation Hub — BUILD PACKAGE INDEX

> **Relocated (Phase 10, 2026-06-27):** the build is complete. This build-instruction index was moved
> here from the repo root so the end-user product `README.md` could take the root. It is kept verbatim
> as the historical build map; the live state is in `PROGRESS.md`, the product overview in the root
> `README.md`.

**You (Claude Code) are about to build a project from this package.** This file is the map. Read it
first, then read `CLAUDE.md`, then start Phase 0. Everything you need is in these docs — no outside
information is required, though some volatile facts are flagged **CONFIRM-AT-BUILD** (re-check against
live docs when you reach them).

> **Naming note:** this root `README.md` is the *build-instruction index* — your first-read map through
> the build. The end-user *product* README is generated later in **Phase 10**, also at the repo root.
> To avoid overwriting this index, **Phase 10 first relocates this file to `docs/BUILD_INDEX.md`**, then
> writes the product `README.md`. Until then, this index stays at the root.

---

## ▶ Start here (read in this order)

1. **`README.md`** ← you are here (the map).
2. **`CLAUDE.md`** — the always-on build contract: the 8 non-negotiable **Invariants**, tech stack, repo
   layout, Makefile commands, and the **Definition of Done** every phase must meet. Re-read its §1
   Invariants before each phase.
3. **`docs/overview.md`** — the "why": problem, scope, the account/UTXO unification, the three object
   families. Read once for context.
4. **`docs/roadmap.md`** — the phase sequence (0→10) and dependency gates. Your top-level plan.
5. **`docs/phases/phase_00_scaffolding.md`** — begin building.

Reference docs (open when a phase tells you to):
`docs/schema.md`, `docs/connectors.md`, `docs/algorithms.md`, `docs/testing.md`.
Live build state: **`PROGRESS.md`** — update it at the end of every phase.

---

## 🗂 File map

| File | What it is | When you need it |
|---|---|---|
| `CLAUDE.md` | Build contract: invariants, stack, layout, commands, Definition of Done | **Always** (auto-loaded); re-read §1 each phase |
| `README.md` | This navigation index | First |
| `PROGRESS.md` | Build journal — phase status, decisions, confirmed volatile facts | Update at end of every phase; read at start of every session |
| `docs/overview.md` | Architecture & rationale | Once, for context |
| `docs/roadmap.md` | Phase order + dependency gates | Planning; between phases |
| `docs/schema.md` | Full SQLite DDL (migrations `0001–0005`) + views + idempotency keys + finality model | Phase 1 (and any schema touch) |
| `docs/connectors.md` | Capability interface, endpoint contracts, field→canonical mapping, bounds, backoff, cassettes | Phases 2, 3, 5, 7 |
| `docs/algorithms.md` | Canonicalization, finality calc, valuation precision, co-spend union-find, CoinJoin detection, FIFO apportionment | Phases 1, 5, 6, 8 |
| `docs/testing.md` | Test layers, the 10 invariant audits, golden smoketests, property tests, CI gates | Every phase |
| `docs/phases/phase_NN_*.md` | Per-phase build instructions + acceptance checklist + tests/audits to add | One per phase, in order |

---

## 🔁 How to work each phase (the build loop)

```
START session → run `make audit && make smoke` (confirm no regression)
                read PROGRESS.md (where am I?) → open the current phase doc
  ┌─ for each phase, in roadmap order ─────────────────────────────────┐
  │ 1. Re-read CLAUDE.md §1 Invariants (restated atop the phase doc).   │
  │ 2. Check Prerequisites gate — prior phase must meet Definition of   │
  │    Done. Do NOT start a phase until its dependencies are green.     │
  │ 3. Build the steps. After any step that writes data: `make audit`.  │
  │ 4. Add the phase's tests + wire its invariant audit(s).             │
  │ 5. Confirm any CONFIRM-AT-BUILD items vs live docs; log results.    │
  │ 6. Definition of Done: `make test && make audit && make smoke` all  │
  │    green, no regression → update PROGRESS.md → next phase.          │
  └────────────────────────────────────────────────────────────────────┘
END session → run `make audit && make smoke` again before stopping.
```

Phases are **idempotent/resumable**: if a session ends mid-phase, the next session re-reads the phase doc
+ `PROGRESS.md` and continues. Migrations are **forward-only** — never edit an applied one; bump
`case_meta.schema_version`.

---

## ⛔ The 8 invariants (never violate — full text in `CLAUDE.md` §1)

1. No scraping. 2. Single-user/local. 3. Provenance on every fact (written atomically with it).
4. Never collapse multi-source claims. 5. The schema tells the truth on both chains — **never fabricate a
Bitcoin input→output transfer** (it's a trace-time claim only). 6. Finality before immutability
(provisional tip data is correctable). 7. Idempotent ingest (upsert on natural keys). 8. Canonicalize
addresses on ingest.

If code passes tests but breaks an invariant, it's a bug. Each invariant has at least one audit/test that
fails loudly if broken (`docs/testing.md`).

---

## 📍 Phase index

| # | Phase doc | Headline deliverable | Hard exit gate |
|---|---|---|---|
| 0 | `phase_00_scaffolding.md` | Repo, deps, migrations runner, **empty test/audit harness**, CI | `make setup/test/audit/smoke` run green (no-op) |
| 1 | `phase_01_data_model.md` | All tables/views + provenance spine + finality + idempotency + canonicalization + cache copy-in | Invariant audits green on a seeded case |
| 2 | `phase_02_evm_connector.md` | Etherscan V2 end-to-end with `bounds` + cassettes | Golden EVM ingest; idempotent re-fetch |
| 3 | `phase_03_btc_connector_view.md` | Esplora + finality; create `v_value_movement` | **No fabricated UTXO edge** (audit #5) green |
| 4 | `phase_04_graph_surface.md` | React + Cytoscape reading the view | Heterogeneous graph renders |
| 5 | `phase_05_valuation.md` | DeFiLlama value-at-time | Valued at block ts; missing handled honestly |
| 6 | `phase_06_entity_resolution.md` | Co-spend, CoinJoin flag, merge/split, contested display | Merge/split round-trips; no `merged_into` cycle |
| 7 | `phase_07_import_parsers.md` | Arkham/MisTrack imports; side-by-side risk/attribution | Two sources side-by-side, no collapse |
| 8 | `phase_08_investigator_layer.md` | Traces (FIFO + override), findings, annotations, tags | FIFO conservation property green |
| 9 | `phase_09_reporting.md` | Playwright immutable report snapshots | Report hashed; supersession clean |
| 10 | `phase_10_export_and_readme.md` | `.casefile` export + **product README** + final audit | Export round-trip verifies; full audit green |

---

## ✅ What "done" means for the whole build

Every phase 0–10 meets its Definition of Done; `make test && make audit && make smoke` are green across an
all-phases case; the `.casefile` export round-trips with a valid hash manifest; and the Phase-10 product
README documents the conservative default settings (finality thresholds first) and how to tune them.
