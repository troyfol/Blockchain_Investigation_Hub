# Validation case — Colonial Pipeline / DarkSide ransom (2021) · BTC

**Purpose:** the BTC half of the LEA/FIU verification (pairs with the EVM Ronin case). A short, fully
address-pinned ransom→seizure flow — the cleanest possible test of BIH's **UTXO** path and **Invariant
#5** (Bitcoin stores `tx_input`/`tx_output` only; an input→output transfer is **never** a fact — the
linkage exists only inside a `trace` as a `basis=fifo` claim). **Prompt order: this case FIRST**, then
Bitfinex after it verifies. (See `docs/validation/ronin_lazarus_case.md` for the verified EVM half.)

## Ground truth (public sources)

- **2021-05-08:** Colonial Pipeline paid a **75 BTC** ransom to DarkSide.
- The ransom address forwarded to a DarkSide-admin address, which sent **63.7 BTC (85%)** to the
  **affiliate** who ran the attack.
- **2021-06-07:** DOJ/FBI announced seizure of the **63.7 BTC** (~$2.3M). The FBI **held the private key**
  to the address holding the affiliate's share and seized it. Warrant: Magistrate Judge Laurel Beeler
  (N.D. Cal). Outcome: published DOJ seizure.

## Addresses (all public, from the DOJ release + seizure affidavit)

- **Ransom payment address** (Colonial → attackers): `15JFh88FcE4WL6qeMLgX5VEAFCbRXjc9fr` (legacy P2PKH)
- **Affiliate / seized-share address** (63.7 BTC, named in the seizure affidavit) — **ANCHOR:**
  `bc1qq2euq8pw950klpjcawuy4uj39ym43hs6cfsegq`
- **FBI holding address** (63.7 BTC moved here on seizure, **unspent**):
  `bc1qpx7vyv5tp7dm0g475ev527krg764t73dh77gls`

Documented flow: `15JFh88…` (75 BTC) → DarkSide admin → **`bc1qq2euq8…` (63.7 BTC)** → **`bc1qpx7vyv5…`
(FBI holding)**.

## What a correct BIH run must reproduce (comparison checklist)

1. **Facts (Esplora/UTXO):** ingest the addresses' transactions as **`tx_input`/`tx_output`** rows — the
   75 BTC arriving at the ransom address, the 63.7 BTC at the anchor, and the move to the FBI holding
   address (unspent). **Never** written as a synthesized A→B `transfer` (Invariant #5). Provenance on
   every row (Inv #3).
2. **Invariant #5 — the headline BTC validation:** the ransom→affiliate→seizure *linkage* must exist
   **only inside a `trace`** as a labeled `basis=fifo` claim — there must be **no** `transfer` fact
   asserting input→output movement, and the audit that forbids fabricated UTXO edges must pass.
3. **Tracing (FIFO):** a trace from the ransom address forward (or the FBI holding address backward)
   reconstructs the 75 → 63.7 BTC path as FIFO claims; the seized 63.7 BTC is visible as **unspent** at
   the FBI holding address.
4. **Attribution / risk:** DarkSide is a ransomware operator — if a public GraphSense TagPack tags these
   addresses (DarkSide / ransomware `abuse`), they surface as attribution + categorical risk; otherwise
   **gracefully absent** (never fabricate, Inv #4). (Don't assume an OFAC hit — the seizure address is
   not necessarily SDN-listed; let the connectors speak.)
5. **Report/export:** a reproducible `.casefile` with provenance and the FIFO trace cited.

## Caveats / what "correct" looks like

- This is the **clean-room test of Invariant #5**: the whole point is that BIH represents the
  ransom→seizure flow as **facts (inputs/outputs) + a FIFO trace claim**, not as fabricated transfer
  edges. Stopping short of that — or inventing a transfer — is the failure mode to catch.
- Small, tractable flow (a handful of txs) → fast deterministic golden guard.
- **Find-the-gaps, not pass-the-test:** if BIH diverges (fabricated edge, lost provenance, trace can't
  reconstruct the path), record it in `PROGRESS.md` + a Results section here; do not tune assertions.

## Results — actual vs. expected (recreated in BIH 2026-06-28)

Harness: `backend/tests/integration/test_colonial_pipeline_validation.py` (a permanent `make smoke` guard).
Fixtures (`backend/tests/fixtures/validation/colonial_*`): RAW Blockstream Esplora responses recorded ONCE
(Esplora is a keyless public API) for the three DOJ-named addresses — replayed offline. **Outcome: this is
the clean-room Invariant #5 test and it PASSES — BIH represents the ransom→seizure flow as UTXO facts
(inputs/outputs) + a FIFO trace claim, never as a fabricated transfer edge. Provenance intact; attribution
honestly absent.** One real connector gap was found *and fixed*.

The actual recorded flow (block heights pin it): the ransom address **15JFh88…** received **75.0003 BTC**
(tx `6a798026`, block 682599) and forwarded it (tx `915fb4f0`, block 682603); the affiliate/anchor
**bc1qq2euq8…** was funded **69.60422177 BTC** (tx `daf38c7b`, block 685213); the **seizure** tx
`943f2d57` (block 686683) moved **63.69996546 BTC** to the FBI holding address **bc1qpx7vyv5…** (+ 5.904
change back to the anchor).

| # | Checklist item | Result | Actual vs. expected (the gaps) |
|---|---|---|---|
| 1 | **Facts** as `tx_input`/`tx_output`, never a transfer | ✅ PASS | The 75 BTC ransom payment, the 69.604 BTC affiliate funding, and the 63.7 BTC seizure all ingest as `tx_output` rows; **zero `transfer` rows** (Invariant #5). Every UTXO row carries a `source_query` (Inv #3). |
| 2 | **Invariant #5** — no fabricated input→output edge | ✅ PASS (headline) | Every `v_value_movement` UTXO row has **`src_address_id` NULL** and the **`no-fabricated-utxo-edge` audit passes**. The ransom→affiliate→seizure *linkage* exists **only** inside the trace as `basis='fifo'` claims — there is no transfer fact asserting input→output movement. |
| 3 | **Tracing (FIFO)** reconstructs the path; seized BTC visible | ✅ PASS (+ point-in-time gap) | The seizure tx's input spends the anchor's funding output — that it spends *that outpoint* is a ledger **fact** (`prev_output_id`); `fifo_trace_transaction` then reconstructs the **63.7 BTC → FBI** hop as a `basis='fifo'` claim (`is_convention=True`). **FIND-THE-GAP:** the checklist's "63.7 BTC **unspent** at the FBI address" was true at seizure (2021-06-07), but in **current** chain data the government has since **moved** it (tx `6f8fcc22`, block 696400, ~2021-09). BIH (showing live state) correctly marks the seizure output **spent**, with that later move as its spender. The seizure **fact** is intact; the test asserts the live state and documents the divergence rather than asserting a stale "unspent". |
| 4 | **Attribution / risk** | ✅ PASS (honest absence) | No public GraphSense TagPack covers these DarkSide operational addresses, and **no OFAC hit is assumed** (the seizure address is not SDN-listed — we let the connectors speak). BIH invents nothing: **zero** attribution/risk rows (Inv #4 / no-synthesis). |
| 5 | **Report / export** with provenance + FIFO trace | ✅ PASS | Audits 10/10; a reproducible, self-contained `.casefile` exports and re-verifies (the Ronin WAL-checkpoint hardening covers the open-connection case). |

**Gap found + FIXED — UTXO intra-batch spend linkage was stream-order-dependent.** The Esplora connector
resolved each input's `prev_output_id` against outputs *already written*, in Esplora's **newest-first**
stream order. So an address's own internal spend-chain could be left **unlinked within a single sync** when
the spending tx is streamed *before* the funding tx it draws on (exactly the anchor's case: the seizure tx
`943f2d57` precedes its funding tx `daf38c7b` in the address listing). Without a fix, the FIFO trace of the
seizure would have depended on ingesting the anchor *before* the FBI address (fragile, order-dependent).
**Fix:** `_write_btc` now does a **two-pass write** — every transaction's outputs first, then resolve all
inputs against the now-complete output set — so intra-batch linkage is order-independent. Regression guard:
`test_intra_batch_linkage_resolves_regardless_of_stream_order` (a single sync that streams the spender
before its funding tx). This strengthens the UTXO half, which is the whole point of this case.

**Other real-data observations (not failures):**
- **Post-seizure noise in a bounded pull.** The three addresses accrued later, unrelated activity that a
  current single-address pull includes: the anchor's newest tx (`4a064218`, block 785974, **2023**) is a
  159-output consolidation sending ~0.00099 BTC of dust to the anchor; the FBI address received later dust
  (2024–2025). Faithfully ingested (current history), but the anchor's **current** ~0.00099 BTC balance is
  unrelated 2023 dust, not affiliate funds — worth flagging to an operator (mirrors the Ronin dust note).
- **Deliberate mid-path gap.** Only the three DOJ-named addresses are ingested, so BIH reconstructs the
  ransom-forward hop and the funding→seizure hop **independently**, not one end-to-end chain — the
  DarkSide-admin redistribution between them is downstream/uningested. Honest bounded behavior: BIH does
  **not** fabricate a bridging edge across the gap.

**Verdict:** the clean-room Invariant #5 test passes — Bitcoin is stored as inputs/outputs with real
spend-linkage, the input→output flow lives solely in a `basis='fifo'` trace claim, provenance and the
export spine are intact, and attribution is honestly absent. The one connector gap (intra-batch linkage
ordering) is fixed and guarded.

## Next step

Prompt the Bitfinex 2016 case (`docs/validation/bitfinex_2016_case.md`) for heavy co-spend clustering —
the entity-resolution stress test that complements this Invariant-#5 clean-room.

## Sources
- DOJ — seizure announcement: https://www.justice.gov/archives/opa/pr/department-justice-seizes-23-million-cryptocurrency-paid-ransomware-extortionists-darkside
- Chainalysis — how the FBI traced DarkSide's funds: https://www.chainalysis.com/blog/darkside-colonial-pipeline-ransomware-seizure-case-study/
- Elliptic — US authorities seize DarkSide ransom: https://www.elliptic.co/blog/us-authorities-seize-darkside
