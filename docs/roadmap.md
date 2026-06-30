# Roadmap — build order & dependency gates

Work phases **in order**. A phase starts only when the previous phase meets the Definition of Done
(`CLAUDE.md` §5). Each phase has its own doc in `docs/phases/phase_NN_*.md` with step-by-step
instructions, acceptance criteria, and the tests/audits it must add.

Sequencing rationale: de-risk the two hardest bets — the **account/UTXO unification** and **entity
resolution** — early, and stand up the **test/audit harness before any data model** so guardrails exist
from line one.

| Phase | Deliverable | Depends on | Hard gate to exit |
|---|---|---|---|
| 0 | Scaffolding: repo layout, `pyproject.toml`, `Makefile`, CI, empty test/audit harness, `yoyo` migration runner, config + keyring, `PROGRESS.md` | — | `make setup`, `make test` (empty green), `make audit` (no-op green) all run |
| 1 | Data model + provenance spine + finality + idempotency + canonicalization + shared-cache copy-in | 0 | All tables/views created via migrations; invariant audits implemented and green on a hand-seeded case.db |
| 2 | EVM connector end-to-end (Etherscan V2) with `bounds` + cassettes | 1 | Golden EVM address ingests → canonical rows + provenance; idempotent re-fetch; contract test vs cassette green |
| 3 | Bitcoin connector (Esplora) + finality; then create `v_value_movement` view | 1, 2 | Golden BTC tx ingests as tx_input/tx_output; transaction-as-node; view returns unified rows with **null src for UTXO** |
| 4 | Graph surface (React + Cytoscape) reading the view | 3 | Heterogeneous graph renders from a real case.db; provisional facts visibly flagged |
| 5 | Valuation (DeFiLlama) on transfers + outputs | 3 | Value-at-time claims attached at block ts; missing/low-confidence handled honestly |
| 6 | Entity resolution: co-spend union-find, auto cluster-entities, CoinJoin flagging, merge/split, retraction, canonical/contested display | 3 | Co-spend cluster forms; CoinJoin flagged; merge then split round-trips; no `merged_into` cycles |
| 7 | Import parsers (Arkham, MisTrack) + side-by-side risk/attribution | 1 (parallelizable after 6) | Bespoke parsers produce canonical claims; multi-source shown side-by-side; no collapse |
| 8 | Investigator layer: traces (FIFO + manual override), findings, annotations, tags | 3, 6 | FIFO trace produced as labeled claim; manual override; conservation property test green |
| 9 | Reporting (Playwright): immutable snapshots, scope bounds recorded, supersession | 4, 5, 8 | Report PDF renders live graph; `content_hash` set; supersession represented; bounds in `scope_spec` |
| 10 | Case export: hash manifest + `.casefile` bundle; final full-system audit | all | Export produces verifiable `manifest.json` + zip; full `make audit && make smoke` green |

## Cross-cutting rules (every phase)

- **Migrations forward-only.** New schema = new numbered migration; never edit an applied one; bump
  `case_meta.schema_version`.
- **Provenance is atomic.** Any code path that writes a fact/claim writes its `source_query` in the same
  transaction (`provenance/atomic.py` helper from Phase 1).
- **Cassettes are fixtures.** Recorded raw responses live in `tests/cassettes/` and double as provenance
  fixtures; contract tests replay them so the suite is deterministic and offline.
- **Audit after data changes.** Run `make audit` after any step that writes rows; a phase is not done if
  audits fail.
- **Update `PROGRESS.md`** at the end of each phase (status, decisions, follow-ups, any volatile facts
  confirmed against live docs).

## Parallelism notes

Phases 4 and 5 both depend on 3 and can be built in either order. Phase 7 (imports) depends only on the
claim tables from Phase 1 and can proceed alongside 6. Everything funnels into 9 (reporting) and 10
(export). Keep the strict gate on 0→1→2→3; that chain is the spine.
