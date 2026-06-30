# Overview & Architecture — Blockchain Investigation Hub

This is the "why" behind the build. Read it once to understand the model; the day-to-day contract is
`CLAUDE.md`, and the concrete schema is `docs/schema.md`.

## 1. Problem

Public blockchain-investigation tools (Arkham, MetaSleuth, MisTrack, Breadcrumbs, block explorers) are
each strong in one dimension and blind in another. The investigator's loop is manual: switch tools,
mentally integrate conflicting outputs, hand-write a report. No public tool produces **investigation-grade
documentation** — a defensible, timestamped, reproducible case file.

This tool is an **integration-and-reporting hub**, not another analytics engine. It does **not** rebuild
the proprietary data moat (clustering heuristics + off-chain intelligence) the commercial vendors own.
Its value is orchestration, integration, provenance, and reporting on top of data the user legitimately
obtains.

## 2. Scope (v1)

**In scope:** EVM (account model, ~50 chains via one Etherscan V2 key) and Bitcoin (UTXO, via Esplora);
fund-flow at transfer granularity; value-at-time (USD, DeFiLlama); entity resolution as a first-class
data-model concept (fed by source labels, the Bitcoin co-spend heuristic, same-address heuristic, and
investigator assertions — **not** an automated proprietary clustering engine); multi-source attribution
and risk shown side-by-side; named savable traces, findings, annotations, tags; a default FIFO Bitcoin
tracing convention; provenance on every fact; immutable report snapshots organized into portable case
files.

**Deferred (not permanent):** tracing conventions other than FIFO; automated multi-hop path *discovery*;
cross-chain bridge association; an automated clustering engine; NFT modeling; multi-user/collaboration;
real-time monitoring.

## 3. Core principle — provenance-first, three object families

Every fact carries (a) its source, (b) retrieval timestamp, (c) a reference to the raw response that
produced it. The model rigidly separates three families:

1. **Raw on-chain facts** — what the ledger records (addresses, transactions, transfers, UTXO
   inputs/outputs, assets). Immutable **once final**; idempotent re-fetch. Ground truth.
2. **Sourced claims** — opinions, not facts (attributions, risk, valuations, entity memberships,
   balances). **Append-only, many per subject**, each tied to its query. Disagreement is preserved,
   never collapsed.
3. **Investigator-constructed objects** — the human's interpretation (cases, entities as resolved nodes,
   traces, findings, annotations, tags).

**Corollary that drives everything: never collapse multi-source claims.**

## 4. The central modeling decision — account/UTXO unification

EVM and Bitcoin move value incompatibly. An EVM transfer is one asset, one sender, one recipient. A
Bitcoin transaction consumes N inputs and produces M outputs, and **the ledger does not record which
input funded which output** — that ambiguity *is* the UTXO tracing problem.

We refuse to fabricate input→output edges:

- `transaction_` is a chain-agnostic envelope (hash, block, timestamp, fee, finality) — first-class for
  both chains.
- **EVM:** value movements are `transfer` rows (`from_address → to_address` of an asset). The transfer
  is the trace primitive and the graph edge.
- **Bitcoin:** the atomic facts are `tx_input` and `tx_output` rows. No synthesized address→address
  transfer. The graph edge is `address → transaction → address` with the transaction a **visible routing
  node**. Any input→output linkage is drawn at **trace time** by a labeled convention (FIFO default) or
  manual override, stored inside a `trace` with its own `basis` and provenance — never as a ledger fact.

The graph is therefore deliberately heterogeneous. To stop this asymmetry from leaking into every
consumer, a **derived read-model view** (`v_value_movement`) projects both paradigms into one shape for
consumers that don't care about the difference — while the truthful base tables remain the source of
truth (see `docs/schema.md`).

## 5. Architecture layers

- **Acquisition** — pluggable connectors behind a capability interface. Two flavors: *API connectors*
  (pull) and *import connectors* (ingest human-exported data). Both record provenance.
- **Normalization** — per-connector adapters validate and coerce native responses into canonical records.
- **Storage** — one self-contained case folder per investigation; a shared library DB is a pure
  performance cache.
- **Investigation surface** — Cytoscape graph canvas + annotation/finding/trace capture.
- **Reporting** — reads the sourced model, emits immutable report snapshots → portable case bundles.
- **Provenance spine** — a `source_query` log records every external call; every fact/claim references
  it. The single most important structural element.

## 6. Canonical model concepts (detail in schema.md)

- **Address** keyed by `(chain, address)`; "same controller across chains" is a *claim*, not the key.
- **Assets/amounts** stored as raw base-unit integers in TEXT; human values derived via `decimals`.
- **Value-at-time** is a sourced, derived claim on a value movement (EVM `transfer`, BTC `tx_output`),
  valued at the block timestamp, USD only in v1.
- **Entities** are resolved nodes; addresses join via `entity_membership` (itself a sourced claim,
  append-only, may contradict). **Co-spend** clustering runs at ingest (CoinJoin-flagged). Clusters
  evolve, so **entity merge/split is first-class** (mutable entity + `merged_into` tombstone; resolution
  chases the pointer; memberships stay append-only; retraction via `entity_membership_retraction`).
- **Traces** are named savable edge sets. EVM edges → `transfer`; Bitcoin edges → trace-time
  `source_output → dest_output` links with a `basis`.
- **Reports** are frozen immutable snapshots; a later report supersedes (never edits) an earlier one.
- **Provenance/exhibits:** `source_query` is the spine (now with `raw_response_hash`); `exhibit` holds
  investigator artifacts. Cache is invisible to the evidentiary record — copying a cached claim copies
  its original `source_query` so provenance reflects original retrieval time.

## 7. What "investigation-grade" does and does not claim

It guarantees: reproducibility (every conclusion traces to a stored raw response), preserved
disagreement, tamper-evidence (content hashes + export manifest). It does **not** guarantee wall-clock
timestamp truth — timestamps are local-machine and establish ordering/internal consistency, not
notarized time. Cryptographic non-repudiation (signing / timestamp authority) is a named future item.
Reports must state this honestly.
