# Findings — optional paid-API integration scaffolding

**Date:** 2026-06-28
**Goal (user directive):** wire **Bitquery** (user has a token) and build the **options/integrations for
the optional paid sources now** so users with keys can use them — **we can't test them without keys, so
build + scaffold now, confirm/test later**. All optional, disabled by default, side-by-side, never
blocking the free baseline (Invariant #4). See [[project-bih-sourcing-architecture]] / `project-bih-backlog-goals`.

## The optional-paid pattern is already established (mirror it)

`connectors/chainalysis.py` + `config.py` define the template — replicate it per connector:
- **Config** (`config.py`, env prefix `BIH_`): `<name>_enabled: bool = False` (paid → default off),
  `<name>_base_url: str`, optional `<name>_paid_tier`.
- **Secret**: key in the OS keyring via `secrets.py::get_secret("<name>_api_key")` — never in config,
  never logged. Constructor sets `self._has_key`; if a capability is called without a key, raise a clear
  `ConnectorError` telling the user which keyring entry to set.
- **Provenance**: write a `source_query` per call even on an empty/negative result (Invariant #3).
- **Defensive + honest**: read fields with `.get`, guard non-dict bodies, mark every unconfirmed live
  field `TODO: confirm`. Tests are **key-gated / live-drift-skipped** (no cassette can be recorded
  without a key) — exactly like the 8 skipped Chainalysis tests.
- **Registration**: register in the connector registry/orchestrator so the source is *selectable when its
  key is present* and silently absent otherwise.

## Per-connector scaffolding spec

### 1. Bitquery (user has a token) — multi-chain GraphQL
- **Endpoint:** V2 `https://streaming.bitquery.io/graphql` (V1 `https://graphql.bitquery.io`). Confirmed
  live: GraphQL over HTTP, `EVM(dataset: archive, network: <net>){ Transactions/Transfers … }`; 40+
  chains incl eth, bsc, base, arbitrum, optimism, matic, tron.
- **Auth — CONFIRMED:** V2 uses an **OAuth2 Bearer access token** (`Authorization: Bearer <token>`) —
  the user's "token" is exactly this (generated from a Bitquery Application; programmatic refresh via
  `client_credentials` + `scope=api` if needed). Default to V2 Bearer. (V1 `graphql.bitquery.io` +
  `X-API-KEY` left as a configurable fallback.) Keyring: `bitquery_token`.
- **Capability:** `get_transactions` / `get_transfers` — a multi-chain EVM **facts** source, useful as a
  fallback when Etherscan's free chain coverage shrinks. Route output through the canonical
  `transfer`/`transaction_` path (reuse the etherscan/arkham adapter shape). The GraphQL query body +
  response→canonical mapping are **TODO: confirm** (build the query, don't fetch per-test).
- **Later capability (not now):** coinpath / money-flow for the tracing layer (V1 `coinpath` money-flow).

### 2. MisTrack API (paid) — fully specced already
- Spec: `docs/findings/misttrack_reconciliation.md` (re-scope the existing CSV parser to an API
  connector). Capabilities `get_risk` (`/v2` or **V3** `risk_score`) + `get_attributions`
  (`/v1/address_labels`). Reminders: `coin` not `chain`; `score` **3–100**; data split across two
  endpoints; nested `risk_detail[]`. Keyring: `misttrack_api_key`. Base: `https://openapi.misttrack.io`.

### 3. Arkham API (paid; 30-day trial then ~$25/mo) — Path B attribution + risk — **CONFIRMED 2026-06-28**
- **Base:** `https://api.arkm.com`. **Auth:** `API-Key: <key>` header (key created in the Arkham UI
  Settings → API Keys). Keyring: `arkham_api_key`. (Source: `arkm.com/llms.txt` + endpoint/schema docs.)
- **`get_attributions`** → `GET /intelligence/address/{address}?chain=ethereum&chain=bsc…` (chain is an
  optional array; auto-detects if omitted). Returns the **Address** schema:
  - `arkhamEntity` (Entity) → confirmed entity → `Entity(origin='source')` + `EntityMembership`
  - `arkhamLabel` (Label) → `Attribution.label` (`source='arkham-api'`)
  - `predictedEntity` (Entity) → **probabilistic** entity → separate attribution/membership at **lower
    confidence** (Arkham is "entity-first, confidence-scored" — never collapse predicted into confirmed;
    Inv #4)
  - `depositServiceID` (e.g. `"binance"`) → deposit-address→service attribution
  - `contract`/`service`/`isUserAddress`/`program` (bools) → metadata; `userEntity`/`userLabel` are the
    *caller's own private* labels → ignore for attribution.
  - Batch: `POST /intelligence/address/batch` (+ `/all` for across-chains).
  - **Entity** schema: `id`, `name` (e.g. "Binance"), `type` (e.g. `cex`), `service`, socials/website →
    `Entity(name=name, entity_type=type, origin='source')`. **Label** schema: `name` (e.g. "Cold Wallet")
    → `Attribution.label`.
- **`get_risk` — CONFIRMED** → `GET /risk/address/{address}` → `RiskScoreResponse`:
  `max_score` (0–100, the headline numeric score) + `risk_level` (`NONE/LOW/MEDIUM/HIGH/SEVERE`) +
  per-category scores (`hacker_score`, `mixer_score`, `sanctions_score`, `ransomware_score`,
  `scam_score`, `ponzi_score`, `darkweb_score`, `gambling_score`, `privacy_score`,
  `non_kyc_service_score`, `mixed_kyc_service_score`, `token_blacklist_score`, `sanctioned_1hop_score`),
  `greatest_risk_category`, `hop_distance`, `is_seed`, `risk_weighted_incoming/outgoing_usd`,
  `top_sources[]`, `updated_at`. Map → `RiskAssessment(score=max_score, score_scale='0-100',
  category=greatest_risk_category, rationale=risk_level + the category breakdown, source='arkham-api')`.
  Store the category breakdown **raw** (don't collapse — Inv #4).
- Note: Arkham API thus covers **both** the paid attribution (Path B) and a paid numeric risk score.

### 4. OKLink (paid AML) — labels + risk — **partially confirmed; AML endpoints still TODO**
- Capabilities `get_attributions` (entity/address labels — "3.4B+ labels") + `get_risk` (KYT/KYA).
- **Confirmed from a manual scrape (2026-06-28)** of the OKLink "Developer tools / Explorer" module
  (these conventions hold API-wide): **base** `https://www.oklink.com/api/v5/explorer/...`; chain is
  selected via a **`chainShortName`** param (`ETH`/`BSC`/`POLYGON`/…); response envelope is
  **`{"code":"0","msg":"","data":[…]}`** (code "0" = success). Auth header **TODO: confirm** (OKX/OKLink
  convention is `Ok-Access-Key`, unverified).
- **Still needed:** the scraped pages were the **Contract Verification** module, NOT the AML data BIH
  wants. The specific **address-label/entity** endpoint and the **address-risk/KYT** endpoint paths +
  response fields are still `TODO: confirm`. Scaffold the connector shell now (base/`chainShortName`/
  envelope above, keyring `oklink_api_key`, capabilities, key-gated); fill the two AML endpoints once
  their docs pages are captured. Do NOT guess the endpoint paths.

## Notes for the build

- Build the **framework + all four connector classes with capabilities, config entries, keyring wiring,
  and graceful key-absent behavior now**; leave live request/response specifics as `TODO: confirm` where
  unconfirmed (Bitquery query body; Arkham/OKLink endpoints). This satisfies "build the options now, test
  later" without faking cassettes.
- Confirmation status (2026-06-28): **Arkham API fully confirmed** (base, `API-Key` auth, attribution +
  risk endpoints, Address schema). **MisTrack** specced in its own note. **Bitquery** endpoint confirmed;
  only the exact GraphQL query body + token type (Bearer vs X-API-KEY) remain `TODO: confirm`. **OKLink**
  docs are not machine-readable — its endpoint/field shapes remain `TODO: confirm` (OpenAPI/Postman or
  vendor contact needed).
- Update `docs/connectors.md §6` (mark these optional/paid) + a short "configuring paid sources" operator
  section (which keyring entries to set) + `PROGRESS.md`.

## Sources
- Bitquery docs: https://docs.bitquery.io/docs/intro/ · auth: https://docs.bitquery.io/docs/category/authorisation/
- MisTrack: docs/findings/misttrack_reconciliation.md
- Arkham API: https://intel.arkm.com/api/docs
- OKLink AML: https://www.oklink.com/docs/aml_en/
