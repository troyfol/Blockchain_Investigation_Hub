# Findings — Arkham export vs. the `arkham.py` parser

**Date:** 2026-06-28
**Author:** investigation/tuning pass
**Inputs:** two real Arkham CSV exports at repo root — `arkham_txns.csv` (Binance entity, 1 EVM row) and `arkham_satoshi.csv` (Genesis address, 16 Bitcoin rows), both from the logged-in UI "download" button; assumed fixture `backend/tests/fixtures/imports/arkham_sample.csv`; parser `backend/app/connectors/imports/arkham.py`.

**Note:** the fixture's middle address (Binance Hot 15, `0x8617…8070D`) has **no transfers on Arkham**, so there is no export for it.

## TL;DR

The parser is built against a schema Arkham does **not** emit. `arkham.py`'s `get_attributions`
assumes **one row = one address attribution** (`address, chain, entity, category, label,
confidence`). The real Arkham UI export is a **transfer log** — **one row = one value movement
between two parties** — with 19 columns and no entity/category/confidence concept at all. This is a
data-domain mismatch, not a header-rename. The import connector's Arkham capability needs to be
re-scoped; the attribution premise cannot be satisfied from this export.

## What Arkham actually exports

Header of `arkham_txns.csv` (19 columns):

```
transactionHash, fromAddress, fromLabel, fromIsContract, toAddress, toLabel,
toIsContract, tokenAddress, type, blockTimestamp, blockNumber, blockHash,
tokenName, tokenSymbol, tokenDecimals, unitValue, tokenId, historicalUSD, chain
```

Sole data row (abridged): a USDT transfer `Bittrex: Hot Wallet → 0x52908400…4169EE7`,
`unitValue=32`, `tokenDecimals=6`, `chain=ethereum`, `blockNumber=14917217`.

Note: `0x52908400…4169EE7` is "Binance Hot 14" in the assumed fixture — here it appears only as a
`toAddress`, and its `toLabel` is just the bare address (unlabeled). So even the *label* the parser
expects is absent from this export.

## Column-by-column reconciliation (assumed → real)

| Parser expects | Present? | Reality in the export |
|---|---|---|
| `address`    | ✗ | Two addresses per row: `fromAddress` / `toAddress`. No single subject address. |
| `chain`      | ✓ | Present, but the values vary: the four real exports carry `ethereum`, `bsc`, `base`, `tron`. The earlier "exact name, no alias map needed" claim held **only for ethereum** — see the Resolution: ids are now alias-normalized to the system's canonical names, and chain *class* (EVM / UTXO / unsupported account) drives ingest vs refusal. |
| `entity`     | ✗ | No entity column. No high-level "Binance"-style grouping field anywhere. |
| `category`   | ✗ | Absent. Nearest signals: `type` (empty in sample) and `fromIsContract`/`toIsContract`. |
| `label`      | ~ | `fromLabel`/`toLabel` exist, but they are transfer-party labels and frequently just echo the address. |
| `confidence` | ✗ | No confidence concept anywhere in the export. |

Everything else in the export (`transactionHash`, `tokenAddress/Name/Symbol/Decimals`,
`unitValue`, `tokenId`, `historicalUSD`, `block*`, `*IsContract`) has **no slot** in the parser's
attribution model — because it's transfer data.

## Why this matters against the invariants

- The export maps naturally onto the **`transfer` fact** (Invariant #5: EVM `transfer`, A→B is a
  fact): `fromAddress → toAddress`, asset, amount, tx hash, chain, block. See `models/onchain.py`
  `Transfer` and `migration 0002_onchain_facts.sql` (`transfer` table, natural key
  `(transaction_id, transfer_type, position)`).
- It does **not** map onto `attribution` / `entity_membership`, which is what `arkham.py`
  currently writes.

## Two paths forward

### Path A — re-aim the Arkham import at transfer ingest (recommended)

Treat `arkham_txns.csv` as a `transfer` source and route it through the existing canonical
transfer path (mirror the `etherscan_adapter.py` `ParsedTransaction`/`ParsedTransfer` shape so the
DB writer resolves addresses/assets to ids). Proposed field mapping:

| Export column | Canonical target | Notes / decisions |
|---|---|---|
| `transactionHash` | `Transaction.tx_hash` | natural key for the tx |
| `chain` | `Transaction.chain` / `Transfer.chain` / `Address.chain` / `Asset.chain` | values already canonical (`ethereum`) |
| `blockNumber` | `Transaction.block_height` | |
| `blockTimestamp` | `Transaction.block_ts` | ISO8601 already |
| `blockHash` | — | **no field** on `Transaction`; drop or extend model |
| `fromAddress` | `Transfer.from_address_id` | via `upsert_address(address_display=fromAddress)` |
| `toAddress` | `Transfer.to_address_id` | via `upsert_address` |
| `tokenAddress` | `Asset.contract_address` | empty ⇒ native coin; set ⇒ erc20 |
| `tokenSymbol` | `Asset.symbol` | |
| `tokenDecimals` | `Asset.decimals` | |
| `tokenName` | — | no field on `Asset`; drop or extend |
| `unitValue` | `Transfer.amount` | **CONFIRMED display units, not base units.** BTC example: `unitValue=0.00000546`, `decimals=8` = 546 sats. `Transfer.amount` must be raw base-unit integer TEXT, so `amount = round(unitValue × 10^decimals)`. Watch float precision — parse via `Decimal`, not `float`. |
| `type` | **NOT `transfer_type`** | Arkham `type` is **direction relative to the queried subject** (`inflow`/`outflow`; empty when neither party is the subject), not `native/erc20/internal`. Derive `transfer_type` instead from `tokenAddress` (empty ⇒ `native`, set ⇒ `erc20`); `internal` is not distinguishable from this export — `TODO: confirm`. |
| `tokenId` | `Asset` coin-slug (pricing key) | **Coin slug** like `tether` / `bitcoin` (matches `historicalUSD` pricing key, DeFiLlama coin-key style), **not** an NFT id. Use as the valuation join key if needed; not required for the raw transfer. |
| `historicalUSD` | — (NOT a raw fact) | Arkham USD valuation = a sourced claim; keep out of the raw `transfer` write (valuation lives in `services/valuation`). Drop for ingest or route separately. |
| `fromLabel` / `toLabel` | — (NOT a transfer field) | per-address labels; often just echo the address. Do **not** fold into the transfer. If captured at all, separate low-value attribution — but see Path B. |
| `fromIsContract` / `toIsContract` | — | address-type hint; no column in `address`. Drop or extend. |
| *(none)* | `Transfer.position` | **NOT in export.** Single-transfer tx ⇒ `position=0`. Multi-transfer tx ⇒ no log index in the CSV; only row order signals it (cf. etherscan erc20 receipt-log-order assumption). **Idempotency risk (Invariant #7)** — document the assumption and test re-ingest. |
| *(none)* | `Transaction.finality_status` / `confirmations` | no `confirmations` column; can't compute from export alone (need current chain height). Decide: mark `provisional` pending a finality check, vs. treat old blocks as `final` (Invariant #6). |

**CRITICAL — Bitcoin rows must NOT use the transfer path (Invariant #5).** `arkham_satoshi.csv`
proves Arkham emits the *same* `fromAddress → toAddress` shape for Bitcoin (`chain=bitcoin`,
`type=inflow`). That is a **synthesized input→output linkage** — precisely the fact Invariant #5
forbids ("never synthesize an input→output transfer as a fact"; BTC stores `tx_input`/`tx_output`
only). So the importer must **branch on `chain`**: EVM rows → `transfer`; Bitcoin (and other
UTXO) rows → either `tx_output`/`tx_input` (only if the export gives enough to do so faithfully —
it likely does **not**, since it collapses the UTXO set to one from/to pair) or be **rejected with
a clear error** rather than written as a transfer. Treat Arkham's BTC from→to as a *claim inside a
trace at most*, never a raw fact. This single point likely means Path A only cleanly serves EVM,
and Bitcoin needs a separate decision.

Open decisions for Path A: (1) ~~confirm `unitValue` units~~ done — display units, `× 10^decimals`
via `Decimal`; (2) `transfer_type` derivation (`type` is direction, not the enum — use
`tokenAddress` presence; `internal` undetectable → `TODO: confirm`); (3) `position`/idempotency
strategy with no log index (Invariant #7); (4) finality strategy with no confirmations
(Invariant #6); (5) whether to extend models for `blockHash`, `tokenName`, `isContract`, or drop
them (`tokenId` is a coin slug, keep for valuation join only); (6) **Bitcoin branch** per the
critical note above.

### Path B — source attribution separately (only if attribution is actually needed)

The address→entity/category/label/confidence data the parser was written for is **not** in any UI
export. It would come from Arkham's **official API** entity/label endpoints (the no-scraping route,
Invariant #1) — a different connector capability, not this file. `fromLabel`/`toLabel` here are too
thin and unreliable (often bare addresses) to reconstruct attributions from.

## Recommendation

Re-scope the Arkham import connector capability from `get_attributions` to a transfer-ingest
capability fed by this CSV (Path A), and update `docs/connectors.md §6` (the table currently lists
Arkham as `get_attributions (entities/labels)`). Keep `arkham.py`'s attribution code only if/when a
real attribution source (API) is wired up under Path B. Update `PROGRESS.md` with the decision.

## Resolution (implemented 2026-06-28) — Path A, EVM-only

`arkham.py` re-scoped to `get_transactions`; pure mapping in `normalization/arkham_adapter.py` (mirrors
`etherscan_adapter`'s `ParsedTransaction`/`ParsedTransfer`); tests in `tests/unit/test_arkham_parser.py`
over the real fixtures `arkham_txns.csv` (1 EVM row) and `arkham_satoshi.csv` (16 BTC rows), plus pure
adapter unit tests for position/native/rounding/rejection; `docs/connectors.md §6` updated; wrong-schema
`arkham_sample.csv` removed; the multi-source display test now seeds the Arkham attribution from
`arkham-api` (Path B). All decisions are surfaced in code with `TODO: confirm` where the export can't settle them:

- **(a) unitValue → amount:** `amount = round(Decimal(unitValue) × 10^decimals)` (Decimal, never float).
  Confirmed display-units (BTC 0.00000546 @ 8 = 546 sats; USDT historicalUSD==unitValue==32). Non-integral
  products (UI-rounded high-decimal tokens) are flagged `rounded` and surfaced. *Confirm exactness vs a
  chain re-fetch for high-decimal tokens.*
- **(b) type:** Arkham `type` is **direction** (`inflow`/`outflow`), NOT `native/erc20/internal`. Derive
  `transfer_type` from `tokenAddress` presence. `internal` is undetectable from this export — *TODO: confirm*.
- **(c) position / idempotency:** no log index → `position` = row-order index within `(tx, transfer_type)`.
  Re-ingesting the same file is idempotent (tested). Cross-source caveat (vs Etherscan log-order) noted as a follow-up.
- **(d) finality:** no confirmations → `provisional`; a later chain re-fetch upgrades to `final` (Invariant #6).
- **(e) dropped:** `blockHash`, `tokenName`, `from/toIsContract`, `from/toLabel`; `tokenId` confirmed a
  **coin slug** (not erc721) and dropped from the raw transfer.
- **(f) historicalUSD:** dropped from the transfer (sourced valuation claim, not a fact).

**Bitcoin branch (the critical Invariant #5 point):** `arkham_satoshi.csv` confirmed Arkham emits a
`from→to` shape for BTC — `fromAddress` is sometimes a comma-joined UTXO **input set** collapsed into one
pair. Writing that as a `transfer` would synthesize an input→output edge. **Decision: refuse.** The
importer ingests only account-model (EVM) chains; any non-EVM row raises a clear `ConnectorError` and rolls
the whole import back (nothing written) — Bitcoin is ingested via the Esplora connector instead. (Arkham's
BTC from→to could later be represented as a *claim inside a trace* with a `basis`, never a raw fact.)

**Not done (Path B):** address→entity/label/confidence attribution — not in any UI export; requires the
Arkham API.

## Update (2026-06-28) — multichain exports: bsc fix, alias map, Tron decision

Three more real exports (`arkham_bsc_native_multitx.csv` 16 bsc rows, `arkham_multichain_tron.csv` with
bsc/ethereum/base/tron, plus the original two) exposed and fixed the following:

- **bsc was wrongly rejected.** `ACCOUNT_MODEL_CHAINS` mirrored `NATIVE_SYMBOL` = {ethereum, arbitrum,
  optimism, base, polygon} — so every BNB Smart Chain row was treated as non-EVM. **Fix:** `bsc` added to
  `NATIVE_SYMBOL` (`bsc→BNB`), `config.CHAINID_TO_NAME` (`56→bsc`), and `DEFAULT_FINALITY_THRESHOLDS`
  (`bsc=15`, conservative placeholder — *TODO: confirm BSC's fast-finality threshold*). bsc rows now ingest
  as transfers (native BNB 18-dec; the BSC USDT row is 18-dec, unlike Ethereum's 6).
- **Chain alias map.** Arkham ids are normalized to the system's canonical names via
  `ARKHAM_CHAIN_ALIASES` (e.g. `bnb`/`binance-smart-chain`→`bsc`, `matic`→`polygon`) before classification.
  The real ids (`bsc`/`base`/`ethereum`/`tron`) already match canonical, but the alias layer future-proofs
  synonyms — the earlier "no alias map needed" note held only for ethereum.
- **Tron decision — two rejection classes, made explicit.** Tron is **account-model but non-EVM** (base58
  `T…` addresses; `canonical_address` would raise). It is **not** a UTXO/Invariant-#5 fabrication risk, so
  it must NOT share Bitcoin's rationale. Classification now splits: **UTXO** (Bitcoin → hard refuse, raises,
  rolls back — Invariant #5) vs **unsupported account-model** (Tron → **skipped and reported**, while the
  supported EVM rows in the same multichain export still ingest). The connector result carries
  `unsupported_skipped` / `unsupported_chains` so nothing is silently dropped. (Bitcoin still all-or-nothing;
  the strengthened `arkham_satoshi.csv` test proves all 16 rows classify UTXO, the 5 comma-joined input-set
  rows never reach `canonical_address`, and after the raise `transaction_/transfer/asset/address/source_query`
  are all empty.)
- **Confirmed-empty `type`** across both new files even on the OUTFLOW tab → `type` is moot (we derive
  `transfer_type` from `tokenAddress`, already). **Decimals/symbol never blank** in real data (6/8/18,
  symbol always present) — the 0-decimal default for a missing token-decimal stays but is *untriggered*.
