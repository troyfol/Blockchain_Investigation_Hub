# Blockchain Investigation Hub — Tier 2 Schema (v2, re-derived)

**Status:** Re-derived from the original Tier 2 (§§9–13 of `blockchain_investigation_hub_spec.md`) to reflect the ten settled Tier 1 decisions plus three Tier 2 representation choices. This is a schema specification for review, **not** a build — no code is produced here.

**Firm constraints unchanged:** no scraping; single-user, local; provenance-first; never collapse multi-source claims.

---

## A. Change-log — what moved and why

Each row maps a settled decision to its concrete schema delta.

| # | Tier 1 decision | Schema delta |
|---|---|---|
| 1 | Finality / provisional tip | `transaction` gains `confirmations` + `finality_status` (`provisional`\|`final`); fact children inherit; provisional rows are mutable/deletable until final, then frozen. New "Finality" convention (§B). |
| 2 | Bounded expansion in capability signatures | Address-scoped capabilities take a `bounds` selector; bounds are recorded in `source_query.params` so partiality is reproducible. `report.scope_spec` must record applied bounds. (§E) |
| 3 | Entity merge/split first-class | `entity` gains `origin`, `merged_into` (tombstone), `canonical_membership_id`. New `entity_membership_retraction` table (append-only retraction). Resolution chases `merged_into`. |
| 4 | Derived value-movement read-model | New SQL view `v_value_movement` (and helper views) over `transfer` + `tx_output`. No duplicated storage. (§D) |
| 5 | Tamper-evidence | `source_query` gains `raw_response_hash`; export writes a `manifest.json` of content hashes; timestamp-trust caveat documented (§B, §C). |
| 6 | Keep Playwright | No schema change. |
| 7 | Defer parquet | Shared library cache is **SQLite**; parquet removed from §C. |
| 8 | Address canonicalization on ingest | `address.address` stores the canonical form; `address.address_display` retains the source form. Normalization-layer rule (§B). |
| 9 | Explicit idempotency keys | Natural `UNIQUE` constraints on `transfer`, `tx_input`, `tx_output`; re-fetch upserts. |
| 10 | Curated-canonical + contested display | `entity.canonical_membership_id` (nullable); null ⇒ render "contested". |

Three Tier 2 representation choices that shaped the above: finality lives **on `transaction`** (children inherit); merge/split uses a **mutable entity + `merged_into` tombstone** with pointer-chasing resolution (memberships stay append-only); the read-model is **SQL views** in `case.db`.

---

## B. Engine and conventions

- **Engine:** SQLite per case (`case.db`).
- **Identifiers:** UUID (text) primary keys throughout, so case files merge/sync without collision and provenance cross-references stay portable.
- **Numeric on-chain amounts:** stored as TEXT base-unit integers (satoshi, wei); computed in Python with `int`/`decimal`.
- **Timestamps:** UTC, **ISO-8601 text**, consistently (one format across the schema). *Caveat (decision #5): these are local-machine timestamps; they establish relative ordering and internal consistency, not wall-clock truth. Cryptographic non-repudiation (signing / timestamp authority) is a named future item.*
- **Provenance link:** `source_query_id` on a fact/claim is the provenance FK; nullable only on investigator-authored rows (noted per table).
- **Address canonicalization (decision #8):** the normalization layer canonicalizes before insert. EVM: lowercase hex is canonical (`address.address`); the checksummed source form is kept in `address.address_display` for UI. Bitcoin: normalize per encoding; distinct address strings (legacy base58 vs bech32 etc.) are distinct addresses. This protects the `(chain, address)` unique key and makes re-fetch idempotent.
- **Idempotency (decision #9):** every raw fact has a natural unique key (per table); re-fetch performs an upsert on that key, never a blind insert.
- **Finality (decision #1):** finality is a property of the block, modeled on `transaction`. On every fetch the normalization layer sets `confirmations` (from chain tip) and `finality_status`: `provisional` until `confirmations ≥ chain_finality_threshold` (per-chain, in app config — e.g. Bitcoin 6, EVM chain-specific), then `final`. **Provisional** transactions and their child facts may be updated or deleted on re-fetch (handles reorg / replacement). **Final** transactions and their children are immutable (insert-once). Child facts inherit finality from their parent transaction; they carry no own finality column.
- **Polymorphic references:** where a table carries a `*_type` + `*_id` pair (`valuation.subject_*`, `finding_ref.ref_*`, `annotation.target_*`, `tag.target_*`), referential integrity is **application-enforced** (SQLite cannot express a variable-target FK).
- **Valuation precision:** `valuation.value = unit_price × (amount / 10^asset.decimals)`, computed with Python `Decimal` at fixed precision (round half-even, 18 significant digits), stored as TEXT.

---

## C. Storage architecture (revised §9)

- **A case is a folder, not a single file.** Layout:
  - `case.db` — the structured model (all tables below). Holds exactly **one** case; the `case` table is single-row metadata; all other tables are scoped by living in this DB (no `case_id` columns).
  - `raw_responses/` — raw API responses, one file per `source_query`, referenced by `source_query.raw_response_ref` and hashed in `source_query.raw_response_hash` (decision #5).
  - `exhibits/` — investigator artifacts, referenced by `exhibit.file_ref`.
  - `reports/` — generated PDFs, referenced by `report.rendered_file_ref`.
  - `manifest.json` — **(new, decision #5)** generated at export: content hashes (SHA-256) of `case.db` and every file under `raw_responses/`, `exhibits/`, `reports/`. Makes the bundle tamper-evident.
  - The whole folder zips to a portable `.casefile` bundle; export = hash + zip.
- **Shared library cache** is a **separate SQLite database** (decision #7 — no parquet) outside any case folder, holding cross-case cached claims, assets, and prices keyed by natural keys, plus `cached_at`/`ttl` metadata. On use, the relevant claim rows **and their originating `source_query` rows (with `raw_response_hash`)** are copied into the active `case.db` so the case stays self-contained and provenance FKs resolve. The cache is a performance optimization only, never a runtime dependency of an opened case. (If row-store performance later proves insufficient at scale, the cache format can be swapped without touching the evidentiary model.)
- **App-level config** (connector enable/disable, base URLs, paid-tier flags, **per-chain finality thresholds**) lives in an app config store; **API keys live in the OS keyring** (no plaintext fallback; an explicit, loudly-warned dev opt-in is allowed for headless/Linux sessions where the keyring daemon is absent).

---

## D. Data model — tables (revised §10)

### Container

**`case`** (single row) — `id`, `title`, `description`, `status`, `schema_version`, `created_at`, `updated_at`.

### Family A — raw on-chain facts (insert-once once **final**; provisional near tip; idempotent; provenance-linked)

**`asset`** — `id`, `chain`, `contract_address` (nullable; null = native coin; EVM stored lowercase-canonical), `symbol`, `decimals` (int), `source_query_id` (FK). **Unique:** `(chain, contract_address)` treating null as native.

**`address`** — `id`, `chain`, `address` (**canonical form**, decision #8), `address_display` (nullable; original source form, e.g. EVM checksum), `first_seen_ts` (nullable), `source_query_id` (FK). **Unique:** `(chain, address)`.

**`transaction`** — `id`, `chain`, `tx_hash`, `block_height` (nullable; null = unconfirmed/mempool), `block_ts` (nullable until mined), `fee` (text, nullable), `status` (nullable; e.g. EVM success/fail), **`confirmations`** (int, nullable), **`finality_status`** (enum: `provisional` | `final`), `source_query_id` (FK). **Unique:** `(chain, tx_hash)`. Chain-agnostic envelope; the finality anchor for all child facts (decision #1).

**`transfer`** (account-model / EVM value movements) — `id`, `transaction_id` (FK), `chain`, `from_address_id` (FK→address, nullable for mint), `to_address_id` (FK→address, nullable for burn), `asset_id` (FK→asset), `amount` (text base units), `transfer_type` (enum: `native` | `erc20` | `internal`; extensible), `position` (int; log index or internal-call index), `source_query_id` (FK). **Unique: `(transaction_id, transfer_type, position)`** (decision #9). EVM trace primitive and graph edge. Inherits finality from its transaction. *Normalization note: a full EVM value picture merges three Etherscan endpoints — `txlist` (native), `txlistinternal` (internal), `tokentx` (erc20) — each paginated and each its own `source_query`; the `(transfer_type, position)` key keeps them collision-free.*

**`tx_input`** (UTXO / Bitcoin) — `id`, `transaction_id` (FK), `prev_output_id` (FK→tx_output, nullable if the spent output isn't in-DB), `address_id` (FK→address, nullable for non-standard scripts), `amount` (text base units), `input_index` (int), `source_query_id` (FK). **Unique: `(transaction_id, input_index)`** (decision #9).

**`tx_output`** (UTXO / Bitcoin) — `id`, `transaction_id` (FK), `address_id` (FK→address, nullable for non-standard scripts), `amount` (text base units), `output_index` (int), `spent` (bool), `spending_tx_id` (FK→transaction, nullable), `source_query_id` (FK). **Unique: `(transaction_id, output_index)`** (decision #9). Bitcoin value resides here; it is the valuation subject for Bitcoin.

### Family B — sourced claims (append-only; many per subject; each → `source_query`)

**`attribution`** — `id`, `address_id` (FK), `label`, `category` (nullable), `source` (e.g. `arkham`, `misttrack`, `breadcrumbs`, `investigator`), `confidence` (real, nullable), `note` (nullable), `retrieved_at`, `source_query_id` (FK, nullable for investigator-authored).

**`risk_assessment`** — `id`, `address_id` (FK), `score` (real, nullable), `score_scale` (text, e.g. `0-100`), `category` (nullable), `rationale` (nullable), `source`, `retrieved_at`, `source_query_id` (FK). Stored raw per source; no synthetic combination.

**`valuation`** — `id`, `subject_type` (enum: `transfer` | `tx_output`), `subject_id` (UUID; the valued movement), `currency` (default `USD`), `unit_price` (text), `value` (text; per §B precision rule), `price_timestamp`, `confidence` (real; from source), `source` (default `defillama`), `retrieved_at`, `source_query_id` (FK). *Bitcoin input-side value is derived by joining `tx_input.prev_output_id` → that output's valuation; coverage therefore depends on the funding tx being in-DB.*

**`balance_snapshot`** — `id`, `address_id` (FK), `asset_id` (FK, nullable; null = native/aggregate), `amount` (text), `as_of_ts`, `source`, `retrieved_at`, `source_query_id` (FK). Point-in-time, sourced.

**`entity_membership`** — `id`, `entity_id` (FK→entity), `address_id` (FK→address), `source` (e.g. `arkham`, `cospend-heuristic`, `same-address-heuristic`, `investigator`), `method` (e.g. `shared-label`, `co-spend`, `same-address-heuristic`, `manual`), `confidence` (real, nullable), `flags` (nullable, e.g. `possible-coinjoin`), `created_at`, `source_query_id` (FK, nullable for investigator). **Append-only; may contradict.** Not mutated by merges (resolution chases `entity.merged_into`).

**`entity_membership_retraction`** **(new, decision #3)** — `id`, `membership_id` (FK→entity_membership), `reason` (text; e.g. `missed-coinjoin`), `source` (e.g. `investigator`), `method` (nullable), `created_at`, `source_query_id` (FK, nullable for investigator). Append-only retraction of a membership; the original membership row is preserved (never deleted), and resolution/compute treats a retracted membership as inactive. Keeps the never-collapse / append-only discipline intact while letting a false co-spend membership be withdrawn.

### Family C — investigator-constructed objects

**`entity`** — `id`, `name` (nullable; co-spend clusters auto-create anonymous entities), `entity_type` (nullable), **`origin`** (enum: `cospend-cluster` | `source` | `investigator`), **`merged_into`** (FK→entity, nullable; tombstone — set on the absorbed entity at merge; resolution follows this pointer to the surviving entity), **`canonical_membership_id`** (FK→entity_membership, nullable; investigator-chosen membership for display, decision #10), `created_at`.
- **Merge (decision #3):** set `merged_into` on the absorbed entity to the surviving entity's id. Memberships are **not** rewritten — entity resolution chases the `merged_into` chain to the canonical survivor. Reversible by clearing the pointer.
- **Split:** create a new entity; move addresses by adding `entity_membership_retraction` rows on the old entity and new `entity_membership` rows on the new entity.
- **Display (decision #10):** show `canonical_membership_id` when set; when null and memberships conflict, render explicitly as **"contested"** (all conflicting active claims shown side-by-side), never auto-collapsed.

**`trace`** — `id`, `name`, `description` (nullable), `created_at`.

**`trace_transfer`** (EVM edges in a trace) — `id`, `trace_id` (FK), `transfer_id` (FK→transfer), `ordering` (int, nullable), `note` (nullable).

**`trace_btc_link`** (Bitcoin trace-time input→output linkages) — `id`, `trace_id` (FK), `transaction_id` (FK), `source_output_id` (FK→tx_output; the output being spent), `dest_output_id` (FK→tx_output; an output of the same transaction), `basis` (enum: `fifo` | `investigator`; extensible to other conventions), `confidence` (real, nullable), `ordering` (int, nullable), `note` (nullable). *FIFO is applied as a taint-apportionment rule along paths the investigator has already expanded — not automated path discovery (deferred).*

**`finding`** — `id`, `statement`, `assessment` (nullable), `created_at`.

**`finding_ref`** — `id`, `finding_id` (FK), `ref_type` (enum: `address` | `transfer` | `transaction` | `tx_output` | `trace` | `exhibit` | `entity`), `ref_id` (UUID), `note` (nullable).

**`annotation`** — `id`, `target_type` (enum: `address` | `transfer` | `transaction` | `tx_output` | `trace` | `entity` | `finding`), `target_id` (UUID), `content`, `created_at`.

**`tag`** — `id`, `target_type` (enum: `address` | `entity`), `target_id` (UUID), `label`, `created_at`. Investigator's own; distinct from `attribution`.

**`report`** — `id`, `title`, `generated_at`, `scope_spec` (JSON; which traces/findings/addresses/entities were included **and the expansion bounds applied** so the report never implies completeness, decision #2), `rendered_file_ref` (path in `reports/`), `content_hash`, **`supersedes_report_id`** (FK→report, nullable; a later report supersedes rather than edits, decision-clean handling of the immutability-vs-corrections risk). Immutable once written.

### Family D — provenance and evidence

**`source_query`** — `id`, `connector` (text), `capability` (e.g. `get_transactions`), `endpoint` (text), `params` (JSON; **includes any expansion bounds applied**, decision #2), `requested_at`, `completed_at`, `status`, `raw_response_ref` (path in `raw_responses/`), **`raw_response_hash`** (SHA-256 of the raw response, decision #5), `result_summary` (nullable). The provenance spine; copied into a case alongside any cached claim it produced.

**`exhibit`** — `id`, `exhibit_type` (enum: `screenshot` | `file` | `export`), `source` (e.g. `arkham-ui`), `captured_at`, `file_ref` (path in `exhibits/`), `content_hash`, `description` (nullable). Investigator-attached artifacts only.

### Read-model views (new, decision #4)

Truthful-asymmetric base tables stay the source of truth; consumers that don't care about chain paradigm read these views instead of branching.

- **`v_value_movement`** — one row per value movement, unified shape `(paradigm, movement_id, movement_kind, transaction_id, chain, src_address_id, dst_address_id, asset_id, amount, position)`:
  - EVM: `SELECT 'evm', tr.id, 'transfer', tr.transaction_id, tr.chain, tr.from_address_id, tr.to_address_id, tr.asset_id, tr.amount, tr.position FROM transfer tr`.
  - Bitcoin: `SELECT 'utxo', o.id, 'tx_output', o.transaction_id, <chain>, NULL, o.address_id, <btc_asset_id>, o.amount, o.output_index FROM tx_output o`. **`src_address_id` is deliberately NULL** for UTXO — which input funded this output is not a ledger fact (it is a trace-time claim in `trace_btc_link`). The view therefore never fabricates an input→output edge.
- **`v_address_flow`** (optional helper) — value in/out per address with `valuation` joined for USD, built on `v_value_movement`.

Views expose `finality_status` (joined from `transaction`) so consumers can filter provisional tip data.

---

## E. Capability interface (revised §11)

Address-scoped capabilities take a **`bounds`** selector (decision #2); the orchestrator records the applied bounds in `source_query.params`.

`bounds` ::= `{ block_range?: [from,to], time_window?: [from,to], min_value?: text, top_n_counterparties?: int, max_pages?: int, direction?: in|out|both }` — all optional; absent = connector default (which itself is recorded).

- `get_transactions(chain, address, bounds) -> [Transaction]` — with child records: `transfer[]` (account) or `tx_input[]` + `tx_output[]` (UTXO). Connector maps `bounds` onto its native paging (Etherscan block range/page; Esplora `last_seen_txid` cursor).
- `get_transfers(chain, tx_hash) -> ValueDetail` — `transfer[]` (account) or `(tx_input[], tx_output[])` (UTXO).
- `get_balance(chain, address) -> [BalanceSnapshot]`.
- `get_attributions(chain, address) -> [Attribution]`.
- `get_risk(chain, address) -> [RiskAssessment]`.
- Enrichment (separate interface): `get_price(chain, asset, timestamp) -> Valuation`.

Every capability call writes a `source_query` row (with `raw_response_hash`) plus the resulting fact/claim rows; provisional facts are upserted on their natural keys and may be corrected on a later fetch until final.

---

## F. Connectors and data sources (revised §12)

Unchanged from the original table except: each address-scoped connector declares how it honours `bounds`, and the shared cache is SQLite (no parquet). Etherscan V2 (EVM, API), Bitquery (EVM, API, supplemental), Blockstream Esplora (Bitcoin, API/self-hostable), DeFiLlama (pricing, API), Arkham (attribution, **import** v1 / API later), MisTrack (risk+attribution, **import** v1 / API later). Import connectors use bespoke per-tool parsers with a screenshot-as-exhibit fallback; bones built so a future API connector is a drop-in.

---

## G. Build phases (revised §13)

Ordered to de-risk the hardest bets early; deltas from the original noted.

1. **Data model + storage foundation** — all tables and views; `source_query` spine **with `raw_response_hash`** from the first table; case-folder layout incl. `manifest.json` export hook; SQLite shared cache with copy-into-case semantics; **finality convention and per-chain thresholds wired in**; address canonicalization and natural unique keys in place.
2. **EVM connector end-to-end** (Etherscan V2) — `get_transactions`/`get_transfers`/`get_balance` **with `bounds`**, normalize (merge native+internal+token), upsert with provenance and finality, retrieve.
3. **Bitcoin connector** (Esplora) — `tx_input`/`tx_output`, transaction-as-node, finality via confirmations; validates the UTXO model and the unification.
4. **Graph surface** (Cytoscape + React) reading the **`v_value_movement` view** — heterogeneous graph (address nodes; Bitcoin transaction-nodes; EVM transfer-edges); provisional facts visibly flagged.
5. **Valuation** (DeFiLlama) — value-at-time on transfers and Bitcoin outputs, with confidence and missing-value honesty.
6. **Entity resolution** — `entity` + `entity_membership` + `entity_membership_retraction`; Bitcoin co-spend clustering at ingest (auto anonymous cluster-entities, CoinJoin flagging); **merge/split with `merged_into` resolution**; source-label and same-address memberships; curated-canonical/contested display.
7. **Import parsers + risk/attribution display** — Arkham and MisTrack bespoke parsers; raw multi-source risk/attribution side-by-side.
8. **Investigator layer** — named traces (incl. Bitcoin FIFO input→output linkages with manual override), findings, annotations, tags.
9. **Reporting** (Playwright) — immutable report snapshots that render the live graph; `scope_spec` records applied bounds; `supersedes_report_id` for corrections.
10. **Case-folder export** — hash manifest + portable `.casefile` bundle.

---

## H. Residual notes for the author

- **CoinJoin detection stays best-effort.** The `flags=possible-coinjoin` marker and `entity_membership_retraction` together handle false clusters, but detection will miss some patterns (PayJoin especially); the merge/split + retraction machinery is the safety net.
- **Finality threshold is a policy knob.** Per-chain thresholds live in app config; document the chosen values, since they define when a fact becomes immutable evidence.
- **`v_value_movement` is read-only by design.** If a consumer needs input→output routing on Bitcoin, it must read `trace_btc_link` (a claim), never the view — preserving the "schema tells the truth" invariant.
