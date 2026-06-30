# Blockchain Investigation Hub — Pre-Build Design Specification (v1)

## 0. Instructions for the reviewing model

You are reviewing a pre-build design specification for a software tool. The author is a solo developer (proficient in Python and React, with a financial-crimes / AML background) who will build this. **Your job is to find flaws in the logic and opportunities to optimize the design before any code is written.** Do not write code. Do not produce the build.

This document has two tiers:

- **Tier 1 — Logic**: the problem, scope, canonical-model *concepts*, architecture, connector model, and the design decisions *with their rationale*, plus a list of known risks and open questions.
- **Tier 2 — Schema**: the concrete table definitions, the connector capability interface, the data-source mapping, and a suggested build order.

**Workflow you should follow:**

1. Critique **Tier 1** first. Evaluate the logic for soundness, internal consistency, missing concerns, and optimization opportunities. Challenge the modeling decisions where you see a better approach. Surface anything that will be expensive to change later.
2. Do **not** invest heavily in line-editing Tier 2 yet. The author will renegotiate Tier 1 decisions based on your critique, in conversation with their primary assistant.
3. **After** Tier 1 is renegotiated, you will be asked to re-derive / adjust Tier 2 to match the agreed changes.

**Firm constraints — do not relitigate these:**

- **No scraping.** Data is acquired only via official APIs or via structured manual import of data a human legitimately accessed through a tool's own UI. Automated scraping of third-party platforms is out, both on terms-of-service grounds and because scraped-against-terms data has weak evidentiary provenance.
- **Single-user, local.** v1 is a single-user desktop-class application running locally. No multi-user auth, no server-side multi-tenancy, no collaboration features beyond exporting a portable case bundle.

**Everything else is challengeable**, including the scope boundaries, the chosen stack, and the modeling decisions.

---

# TIER 1 — LOGIC

## 1. Problem statement

A wide range of blockchain-investigation tools are available to the public (Arkham, MetaSleuth, MisTrack, Breadcrumbs, block explorers, etc.). Each is strong in one dimension and blind in another, and the investigator's workflow is a tedious manual loop of switching between them, mentally integrating their conflicting outputs, and producing a write-up by hand. No public tool produces investigation-grade documentation: a defensible, timestamped, reproducible case file.

This tool is an **integration-and-reporting hub**, not another analytics engine. It orchestrates data from the public tools (some via API, some via structured manual import), normalizes it into one provenance-first data model, provides an investigation surface for building the picture, and emits investigation-grade reports that organize into portable case files.

It explicitly does **not** try to rebuild the proprietary data moat (clustering heuristics + off-chain intelligence) that the commercial vendors own. Attribution is a data-moat problem an individual cannot cross; the value here is orchestration, integration, provenance, and reporting on top of data the user legitimately obtains.

## 2. Scope and non-goals (v1)

**In scope for v1:**

- Two chain paradigms from day one: **account-model (EVM)** and **UTXO (Bitcoin)**. EVM coverage spans the chains reachable through a single Etherscan V2 key (~50 EVM chains). Bitcoin via Blockstream Esplora.
- Fund-flow modeling at **transfer granularity** (not just transaction granularity).
- **Value-at-time** (USD) on value movements, sourced from DeFiLlama.
- **Full entity resolution as a first-class data-model concept** — fed by source labels, the Bitcoin co-spend heuristic, and investigator assertions. (See the precise definition in §5; this is *not* an automated proprietary clustering engine.)
- Multi-source **attribution and risk** display, stored raw and surfaced side-by-side.
- **Named, savable traces**; investigator findings, annotations, and tags.
- A default **FIFO tracing heuristic** for Bitcoin input→output linkage, applied as a labeled, reproducible convention (never rendered as ground truth) with manual override. (FIFO is chosen as the one method with both legal pedigree — the rule in Clayton's case, applied to crypto in *D'Aloia v Persons Unknown* — and forensic usefulness; its value is reproducible transparency, not accuracy.)
- **Provenance on every fact**, append-only sourced claims, and **immutable report snapshots** that organize into **self-contained, portable case files**.

**Deferred (future, not permanent exclusions):**

- Tracing conventions other than FIFO (LIFO, pari passu, rolling charge) and automated multi-hop path *discovery*. Haircut and poison taint are exposure scoring, not tracing, and are out of the tracing path.
- Cross-chain bridge association (deposit↔release matching across chains).
- An automated proprietary entity-clustering engine.
- NFT (ERC-721/1155) transfer modeling and valuation.
- Multi-user / collaboration; real-time monitoring and alerting.

## 3. Core design principle

This is a **provenance-first** tool. The distinction between "investigation-grade" and "a viewer" is that every fact carries (a) its source, (b) the timestamp it was retrieved, and (c) a reference to the raw response that produced it, so any conclusion is reproducible and any attribution is defensible.

The model rigidly separates **three families of objects**:

1. **Raw on-chain facts** — what the ledger actually records (addresses, transactions, transfers, UTXO inputs/outputs, assets). Insert-once and immutable; re-fetching is idempotent. These are ground truth.
2. **Sourced claims** — opinions, not facts (attributions/labels, risk scores, valuations, entity memberships). **Append-only**, **many per subject**, each tied to the query that produced it. Different sources are allowed to disagree, and the disagreement is preserved, never collapsed into a single synthesized value.
3. **Investigator-constructed objects** — the human's interpretation (cases, entities-as-resolved-nodes, traces, findings, annotations, tags). Distinct from both the ledger facts and the source claims.

The corollary that drives the whole model: **never collapse multi-source claims.** If Arkham labels an address one way and MisTrack scores it another, both are stored side-by-side with provenance. Collapsing them (e.g., averaging risk scores) manufactures false equivalence between sources that measure differently, and destroys exactly the information an investigator needs.

## 4. Architecture

Four layers, with a provenance spine threaded through all of them.

- **Acquisition layer** — a set of pluggable connectors behind a common capability interface (§6). Connectors come in two flavors: *API connectors* that pull automatically, and *import connectors* that ingest data a human exported from a tool's UI. Both record provenance.
- **Normalization layer** — maps each connector's heterogeneous response into the canonical model. Each connector has an adapter that validates and coerces its source's shape into canonical records.
- **Storage layer** — the case lives here. One self-contained case folder per investigation; a shared library DB acts purely as a performance cache.
- **Investigation surface** — the interactive graph canvas plus annotation/finding/trace capture.
- **Reporting layer** — reads the sourced model and emits immutable report snapshots, which organize into portable case-file bundles.
- **Provenance spine** — a `source_query` log records every external call; every fact and claim references the query that produced it. This is what makes the case reproducible and is the single most important structural element.

## 5. Canonical model — concepts and rationale

### 5.1 Addresses and cross-chain identity

An address is keyed by `(chain, address)`. The same hex string on Ethereum and Arbitrum is two distinct address records. "Same controller across chains" is **not** modeled in the address key; it is a *claim* (an entity membership with a `same-address-heuristic` method), because identical hex across EVM chains usually but not always implies the same controller, and never implies it across the EVM/Bitcoin boundary.

### 5.2 Transactions and transfers — the account/UTXO unification (the central decision)

EVM and Bitcoin model value movement incompatibly. An EVM transfer is one asset, one sender, one recipient. A Bitcoin transaction consumes N inputs (each a prior output tied to an address) and produces M outputs (each to an address), and **the ledger does not record which input funded which output**. That ambiguity is the UTXO tracing problem itself.

The design refuses to fabricate input→output edges, because doing so would assert flows the blockchain never specified and poison every downstream trace with false precision. Instead:

- `transaction` is a first-class node for **both** chains (a chain-agnostic envelope: hash, block, timestamp, fee).
- **For EVM**, value movements are `transfer` rows: direct `from_address → to_address` of an asset, with the transaction as parent. The transfer is the trace primitive and the graph edge.
- **For Bitcoin**, the atomic facts stored are `tx_input` and `tx_output` rows. The system does **not** synthesize address→address transfers. The Bitcoin graph edge is `address → transaction → address`, with the transaction rendered as a **visible routing node**. Any "this input funded that output" linkage is drawn at **trace time** — by a clearly-labeled deterministic convention (FIFO by default in v1) or by manual investigator override — and is stored as part of a *trace*, with its own provenance and `basis` (e.g., `fifo`). It is never stored as a ledger fact.

**Rationale:** the schema tells the truth on both chains. On EVM the ledger says "A sent X to B," and that is stored as fact. On Bitcoin the ledger says only "these addresses funded this transaction; this transaction paid these addresses" — and that is all that is stored as fact. The dollar-level routing on Bitcoin is an investigator/heuristic claim, which is exactly what it is in reality.

**Consequence:** the graph is heterogeneous — EVM produces `address↔address` edges; Bitcoin produces `address↔transaction↔address` with transaction-nodes visible.

**Alternative for the reviewer to weigh:** a fully generalized transfer modeled as "value moved from a source-set to a destination-set within a transaction," where EVM is the degenerate single-element case. This is more uniform but pushes UTXO ambiguity into every row rather than isolating it to Bitcoin. The author leans toward the truthful-asymmetric model above; critique this choice explicitly.

### 5.3 Assets and value-at-time

An `asset` record carries `(chain, contract_address | null, symbol, decimals)`; native coins (BTC, ETH) have a null contract. Amounts everywhere are stored as **raw base-unit integers** (satoshi, wei) in text form to preserve precision; human-readable values are derived via `decimals`.

**Value-at-time** is in scope for v1. It is a *sourced, derived claim* attached to a value movement (a `transfer` on EVM, a `tx_output` on Bitcoin, since that is where Bitcoin value resides), carrying `{unit_price, value, price_timestamp, confidence, source=defillama, retrieved_at, source_query}`. Historical prices are immutable once the block is past, so they are computed once and stored, but still recorded as a sourced claim, not a bare number. Each movement is valued at **its block timestamp** (DeFiLlama is timestamp-granular), and v1 is **USD only**.

### 5.4 Sourced claims (attributions, risk, valuations, memberships, balances)

All claims share the same discipline: append-only, many per subject, each tied to a `source_query`. A risk score pulled today and a different one pulled next month are two rows, both retained with their timestamps. Risk and attribution are stored **raw per source** with **no synthetic combined score**; the UI surfaces them side-by-side. Balance lookups are also claims (point-in-time, sourced), since balances change.

### 5.5 Entities and resolution

Entity resolution is a **first-class, non-deferred data-model concept**, but it is **not an automated proprietary clustering engine** (that is the deferred item and the data moat we do not rebuild).

- An `entity` is a resolved node (a person, organization, exchange, or cluster).
- Addresses join an entity via `entity_membership` rows, which are themselves **sourced claims**: `source` ∈ {a source label e.g. `arkham`, the `cospend-heuristic`, `same-address-heuristic`, or `investigator`}, with a `method`, optional `confidence`, optional `flags`, and (for non-manual sources) a `source_query`. Memberships are **append-only and may contradict** each other.
- The entity is the resolved object; the memberships are the contestable evidence for it. Resolution is *fed* by sources, heuristics, and the investigator's judgment.

**The Bitcoin co-spend heuristic** is the one genuinely free, standard, locally-computable clustering signal, and it pairs directly with the stored UTXO inputs: inputs to a common transaction are presumed same-controller. v1 computes co-spend clusters as `entity_membership` rows (`method=co-spend`) at ingest, each carrying a confidence and respecting known CoinJoin patterns (co-spend over a CoinJoin produces false clusters, so such memberships are flagged). This is real, defensible clustering that needs no data moat.

### 5.6 Investigator layer (traces, findings, annotations, tags)

- A `trace` is a **named, savable sub-selection** within a case — an ordered, annotated set of edges. For EVM, edges reference `transfer` rows. For Bitcoin, edges are the trace-time `source_output → dest_output` linkages described in §5.2, each carrying a `basis` (e.g., `fifo` for the default heuristic, or `investigator` for a manual override; extensible to other conventions) and confidence. One case can hold multiple distinct traces (e.g., "the laundering path" and "the cash-out path").
- A `finding` is a conclusion (statement + the investigator's assessment) with supporting references to any objects (addresses, transfers, transactions, outputs, traces, exhibits, entities).
- An `annotation` is a free-text note attached polymorphically to any object.
- A `tag` is the investigator's own label, kept **distinct from a source `attribution`** — one is the investigator's judgment, the other is a source's claim.

### 5.7 Case / report hierarchy

- A `case` is the top container and owns all of its data. Each case file holds exactly one case (see §9), so the `case` table is single-row metadata and case-scoping is implicit in the file.
- A `report` is a **generated, frozen snapshot** — a rendering of selected case state at a point in time, **immutable once generated**, referencing the rendered file in the case folder. Multiple reports per case are supported (e.g., interim and final), each immutable. This is what serves the "organize multiple reports into case files" goal.

### 5.8 Provenance, exhibits, and cache invisibility

- `source_query` is the spine: every external call writes a row (connector, capability, endpoint, params, timestamps, status, a reference to the stored raw response, a result summary). Every fact and claim references its originating `source_query`.
- `exhibit` holds investigator-attached artifacts (screenshots, manual exports), each with a content hash and capture timestamp. (Automated raw responses live with `source_query`, not as exhibits, to avoid double-modeling.)
- **Cache is invisible to the evidentiary record.** When a cached claim is copied from the shared library into a case, its `source_query` row is copied alongside it, so its provenance reflects the **original** retrieval time, not the cache-hit time. The act of copying from cache is not itself a `source_query`.

## 6. Connector model

### 6.1 Capability interface

Connectors declare which canonical "questions" they answer rather than implementing one fat uniform interface. The address/transaction capability taxonomy is:

- `get_transactions(chain, address)`
- `get_transfers(chain, tx_hash)`
- `get_balance(chain, address)`
- `get_attributions(chain, address)`
- `get_risk(chain, address)`

Value-at-time pricing is a **separate enrichment capability** (`get_price(chain, asset, timestamp)`), because it is keyed by asset+timestamp rather than by address/transaction. The orchestrator dispatches on capability; a connector that lacks a capability simply isn't called for it.

### 6.2 Orchestration

**Lean / on-demand by default.** When an address enters a case, the hub does not automatically fan out to every capable connector. The investigator explicitly requests enrichment ("get risk," "get labels"). An **"enrich all" action** is available to fan out to all capable connectors on demand. This keeps API spend and provenance intentional, and keeps the investigator in control of what gets pulled.

### 6.3 Caching and TTL

Raw on-chain facts cache effectively permanently (they are immutable). Sourced claims carry a **~30-day default TTL** before the UI offers a refresh, and a **manual refresh is always available**. Cache hits preserve original provenance (§5.8).

### 6.4 Import connectors

Because MisTrack and Arkham data enters via manual import in v1 (with their paid APIs anticipated later), the hub uses **per-tool bespoke parsers from day one** — the tools are enumerated, and the manual path is expected to be in use for a while, so investing in real parsers (rather than a lowest-common-denominator generic importer) pays off. A **screenshot-as-exhibit** fallback covers any data a tool exposes only visually. The connector "bones" are built so that swapping an import connector for the same tool's API connector later is a drop-in (same capabilities, same normalized output).

## 7. Implementation choices (with rationale)

- **Graph library: Cytoscape.js.** Investigation graphs are usually curated subsets (dozens to low hundreds of nodes), so raw WebGL scale matters less than interaction richness and built-in layouts/algorithms. Cytoscape is the strongest fit for that profile. `graphology` is available as an analysis layer (centrality, community detection, shortest-path) if/when wanted, independent of the renderer.
- **Frontend: React.** The UI is a stateful canvas plus side panels, forms, and import dialogs — moderately complex and growing — which justifies a framework over vanilla JS, and React has the broader ecosystem and graph-library bindings.
- **Backend: FastAPI.** The core runtime pattern is fanning a single address out to multiple connectors concurrently, which fits async.
- **Runtime/packaging.** v1 is FastAPI serving a React frontend on localhost, opened in the system browser, for fast iteration. This will subsequently be packaged into a one-click launcher wrapped in **pywebview** for a native-window feel.
- **Report engine: Playwright (headless Chromium).** Because it is a real browser, reports render the actual Cytoscape view server-side and capture it at full fidelity (and can embed an interactive HTML appendix), rather than relying on a fragile client-side canvas export. *Known cost:* Playwright bundles Chromium (hundreds of MB), and since the future pywebview wrapper uses the OS webview for the UI, the packaged app ships two rendering engines (OS webview for the app, Chromium for reports). Accepted.
- **Storage tech.** SQLite per case (`case.db`), plus parquet for bulk transaction caching in the shared library cache, plus a shared library DB for cross-case label/risk/price caching. API keys are held in the OS keyring (with plaintext-fallback rejected), never in the database.

## 8. Known risks and open questions for review

These are the soft spots the author is aware of. Scrutinize them and add any you find.

1. **FIFO tracing — presentation discipline (decided: FIFO ships in v1 by default).** The default Bitcoin tracing heuristic is FIFO, producing `basis=fifo` trace claims layered over the truthful fact model (never overwriting facts). Two residual concerns to scrutinize: (a) **presentation discipline** — FIFO output must always render as a named, reproducible *convention*, never as ground-truth flow, or it manufactures exactly the false precision the model otherwise avoids; and (b) **extensibility** — because the case law treats FIFO as accepted but *not the only* method, the architecture must keep other conventions addable, even though only FIFO ships in v1.
2. **Entity display when memberships conflict.** The model stores contradictory memberships, but the UI and reports must *display* a resolved entity. What is the resolution-for-display policy — investigator-curated canonical membership, highest-confidence, most-recent, or explicit "contested" rendering? This is an open design question, not yet decided.
3. **Valuation coverage and confidence.** DeFiLlama coverage can be sparse or lagged for long-tail or very old tokens. Valuation is best-effort, carries a confidence field, and may be missing for some movements. Reports must represent missing/low-confidence valuations honestly.
4. **High-degree nodes / expansion strategy.** Exchange and other high-activity addresses have enormous transaction histories; naively loading full history is infeasible and useless. v1 needs a bounded-expansion strategy (hop limits, time windows, value thresholds, top-N counterparties). This is currently underspecified and is a real gap.
5. **Rate limits and API budget.** Etherscan free tier is ~5 req/s and 100k/day (and as of 2026 covers ~90% of chains free, with some requiring a paid Lite plan); Esplora's free public tier is rate-limited (self-hosting removes this); DeFiLlama core endpoints are free but rate-limited. Concurrent fan-out can hit these. The on-demand-default and throttling mitigate it, but the connector base class needs explicit backoff/quota handling.
6. **Cross-chain same-address heuristic is weak.** Identical hex across EVM chains usually implies the same controller but not always, and the signal is meaningless across the EVM/Bitcoin boundary. Memberships from this method must carry low confidence and clear labeling.
7. **Report immutability vs. corrections.** If a finalized report contains an error discovered later, the immutability rule means a new report supersedes it rather than editing it. Confirm this is the intended evidentiary behavior (it likely is) and that superseding is represented cleanly.

---

# TIER 2 — SCHEMA

Engine: SQLite per case. Identifiers: UUID (text) primary keys throughout, so case files can be merged/synced without collision and provenance cross-references stay portable. Numeric on-chain amounts: stored as TEXT base-unit integers; computed in Python with `int`/`decimal`. Timestamps: UTC, stored as ISO-8601 text or unix integer (choose one consistently). `source_query_id` on a fact/claim is the provenance link; it is nullable only where noted (investigator-authored rows).

## 9. Storage architecture

- **A case is a folder, not a single file.** Layout:
  - `case.db` — the structured model (all tables below). Holds exactly **one** case; the `case` table is single-row metadata; all other tables are scoped to that case by living in this DB. No `case_id` columns are needed on other tables.
  - `raw_responses/` — raw API responses, one file per `source_query`, referenced by `source_query.raw_response_ref`.
  - `exhibits/` — investigator-attached artifacts, referenced by `exhibit.file_ref`.
  - `reports/` — generated report files (PDF), referenced by `report.rendered_file_ref`.
  - The whole folder zips to a single portable `.casefile` bundle; export is "zip the folder."
- **Shared library cache** is a **separate** database (not in any case folder) holding cross-case cached claims, assets, and prices keyed by their natural keys, plus `cached_at`/`ttl` metadata. On use, the relevant claim rows **and their originating `source_query` rows** are copied into the active `case.db` so the case stays self-contained and provenance FKs resolve. The cache is a performance optimization only and is never a runtime dependency of an opened case.
- **App-level config** (connector enable/disable, base URLs, paid-tier flags) lives in an app config store; **API keys live in the OS keyring**.

## 10. Data model — tables

Note on polymorphic references: where a table carries a `*_type` + `*_id` pair (e.g., `valuation.subject_*`, `finding_ref.ref_*`, `annotation.target_*`, `tag.target_*`), the reference is **application-enforced** — SQLite cannot express a foreign key whose target table varies, so referential integrity for these is the application's responsibility.

### Container

**`case`** (single row) — `id`, `title`, `description`, `status`, `created_at`, `updated_at`. The single-row container; all other tables are scoped to this case by living in the same `case.db`.

### Family A — raw on-chain facts (insert-once, idempotent, immutable)

**`asset`** — `id`, `chain`, `contract_address` (nullable; null = native coin), `symbol`, `decimals` (int), `source_query_id` (FK). Unique: `(chain, contract_address)` treating null as native.

**`address`** — `id`, `chain`, `address`, `first_seen_ts` (nullable), `source_query_id` (FK). Unique: `(chain, address)`.

**`transaction`** — `id`, `chain`, `tx_hash`, `block_height` (nullable), `block_ts`, `fee` (text, nullable), `status` (nullable; e.g., EVM success/fail), `source_query_id` (FK). Unique: `(chain, tx_hash)`. Chain-agnostic envelope.

**`transfer`** (account-model / EVM value movements) — `id`, `transaction_id` (FK), `chain`, `from_address_id` (FK→address, nullable for mint), `to_address_id` (FK→address, nullable for burn), `asset_id` (FK→asset), `amount` (text base units), `transfer_type` (enum: `native` | `erc20` | `internal`), `position` (int; log index or internal-call index), `source_query_id` (FK). The EVM trace primitive and graph edge. (NFT types deferred; enum extensible.)

**`tx_input`** (UTXO / Bitcoin) — `id`, `transaction_id` (FK), `prev_output_id` (FK→tx_output, nullable if the spent output isn't in-DB), `address_id` (FK→address, nullable for non-standard scripts), `amount` (text base units), `input_index` (int), `source_query_id` (FK).

**`tx_output`** (UTXO / Bitcoin) — `id`, `transaction_id` (FK), `address_id` (FK→address, nullable for non-standard scripts), `amount` (text base units), `output_index` (int), `spent` (bool), `spending_tx_id` (FK→transaction, nullable), `source_query_id` (FK). Bitcoin value resides here; it is the valuation subject for Bitcoin.

### Family B — sourced claims (append-only; many per subject; each → `source_query`)

**`attribution`** — `id`, `address_id` (FK), `label`, `category` (nullable), `source` (e.g., `arkham`, `misttrack`, `breadcrumbs`, `investigator`), `confidence` (real, nullable), `note` (nullable), `retrieved_at`, `source_query_id` (FK, nullable for investigator-authored).

**`risk_assessment`** — `id`, `address_id` (FK), `score` (real, nullable), `score_scale` (text, e.g., `0-100`), `category` (nullable), `rationale` (nullable), `source`, `retrieved_at`, `source_query_id` (FK). Stored raw per source; no synthetic combination.

**`valuation`** — `id`, `subject_type` (enum: `transfer` | `tx_output`), `subject_id` (UUID; the valued movement), `currency` (default `USD`), `unit_price` (text), `value` (text; unit_price × amount), `price_timestamp`, `confidence` (real; from source), `source` (default `defillama`), `retrieved_at`, `source_query_id` (FK).

**`balance_snapshot`** — `id`, `address_id` (FK), `asset_id` (FK, nullable; null = native/aggregate), `amount` (text), `as_of_ts`, `source`, `retrieved_at`, `source_query_id` (FK). Point-in-time, sourced.

**`entity_membership`** — `id`, `entity_id` (FK→entity), `address_id` (FK→address), `source` (e.g., `arkham`, `cospend-heuristic`, `same-address-heuristic`, `investigator`), `method` (e.g., `shared-label`, `co-spend`, `same-address-heuristic`, `manual`), `confidence` (real, nullable), `flags` (nullable, e.g., `possible-coinjoin`), `created_at`, `source_query_id` (FK, nullable for investigator). Append-only; may contradict.

### Family C — investigator-constructed objects

**`entity`** — `id`, `name`, `entity_type` (nullable), `created_at`. The resolved node; addresses attach via `entity_membership`.

**`trace`** — `id`, `name`, `description` (nullable), `created_at`.

**`trace_transfer`** (EVM edges in a trace) — `id`, `trace_id` (FK), `transfer_id` (FK→transfer), `ordering` (int, nullable), `note` (nullable).

**`trace_btc_link`** (Bitcoin trace-time input→output linkages) — `id`, `trace_id` (FK), `transaction_id` (FK), `source_output_id` (FK→tx_output; the output being spent), `dest_output_id` (FK→tx_output; an output of the same transaction), `basis` (e.g., `fifo`, `investigator`; extensible to other tracing conventions), `confidence` (real, nullable), `ordering` (int, nullable), `note` (nullable).

**`finding`** — `id`, `statement`, `assessment` (nullable; investigator's confidence/wording), `created_at`.

**`finding_ref`** — `id`, `finding_id` (FK), `ref_type` (enum: `address` | `transfer` | `transaction` | `tx_output` | `trace` | `exhibit` | `entity`), `ref_id` (UUID), `note` (nullable).

**`annotation`** — `id`, `target_type` (enum: `address` | `transfer` | `transaction` | `tx_output` | `trace` | `entity` | `finding`), `target_id` (UUID), `content`, `created_at`.

**`tag`** — `id`, `target_type` (enum: `address` | `entity`), `target_id` (UUID), `label`, `created_at`. Investigator's own; distinct from `attribution`.

**`report`** — `id`, `title`, `generated_at`, `scope_spec` (JSON; which traces/findings/addresses/entities were included), `rendered_file_ref` (path in `reports/`), `content_hash`. Immutable once written; a later report supersedes rather than edits.

### Family D — provenance and evidence

**`source_query`** — `id`, `connector` (text), `capability` (e.g., `get_transactions`), `endpoint` (text), `params` (JSON), `requested_at`, `completed_at`, `status`, `raw_response_ref` (path in `raw_responses/`), `result_summary` (nullable). The provenance spine; copied into a case alongside any cached claim it produced.

**`exhibit`** — `id`, `exhibit_type` (enum: `screenshot` | `file` | `export`), `source` (e.g., `arkham-ui`), `captured_at`, `file_ref` (path in `exhibits/`), `content_hash`, `description` (nullable). Investigator-attached artifacts only.

## 11. Capability interface

Each connector implements a subset; the orchestrator dispatches on capability. Return shapes are expressed in canonical terms (the normalization adapter maps the source's native response into these, and writes a `source_query` row plus the resulting fact/claim rows).

- `get_transactions(chain, address) -> [Transaction]` — with associated child records: `transfer[]` for account chains; `tx_input[]` + `tx_output[]` for UTXO chains. (Note: account vs UTXO determines the child shape; the capability signature is uniform.)
- `get_transfers(chain, tx_hash) -> ValueDetail` — the value-movement decomposition of a transaction: `transfer[]` for account chains, `(tx_input[], tx_output[])` for UTXO chains.
- `get_balance(chain, address) -> [BalanceSnapshot]` — native and/or token balances (account chains); funded-minus-spent (UTXO chains).
- `get_attributions(chain, address) -> [Attribution]`.
- `get_risk(chain, address) -> [RiskAssessment]`.
- Enrichment (separate interface): `get_price(chain, asset, timestamp) -> Valuation` — unit price + confidence at the given timestamp.

Pagination note: connectors handle source-specific pagination internally behind these signatures (e.g., Etherscan by block range, Esplora by `last_seen_txid` cursor).

## 12. Connectors and data sources

| Connector | Paradigm | Provides (capabilities) | v1 access | Notes |
|---|---|---|---|---|
| Etherscan V2 | EVM | `get_transactions`, `get_transfers` (native/erc20/internal), `get_balance` | API (free tier) | One key, ~50 EVM chains via `chainid`; ~5 req/s, 100k/day; ~90% chain coverage free, rest needs paid Lite; contract/ABI endpoints free on all tiers. |
| Bitquery | EVM | `get_transactions`, `get_transfers`, `get_balance` | API (free tier) | GraphQL; supplemental/alternative to Etherscan, useful for token-transfer queries. |
| Blockstream Esplora | Bitcoin (UTXO) | `get_transactions`, `get_transfers` (vin/vout), `get_balance` | API (free public tier; self-hostable) | Returns full vin/vout; address `chain_stats` give funded/spent txo sums; paginates 25 confirmed/page by `last_seen_txid`. mempool.space as fallback. |
| DeFiLlama | Pricing | `get_price` (enrichment) | API (free, no key for core) | Historical price at arbitrary unix timestamp, keyed by chain+token-address; returns a 0–1 confidence; covers BTC and EVM tokens. |
| Arkham | Attribution | `get_attributions` (entities/labels) | **Import** (v1); API later | Bespoke import parser from day 1; screenshot-as-exhibit fallback. Bones built so the future API connector is a drop-in. |
| MisTrack | Risk / attribution | `get_risk`, `get_attributions` | **Import** (v1); API later | Bespoke import parser from day 1; same drop-in design for the future API. |

## 13. Build phases (suggested order)

Ordered to **de-risk the two hardest schema bets — the account/UTXO unification and entity resolution — early**, before downstream layers solidify on top of them. Sequence is a suggestion, open to challenge.

1. **Data model + storage foundation** — all tables; the `source_query` spine wired in from the first table; the case-folder layout; the shared library cache with copy-into-case semantics.
2. **EVM connector end-to-end** (Etherscan V2) — `get_transactions`/`get_transfers`/`get_balance`, normalize, store with provenance, retrieve. Validates the account-model path and the provenance loop.
3. **Bitcoin connector** (Esplora) — `tx_input`/`tx_output`, transaction-as-node. Validates the UTXO model and the unification immediately, before anything is built on top.
4. **Graph surface** (Cytoscape + React) reading from storage — renders the heterogeneous graph (address nodes; Bitcoin transaction-nodes; EVM transfer-edges).
5. **Valuation** (DeFiLlama) — value-at-time on transfers and Bitcoin outputs.
6. **Entity resolution** — `entity` + `entity_membership`; Bitcoin co-spend clustering at ingest (with CoinJoin flagging); source-label and same-address memberships; investigator grouping. Validates the resolution model.
7. **Import parsers + risk/attribution display** — Arkham and MisTrack bespoke parsers behind the capability interface; raw multi-source risk/attribution surfaced side-by-side.
8. **Investigator layer** — named traces (including Bitcoin input→output linkages via the default FIFO heuristic, with manual override), findings, annotations, tags.
9. **Reporting** (Playwright) — immutable report snapshots that render the live graph view.
10. **Case-folder export** — the portable `.casefile` bundle.
