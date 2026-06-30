# Connectors — capability interface, contracts & mapping

How data enters the model. Connectors return **canonical records**; normalization adapters do the
mapping; nothing downstream knows a source's native shape. Every call writes a `source_query` row (with
`raw_response_hash`) in the same transaction as the rows it produces.

> **CONFIRM-AT-BUILD:** the endpoint paths and base URLs below are stable, but exact field names, rate
> limits, free-tier chain coverage, and pagination details are **volatile** — confirm against the cited
> live docs before implementing each adapter, and log the check in `PROGRESS.md`. Record one real
> response per capability as a cassette in `tests/cassettes/` (it doubles as a provenance fixture).

## 1. Capability interface

Each connector implements a subset; the orchestrator dispatches on capability. Address-scoped
capabilities take a **`bounds`** selector (decision #2), recorded in `source_query.params`.

```python
# backend/app/connectors/base.py  (sketch)
Bounds = TypedDict("Bounds", {
    "block_range": tuple[int, int] | None,
    "time_window": tuple[str, str] | None,     # ISO-8601
    "min_value": str | None,                    # base-unit integer as text
    "top_n_counterparties": int | None,
    "max_pages": int | None,
    "direction": Literal["in", "out", "both"] | None,
}, total=False)

class Connector(Protocol):
    name: str
    def capabilities(self) -> set[str]: ...
    # address-scoped (take bounds):
    def get_transactions(self, chain: str, address: str, bounds: Bounds) -> list[Transaction]: ...
    def get_balance(self, chain: str, address: str) -> list[BalanceSnapshot]: ...
    def get_attributions(self, chain: str, address: str) -> list[Attribution]: ...
    def get_risk(self, chain: str, address: str) -> list[RiskAssessment]: ...
    # tx-scoped:
    def get_transfers(self, chain: str, tx_hash: str) -> ValueDetail: ...
    # enrichment (separate interface, keyed by asset+timestamp):
    def get_price(self, chain: str, asset: Asset, timestamp: int) -> Valuation: ...
```

`get_transactions` returns `Transaction` objects carrying their child records: `transfer[]` for account
chains; `(tx_input[], tx_output[])` for UTXO chains. `bounds` absent ⇒ connector default, but the default
is still written to `params` as `"bounds":"default"` so partiality is reproducible (audit #10).

**Base-class responsibilities:** per-connector rate-limit token bucket + exponential backoff with jitter
on 429/5xx; honor `max_pages`; write the `source_query` row (status `ok|error|partial`) and the raw
response file + hash atomically with the produced rows; surface `partial` when bounds truncate results.

## 2. Etherscan V2 (EVM) — API

- **Base:** `https://api.etherscan.io/v2/api` · single key, ~50 EVM chains via `chainid` (e.g. 1
  Ethereum, 8453 Base, 42161 Arbitrum). *Confirm current free-tier chain coverage; some chains need a
  paid Lite plan.* Rate limit ~5 req/s, 100k/day (confirm).
- **Provides:** `get_transactions`, `get_transfers`, `get_balance`.
- **Response envelope:** `{ "status": "1|0", "message": ..., "result": [...] }`. `status:"0"` with an
  empty result is "no records," not an error — handle distinctly from rate-limit/error messages.

A full EVM value picture for an address **merges three endpoints**, each paginated and each its own
`source_query` (Invariant: provenance per call):

| Native | `module=account&action=txlist&address=&startblock=&endblock=&page=&offset=&sort=asc` | → `transaction_` + `transfer(transfer_type='native')` |
| Internal | `module=account&action=txlistinternal&address=...` | → `transfer(transfer_type='internal')` (parent tx already present) |
| ERC-20 | `module=account&action=tokentx&address=...[&contractaddress=]` | → `asset` + `transfer(transfer_type='erc20')` |
| Balance | `module=account&action=balance&address=&tag=latest` | → `balance_snapshot` |

**Field → canonical mapping (confirm names):**

- txlist row: `hash`→`transaction_.tx_hash`; `blockNumber`→`block_height`; `timeStamp`(unix)→`block_ts`
  (convert to ISO-8601); `gasUsed`×`gasPrice`→`fee`; `isError`/`txreceipt_status`→`status`;
  `from`/`to`→`address` (canonicalize, lowercase); `value`(wei)→`transfer.amount`; `confirmations`→
  `transaction_.confirmations` (then compute `finality_status`). Native asset = the chain's native coin
  (contract NULL). `position` for native = 0 (one native value move per tx) or the tx index convention —
  define and keep consistent.
- txlistinternal row: same envelope; `position` = internal-call index (`traceId`/sequence). These have
  **no token**; asset = native.
- tokentx row: `contractAddress`,`tokenSymbol`,`tokenDecimal`→`asset`; `value`(raw)→`transfer.amount`;
  `position` = log index.

**Bounds mapping:** `block_range`→`startblock/endblock`; `max_pages`→stop after N pages of `offset`-sized
pages; `time_window`→resolve to block range first (or filter post-hoc and mark `partial`); `min_value`,
`top_n_counterparties`,`direction`→post-filter and record in `params`.

## 3. Bitquery (EVM) — API (supplemental)

GraphQL; optional alternative/supplement to Etherscan, useful for token-transfer queries. Same
capabilities and same canonical output as Etherscan (drop-in). Implement only if Etherscan coverage is
insufficient for a chain; keep behind the same interface. *Confirm current free-tier limits.*

## 4. Blockstream Esplora (Bitcoin / UTXO) — API

- **Base:** `https://blockstream.info/api` (mainnet); self-hostable to remove rate limits. mempool.space
  API as fallback (compatible shape — confirm).
- **Provides:** `get_transactions`, `get_transfers`, `get_balance`.
- **Endpoints:**
  - `GET /address/:addr/txs` — confirmed+mempool, newest first, **25 per page**.
  - `GET /address/:addr/txs/chain/:last_seen_txid` — next page (cursor = last txid seen). Honor
    `max_pages`.
  - `GET /address/:addr` — `chain_stats`/`mempool_stats` with `funded_txo_sum`, `spent_txo_sum`,
    `tx_count`. **Balance = funded_txo_sum − spent_txo_sum** → `balance_snapshot`.
  - `GET /tx/:txid` — `txid, version, locktime, size, weight, fee, vin[], vout[], status`.
  - `GET /blocks/tip/height` — current tip height, for `confirmations = tip − block_height + 1`.
- **`/tx` → canonical mapping:**
  - tx → `transaction_` (`txid`→`tx_hash`; `status.block_height`→`block_height`;
    `status.block_time`(unix)→`block_ts`; `fee`→`fee`; compute `confirmations`/`finality_status`).
  - each `vin` → `tx_input` (`vin.txid`+`vin.vout` identify `prev_output`; `prevout.scriptpubkey_address`
    →`address` (canonicalize per encoding); `prevout.value`(sat)→`amount`; index→`input_index`).
    `prev_output_id` resolves only if that output is in-DB.
  - each `vout` → `tx_output` (`scriptpubkey_address`→`address` (NULL for non-standard);
    `value`(sat)→`amount`; index→`output_index`; `spent` updated when the spending tx is seen).
  - **Never** synthesize a vin→vout transfer (Invariant #5).

## 5. DeFiLlama (pricing) — API (enrichment)

- **Base:** `https://coins.llama.fi` · no key for core endpoints (rate-limited — confirm).
- **Endpoint:** `GET /prices/historical/{unix_timestamp}/{coins}` where `coins` is a comma list of
  `chain:address` keys (e.g. `ethereum:0xA0b8...`). **Native-coin keys need confirmation** (e.g.
  `coingecko:bitcoin`, `coingecko:ethereum`); resolve and document per chain.
- **Response:** `{ "coins": { "<key>": { "decimals", "symbol", "price", "timestamp", "confidence" } } }`.
  `confidence` is 0–1.
- **Mapping:** value each movement at **its block timestamp** → `valuation`
  (`unit_price`=price; `value`=Decimal(price)×amount/10^decimals, half-even 18 sig; `confidence`;
  `price_timestamp`=returned ts; `source='defillama'`). Coverage can be sparse/lagged for long-tail or
  very old tokens — `valuation` may be missing or low-confidence; reports represent this honestly.

## 6. Source connectors (free + optional paid) — imports, APIs, and the paid integration layer

Bespoke per-tool parsers from day one (the manual path is expected to be used for a while); a
**screenshot-as-exhibit** fallback covers visually-only data. Build the connector "bones" so swapping an
import connector for the same tool's future API connector is a drop-in (same capabilities, same canonical
output).

| Arkham | `get_transactions` (transfer log) | parse exported transfer CSV → `transaction_` + `transfer` (+ `asset`/`address`), via the canonical adapter path. The logged-in UI export is a **transfer log** (one row = one A→B movement, 19 cols), NOT attributions — see `docs/findings/arkham_export_reconciliation.md`. |
| MisTrack API (paid) | `get_risk`, `get_attributions` | **API** (`openapi.misttrack.io`) — the CSV importer was retired (`docs/findings/misttrack_reconciliation.md`). `/v2/risk_score` (score **3-100**, nested `risk_detail[]` kept raw) + `/v1/address_labels` → `risk_assessment`/`attribution` (`source='misttrack'`). Keyring `misttrack_api_key`. |
| GraphSense TagPacks | `get_attributions`, `get_risk`, `get_entities` | parse free/open **YAML** TagPacks (header→tag inheritance + `header: !include`) → `attribution` (`source='graphsense'`); `abuse` → categorical `risk_assessment` (score=None); ActorPacks + tag `actor` refs → `entity` + `entity_membership`. The **free attribution pillar** — see `docs/findings/graphsense_tagpack_reconciliation.md`. |
| OFAC SDN | `get_risk`, `get_attributions` | parse the official OFAC SDN **XML** (free, no key) → categorical `risk_assessment(category='sanctioned', score=None, source='ofac-sdn')` per sanctioned BTC/EVM address, `rationale`="OFAC SDN: \<entity\> (\<program\>)"; optional `attribution(category='sanctioned_entity')`. The **free risk pillar** — see `docs/findings/ofac_sanctions_reconciliation.md`. |
| Chainalysis sanctions API | `get_risk` (HTTP, free key) | `GET /api/v1/address/{addr}` (`X-API-Key`) → `identifications[]` → `risk_assessment(source='chainalysis-sanctions')`, a second sanctions source stored **side-by-side** with OFAC (Invariant #4). Key via keyring; `TODO: confirm` field names live. |
| Bitquery (paid) | `get_transactions`, `get_transfers` | **GraphQL** multi-chain EVM **facts** fallback. V2 `streaming.bitquery.io/graphql`, OAuth2 **Bearer** (V1 + `X-API-KEY` configurable). Routed through the canonical `transfer`/`transaction_` path. Keyring `bitquery_token`. **Query body + field paths `TODO: confirm`** (built, validated by RUN_LIVE drift — no fabricated cassette). |
| Arkham API (paid; Path B) | `get_attributions`, `get_risk` | **CONFIRMED.** `api.arkm.com`, `API-Key` header. `GET /intelligence/address/{addr}` → `arkhamEntity`→`entity`+`entity_membership`, `arkhamLabel`→`attribution`, **`predictedEntity`→ a SEPARATE lower-confidence entity/membership/attribution (never collapsed into confirmed, Inv #4)**, `depositServiceID`→service attribution (`userEntity`/`userLabel` ignored). `GET /risk/address/{addr}` → `RiskAssessment(score=max_score, scale='0-100', category=greatest_risk_category)` with the per-category breakdown kept raw. `source='arkham-api'`. Keyring `arkham_api_key`. |
| OKLink (paid; **shell**) | `get_attributions`, `get_risk` | **Partial.** Confirmed conventions wired (base `oklink.com/api/v5/explorer/`, `chainShortName`, `{code,msg,data}` envelope); the AML endpoint paths/fields are `TODO: confirm`, so the capabilities raise an honest "not wired" error rather than guess. Keyring `oklink_api_key`. |

**Arkham reconciliation (2026-06-28):** the original assumed-attribution schema (`address, chain, entity,
category, label, confidence`) is **not** what Arkham emits. The UI "download" gives a transfer log, so the
Arkham importer was re-scoped from `get_attributions` to **`get_transactions`** (transfer ingest through
the canonical `Transfer` path, mirroring `etherscan_adapter`). Open decisions resolved in
`normalization/arkham_adapter.py` (with `TODO: confirm` where the export lacks data): `unitValue` is a
**display** value → `amount = round(Decimal(unitValue) × 10^decimals)`; `type` is **direction**
(`inflow`/`outflow`), **not** the enum, so `transfer_type` is derived from `tokenAddress` presence;
`tokenId` is a **coin slug** (e.g. `tether`), not an erc721 id, and is dropped; `position` = row-order
index within `(tx, transfer_type)` (no log index → idempotent re-ingest, but may not align with Etherscan's
positions); finality `provisional` (no confirmations column, upgraded on a chain re-fetch).
**Chain classification (three classes; 2026-06-28 multichain update):** chains are alias-normalized
(`ARKHAM_CHAIN_ALIASES`) to canonical names, then classified — **EVM** (`ethereum`, **`bsc`**, `base`,
`arbitrum`, `optimism`, `polygon`) → ingested as transfers; **UTXO** (`bitcoin`) → **hard refuse, all-or-
nothing** (a `from→to`, sometimes a comma-joined UTXO *input set*, would fabricate an input→output edge —
Invariant #5; ingest Bitcoin via Esplora); **unsupported account-model** (e.g. `tron`, base58 addresses) →
**skipped and reported** (`unsupported_skipped`/`unsupported_chains`), NOT a fabrication risk, so the
supported EVM rows in the same multichain export still ingest. (`bsc` was added to `NATIVE_SYMBOL`/
`CHAINID_TO_NAME`/finality thresholds — it had been wrongly rejected.) Address→entity/label/**attribution**
is **not in any UI export** — it needs Arkham's official **API** (Path B, Invariant #1); `from/toLabel` are
too thin to synthesize from.

**GraphSense TagPacks (2026-06-28) — the free attribution pillar.** GraphSense TagPacks are free, open
(MIT), public-source attribution tags in YAML, designed for *provenance-aware* sharing — they map almost
one-to-one onto BIH's `attribution` + `source_query` spine (Invariants #1/#3/#4) and fill the
`attribution`/`entity_membership` capability that had **no correct producer** after the Arkham re-scope.
The connector ingests a YAML TagPack from a local clone of the public repo
(`github.com/graphsense/graphsense-tagpacks`, `packs/`) — a structured import of public data (Invariant #1,
no scraping). Mapping (`normalization/graphsense_adapter.py`; full table in the findings note):
**`address`+`currency`** → `Address` via a currency→chain map (BTC→bitcoin, ETH→ethereum); **`label`** →
`Attribution.label`; **`category`** → `Attribution.category` (raw taxonomy concept); **`confidence`** is a
categorical *id* (e.g. `forensic_investigation`), not a float, looked up in the vendored
`confidence.csv` to `level/100` (unknown/missing id → `confidence=None`, never a guessed level); the
**`source`** backlink + the confidence id go into `Attribution.note` (also the per-tag idempotency
discriminator). **Chain filter:** BIH v1 is BTC + EVM only, so unsupported currencies (BCH/LTC/ZEC/XRP/…)
are **skipped + reported**, never canonicalized (mirrors the Arkham `tron` skip); a malformed address on a
*supported* chain is a hard error (all-or-nothing). **Idempotency:** `upsert_attribution` keys on
`(address, label, source, note)` (Invariant #7) — the same `(address,label)` from different TagPacks keeps
side-by-side rows, never merged (Invariant #4). **Phase B:** a tag's `abuse` value also writes a
*categorical* `risk_assessment` (`score=None` — no numeric score is invented). **Phase C:** ActorPacks
(`actors:`) become `entity` rows (origin='source', idempotent on the actor id via `entity.external_id`,
migration `0006`); a tag's `actor` ref → `entity_membership` (method=`tagpack-actor`, the tag's confidence,
`is_cluster_definer` → `flags='cluster-definer'`), resolved by actor id so ActorPack/TagPack ingest order
does not matter. YAML is parsed with a hardened `SafeLoader` (an unquoted `0x…` EVM address resolves as a
*string*, not a YAML hex int; only `!include` is allowed as a custom tag — no code execution).

**OFAC + Chainalysis — the free risk pillar (2026-06-28).** Sanctions screening is free and
authoritative; both sources write **categorical** `risk_assessment` (`score=None`, `score_scale=None` —
never a numeric score). OFAC uses the fixed `category='sanctioned'`; **Chainalysis preserves the API's
own `category`** (e.g. `'sanctions'`/`'pep'`, falling back to `'sanctioned'` if absent) rather than
flattening it — Invariant #4 stores each source's classification raw (the two are side-by-side anyway).
**OFAC SDN** (`connectors/imports/ofac.py`,
`source='ofac-sdn'`) is the primary, key-less source: a local copy of the official SDN XML is parsed by
the pure `normalization/ofac_adapter.py`, which reads crypto addresses from `"Digital Currency Address -
<TICKER>"` ids and maps **`<TICKER>`→chain** (XBT→bitcoin, ETH→ethereum, ARB→arbitrum, BSC→bsc, ERC-20
USDC/USDT→ethereum) — unsupported tickers (XMR/LTC/ZEC/…) are **skipped + reported**, never canonicalized
(mirrors the Arkham tron / GraphSense unsupported-currency skip); the `rationale` carries the SDN entity +
program(s). **XML-format note (`TODO: confirm`):** implemented against the standard `sdn.xml` (a stable,
faithfully-modellable structure), not the findings' `sdn_advanced.xml` whose entity/program data sits
behind reference-value indirection that can't be confirmed offline (§6 — confirm over guess); an
advanced-format adapter and the 0xB10C addresses-only lists are documented follow-ups. **Sanctions are
mutable:** OFAC delists addresses, so each fetch is a dated observation (the SDN publication date is the
`source_query.endpoint`); a delisted address is **reported** (`delisted`), absent from the new fetch, but
its prior claim is **retained** (append-only — "X was sanctioned as of \<date\>" stays true and
provenance-backed; deleting a claim would break the append-only invariant + audit). The XML is parsed
defensively (a `<!DOCTYPE>` is rejected to foreclose entity-expansion). Phase B optionally adds
`attribution(category='sanctioned_entity')` when the entry names the entity. **Chainalysis**
(`connectors/chainalysis.py`, `source='chainalysis-sanctions'`, free key via keyring) is a second
per-address sanctions screener stored **side-by-side** with OFAC (Invariant #4 — two sanctions sources may
differ, both shown); a negative screen still records a `source_query` ("checked, clean as of \<date\>").

Imported/API **claims** (GraphSense attribution/risk/membership, OFAC sanctions-risk/attribution,
Chainalysis sanctions-risk, MisTrack/Arkham-API risk/attribution) are **append-only and stored raw per
source** — never merged with another source's claim. Imported **facts** (Arkham transfers, Bitquery
transfers) go through the same idempotent natural-key upsert as any other fact (Invariant #7). Either way
the connector writes a `source_query` (`raw_response_ref` = the stored file/response, hashed) so
provenance holds.

**The optional PAID integration layer (2026-06-28; `docs/findings/paid_api_integrations.md`).** Four
optional paid sources, all **disabled by default** and gated by `connectors/registry.py`: a source is
selectable only when its `BIH_<name>_ENABLED` config flag is on **AND** its key is in the keyring;
otherwise it is silently absent and never blocks the free baseline (Etherscan/Esplora/DeFiLlama/GraphSense/
OFAC) — Invariant #4. They follow the Chainalysis template (config flag + keyring key + `_has_key` + a
clear `ConnectorError` naming the keyring entry if called unkeyed + a `source_query` even on empty
results + defensive `.get`/non-dict guards). **Bitquery** (GraphSense the only paid *fact* source) is
wired into the orchestrator after the free connectors (fallback only). **Arkham API** is fully confirmed;
**MisTrack** is confirmed from its OpenAPI; **Bitquery**'s query body and **OKLink**'s AML endpoints
remain `TODO: confirm`. There are **no fabricated cassettes** — wire shapes are validated by key-gated
`RUN_LIVE` drift tests; the confirmed mappings (Arkham/MisTrack) additionally have pure-mapper logic tests
(synthetic input) that guard the Invariant #4 never-collapse rule (Arkham confirmed-vs-predicted) and the
raw breakdown / 3-100 score scale. `/health` reports each paid source's `enabled`/`has_key`/`available`.

### 6a. Configuring paid sources (operator)

To enable an optional paid source: set its `BIH_<name>_ENABLED=1` (env / `.env`) **and** store its key in
the OS keyring (`python -c "from backend.app.secrets import set_secret; set_secret('<entry>', '<key>')"`).
Keys live ONLY in the keyring (never config, never logged); the loud `BIH_ALLOW_PLAINTEXT_KEYS=1` +
`BIH_SECRET_<NAME>` env path is a dev-only exception.

| Source | Enable flag | Keyring entry | Notes |
|---|---|---|---|
| Bitquery | `BIH_BITQUERY_ENABLED` | `bitquery_token` | V2 OAuth2 Bearer (set `BIH_BITQUERY_USE_V1=1` for V1 + X-API-KEY) |
| Arkham API | `BIH_ARKHAM_API_ENABLED` | `arkham_api_key` | `API-Key` header; created in Arkham UI → Settings → API Keys |
| MisTrack | `BIH_MISTRACK_ENABLED` | `misttrack_api_key` | sent as the `api_key` query param (kept out of recorded provenance) |
| OKLink | `BIH_OKLINK_ENABLED` | `oklink_api_key` | shell — AML endpoints `TODO: confirm`; capabilities raise "not wired" until filled |

## 7. Connector test obligations (see testing.md)

Each connector ships with: a **contract test** replaying a recorded cassette → expected canonical rows; a
**bounds test** asserting `params` records the applied bounds; and participation in the relevant **golden
smoketest**. Add a `test_live_drift` entry (opt-in `RUN_LIVE=1`) that re-hits the real endpoint and fails
if the response shape diverges from the cassette.
