# Phase 4 — Graph surface (React + Cytoscape)

> **Invariants (always):** the graph is **heterogeneous and truthful** — EVM = address↔address edges;
> Bitcoin = address↔transaction↔address with transaction nodes visible. Render provisional facts as
> visibly provisional. Read via `v_value_movement`, not the base tables. See `CLAUDE.md` §1 #5/#6.

## Goal

An interactive Cytoscape canvas that renders the heterogeneous graph from a real `case.db`, reading the
read-model view so the frontend never branches on chain paradigm.

## Prerequisites

Phase 3 done (view validated).

## Steps

1. **Backend read API** (FastAPI) — endpoints serving graph data from `v_value_movement` (+ `address`,
   `transaction_`, `entity` later): nodes (addresses; Bitcoin transaction-nodes) and edges (EVM transfers;
   Bitcoin address→tx→address). Include `finality_status` so the UI can style provisional elements.
2. **Frontend** — React app embeds Cytoscape.js; node/edge styling: address nodes, Bitcoin transaction
   routing nodes (distinct shape), EVM transfer edges; provisional elements dashed/greyed. Side panel for
   a selected node showing its facts and (later) claims.
3. **Bounded expansion UX** — expanding a node calls the orchestrator with `bounds` (hop/time/value/top-N);
   show when a result is `partial`. (Backed by Phase 2/3 bounds.)
4. **Tests** — a small integration test that loads a seeded case and asserts the API returns the expected
   node/edge counts and that UTXO edges route through transaction nodes.

## Files to create

`backend/app/main.py` graph routes, `backend/app/services/graph.py`, `frontend/src/{App,Graph,SidePanel}.jsx`,
`tests/integration/test_graph_api.py`.

## Acceptance criteria

- [ ] Loading a real case.db renders both EVM and Bitcoin subgraphs correctly (transaction nodes visible
      for BTC).
- [ ] Provisional facts are visually distinct from final.
- [ ] Node expansion respects `bounds` and surfaces `partial` results.
- [ ] Graph API reads the view, not base tables (no paradigm branching in frontend).

## Confirm-at-build

- Cytoscape.js current API (`CLAUDE.md` §2 version). 

## Before exit (Definition of Done)

`make test && make audit && make smoke` green, no regression; `make run` shows the graph; `PROGRESS.md`
updated.
